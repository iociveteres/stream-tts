"""Общий клиент сервера синтеза и очередь доступа к нему: строго один запрос за раз.

Протокол — подмножество API cosyvoice-server (его код не наш), другие движки (espeech-server) повторяют его:
GET /status, GET /speaker, DELETE /speaker/<имя>, POST /speaker?type=extract, POST /speaker/save,
POST /tts {text, voice, seed?, stream?, …} — ответ WAV-заголовок и PCM16 24 кГц, POST /tts/stop. Свои поля
запроса (настройки модели из settings) движок добавляет в params(). Ядро stream-tts (engine.py, voices.py, bench.py)
пользуется только lane, ok/error, status, speakers, synth, stream, streams и stop."""
import asyncio, contextlib, heapq, itertools, logging, time

import aiohttp

from .. import audio
from ..i18n import tr

log = logging.getLogger("tts")

LIVE, PREVIEW, BACKGROUND = 0, 1, 2   # приоритеты: озвучка наград, «послушать» в интерфейсе, примеры голосов


class Lane:
    """Блокировка с приоритетом: при освобождении её получает ожидающий с меньшим priority."""

    def __init__(self):
        self.busy = False
        self.holder = None
        self.waiters = []
        self.seq = itertools.count()

    @contextlib.asynccontextmanager
    async def hold(self, priority, what=""):
        if self.busy:
            fut = asyncio.get_running_loop().create_future()
            heapq.heappush(self.waiters, (priority, next(self.seq), fut))
            try:
                await fut
            except asyncio.CancelledError:
                if fut.done() and not fut.cancelled():
                    self._release()
                raise
        else:
            self.busy = True
        self.holder = (priority, what)
        try:
            yield
        finally:
            self.holder = None
            self._release()

    def _release(self):
        while self.waiters:
            _, _, fut = heapq.heappop(self.waiters)
            if not fut.done():
                fut.set_result(None)
                return
        self.busy = False

    def waiting(self, priority):
        return any(p <= priority and not f.done() for p, _, f in self.waiters)

    def queued(self):
        """Сколько запросов ждут сервер синтеза (в /api/state: по нему e2e-тесты видят, что запрос встал в очередь)."""
        return sum(not f.done() for _, _, f in self.waiters)


class Client:
    ENGINE = "tts"   # id движка: в тексте ошибок сервера

    def __init__(self, url, settings, api_key=""):
        self.url = url
        self.s = settings
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.session = None
        self.lane = Lane()
        self.ok = None          # доступен ли сервер (последняя проверка)
        self.error = ""
        self.info = {}
        self.checked = 0.0

    async def start(self):
        self.session = aiohttp.ClientSession(headers=self.headers)

    async def close(self):
        if self.session:
            await self.session.close()

    async def _fail(self, r):
        raise RuntimeError(f"{self.ENGINE} {r.status}: {(await r.text())[:200]}")

    async def status(self):
        try:
            async with self.session.get(f"{self.url}/status", timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 401:
                    raise RuntimeError(tr("неверный COSYVOICE_API_KEY"))
                r.raise_for_status()
                self.info = await r.json()
            self.ok, self.error = bool(self.info.get("model_loaded")), ""
            if not self.ok:
                self.error = tr("модель не загружена")
            await self._checked()
        except Exception as e:
            self.ok, self.error = False, str(e) or type(e).__name__
            self._lost()
        self.checked = time.time()
        return self.ok

    async def _checked(self):
        """Сервер ответил на /status: движок дочитывает своё."""

    def _lost(self):
        """Сервер не ответил: перезапущенный мог получить другие флаги."""

    async def speakers(self):
        async with self.session.get(f"{self.url}/speaker", timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return sorted(await r.json())

    # --- синтез ---
    def params(self, stream):
        """Поля запроса /tts из настроек модели: передаются в каждом запросе."""
        return {}

    def streams(self, text):
        """Стоит ли синтезировать text потоком (при включённом стриминге)."""
        return True

    def _body(self, voice, text, stream, seed=None):
        body = dict(text=text, voice=voice, stream=stream, **self.params(stream))
        if seed is not None:
            body["seed"] = seed
        return body

    async def synth(self, voice, text, seed=None):
        """Целиком, float32 24 кГц."""
        async with self.session.post(f"{self.url}/tts", json=self._body(voice, text, False, seed),
                                     timeout=aiohttp.ClientTimeout(total=300)) as r:
            if r.status != 200:
                await self._fail(r)
            return audio.read_wav(await r.read())

    async def stream(self, voice, text):
        """Потоком: куски float32 по мере синтеза. Поток — это WAV-заголовок и дальше PCM16."""
        async with self.session.post(f"{self.url}/tts", json=self._body(voice, text, True),
                                     timeout=aiohttp.ClientTimeout(total=None, sock_read=60)) as r:
            if r.status != 200:
                await self._fail(r)
            buf, header = b"", True
            async for chunk in r.content.iter_any():
                buf += chunk
                if header:
                    if buf[:4] == b"RIFF":
                        i = buf.find(b"data")
                        if i < 0 or len(buf) < i + 8:
                            continue
                        buf = buf[i + 8:]
                    header = False
                n = len(buf) // 2 * 2
                if n:
                    yield audio.from_pcm16(buf[:n])
                    buf = buf[n:]

    async def stop(self):
        """POST /tts/stop: сервер дорабатывает текущий шаг и останавливается."""
        try:
            async with self.session.post(f"{self.url}/tts/stop", timeout=aiohttp.ClientTimeout(total=15)) as r:
                return r.status == 200
        except Exception:
            return False

    # --- голоса из записи (editor.py) ---
    async def extract(self, name, text, wav):
        """POST /speaker?type=extract: голос из образца (WAV) и его расшифровки, сразу доступен для синтеза."""
        form = aiohttp.FormData()
        form.add_field("name", name)
        form.add_field("text", text)
        form.add_field("audio", wav, filename="sample.wav", content_type="audio/wav")
        async with self.session.post(f"{self.url}/speaker", params={"type": "extract"}, data=form,
                                     timeout=aiohttp.ClientTimeout(total=300)) as r:
            if r.status != 200:
                await self._fail(r)

    async def save_speaker(self, name, path):
        """POST /speaker/save: голос в файл; path — на машине сервера."""
        async with self.session.post(f"{self.url}/speaker/save", json=dict(name=name, path=path),
                                     timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status != 200:
                await self._fail(r)

    async def delete_speaker(self, name):
        with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError):
            async with self.session.delete(f"{self.url}/speaker/{name}", timeout=aiohttp.ClientTimeout(total=15)):
                pass
