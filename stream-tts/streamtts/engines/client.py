"""Общий клиент сервера синтеза и очередь доступа к нему: строго один запрос за раз.

Протокол — подмножество API cosyvoice-server (его код не наш), другие движки (espeech-server) повторяют его:
GET /status, GET /speaker, DELETE /speaker/<имя>, POST /speaker?type=extract, POST /speaker/save,
POST /tts {text, voice, seed?, stream?, …} — ответ WAV-заголовок и PCM16 24 кГц, POST /tts/stop. Свои поля
запроса (настройки модели из settings) движок добавляет в params(). Ядро stream-tts (engine.py, voices.py, bench.py)
пользуется только lane, ok/error, status, speakers, synth, stream, streams и stop.

Расширения сервер объявляет в /status: engine, api_version, capabilities (stream, accent, extract, train,
voice_formats), params — схема настроек модели для админки ({имя: {type: choice, choices, default, title, help}}).
Движок связывает параметры схемы с ключами settings (SETTINGS) и передаёт их в каждом запросе; схема на случай,
когда сервер ещё не ответил, — DEFAULT_PARAMS."""
import asyncio, contextlib, heapq, itertools, logging, time

import aiohttp

from .. import audio
from ..i18n import tr

log = logging.getLogger("tts")

LIVE, PREVIEW, BACKGROUND = 0, 1, 2   # приоритеты: озвучка наград, «послушать» в интерфейсе, примеры голосов
# Сколько держать простаивающее соединение с сервером. cosyvoice-server (cpp-httplib) закрывает keep-alive через 5 с,
# а aiohttp по умолчанию переиспользует соединение до 15 с: фраза через 5–15 с после прошлого запроса уходила в
# закрытое соединение и падала с «Server disconnected» (возврат баллов; так падал test_real.py::test_synth_whole)
KEEPALIVE = 2.0


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
    ENGINE = "tts"      # id движка: в тексте ошибок сервера
    SETTINGS = {}       # параметр из /status.params -> ключ settings
    DEFAULT_PARAMS = {}
    frontend_ready = frontend_loaded = False   # ONNX-фронтенд для голосов из записи есть только у CosyVoice

    def __init__(self, url, settings, api_key="", lane=None):
        self.url = url
        self.s = settings
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.session = None
        self.lane = lane or Lane()   # общая для всех движков: при смене модели очередь к серверу та же
        self.ok = None          # доступен ли сервер (последняя проверка)
        self.error = ""
        self.info = {}
        self.checked = 0.0

    async def start(self):
        self.session = aiohttp.ClientSession(headers=self.headers,
                                             connector=aiohttp.TCPConnector(keepalive_timeout=KEEPALIVE))

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

    @property
    def capabilities(self):
        return self.info.get("capabilities") or {}

    # --- настройки модели ---
    def schema(self):
        """Настройки модели для админки: [{key (в settings), param, type, choices, default, title, help, value}]."""
        params = self.info.get("params") or {}
        out = []
        for name, key in self.SETTINGS.items():
            p = params.get(name) or self.DEFAULT_PARAMS.get(name)
            if p:
                out.append(dict(p, key=key, param=name, value=self.s[key]))
        return out

    def check_param(self, key, value):
        """Значение настройки модели из админки; ValueError — не из вариантов схемы."""
        p = next((p for p in self.schema() if p["key"] == key), None)
        if p is None:
            raise ValueError(key)
        if p.get("type") == "choice":
            value = type(p["choices"][0])(value)
            if value not in p["choices"]:
                raise ValueError(f"{key}: {value}")
        return value

    def bench_info(self):
        """Настройки модели, с которыми сделан замер скорости (bench.py)."""
        return {}

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
    async def extract(self, name, text, wav, **fields):
        """POST /speaker?type=extract: голос из образца (WAV) и его расшифровки, сразу доступен для синтеза;
        fields — свои поля движка (у ESpeech tempo), None не передаётся."""
        form = aiohttp.FormData()
        form.add_field("name", name)
        form.add_field("text", text)
        for k, v in fields.items():
            if v is not None:
                form.add_field(k, str(v))
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

    async def accent(self, text):
        """POST /accent: как прочитает модель — {normalized, accented} (capabilities.accent)."""
        async with self.session.post(f"{self.url}/accent", json=dict(text=text),
                                     timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                await self._fail(r)
            return await r.json()

    async def delete_speaker(self, name):
        with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError):
            async with self.session.delete(f"{self.url}/speaker/{name}", timeout=aiohttp.ClientTimeout(total=15)):
                pass
