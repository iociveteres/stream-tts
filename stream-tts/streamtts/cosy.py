"""Клиент cosyvoice-server и очередь доступа к нему: строго один запрос за раз."""
import asyncio, contextlib, heapq, itertools, logging, time

import aiohttp

from . import audio
from .i18n import tr

log = logging.getLogger("cosy")

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


class Cosy:
    def __init__(self, url, api_key=""):
        self.url = url
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.session = None
        self.lane = Lane()
        self.ok = None          # доступен ли сервер (последняя проверка)
        self.error = ""
        self.info = {}
        self.checked = 0.0
        self.frontend_paths = None   # пути ONNX-фронтенда с сервера (GET /frontend/model); {} — не настроен

    async def start(self):
        self.session = aiohttp.ClientSession(headers=self.headers)

    async def close(self):
        if self.session:
            await self.session.close()

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
            if self.frontend_paths is None:
                try:
                    self.frontend_paths = await self._frontend_paths()
                except Exception as e:   # синтезу это не мешает: повтор при следующей проверке
                    log.warning("пути ONNX-фронтенда: %s", e)
        except Exception as e:
            self.ok, self.error = False, str(e) or type(e).__name__
            self.frontend_paths = None   # перезапущенный сервер мог получить другие флаги
        self.checked = time.time()
        return self.ok

    # --- ONNX-фронтенд: нужен только для голосов из записи (editor.py), грузится по требованию ---
    async def _frontend_paths(self):
        """Пути из флагов --speech-tokenizer/--campplus: сервер отдаёт их и до загрузки фронтенда."""
        async with self.session.get(f"{self.url}/frontend/model", timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 404:   # сборка cosyvoice.cpp без этого API
                return {}
            r.raise_for_status()
            d = await r.json()
        return d if d.get("speech_tokenizer") and d.get("campplus") else {}

    @property
    def frontend_loaded(self):
        return bool(self.info.get("frontend_available"))

    @property
    def frontend_ready(self):
        """Фронтенд загружен или его можно загрузить."""
        return self.frontend_loaded or bool(self.frontend_paths)

    async def load_frontend(self):
        """Вызывать под lane.hold: загрузка занимает сервер ~3 с."""
        if self.frontend_loaded:
            return False
        if not self.frontend_paths:
            raise RuntimeError(tr("на сервере синтеза нет фронтенда для создания голосов: скачайте пакет"))
        async with self.session.post(f"{self.url}/frontend/model/load", json=self.frontend_paths,
                                     timeout=aiohttp.ClientTimeout(total=120)) as r:
            if r.status != 200:
                raise RuntimeError(f"cosyvoice {r.status}: {(await r.text())[:200]}")
        self.info["frontend_available"] = True
        return True

    async def unload_frontend(self):
        async with self.session.post(f"{self.url}/frontend/model/unload", timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                raise RuntimeError(f"cosyvoice {r.status}: {(await r.text())[:200]}")
        self.info["frontend_available"] = False

    async def speakers(self):
        async with self.session.get(f"{self.url}/speaker", timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return sorted(await r.json())

    def _body(self, voice, text, ratio, stream, seed=None, chunk=None):
        body = dict(text=text, voice=voice, mode="zero-shot", response_format="wav", stream=stream)
        if ratio:
            body["max_token_text_ratio"] = ratio
        if chunk:
            body["chunk_tokens"] = chunk
        if seed is not None:
            body["seed"] = seed
        return body

    async def synth(self, voice, text, ratio=None, seed=None):
        """Целиком, float32 24 кГц."""
        async with self.session.post(f"{self.url}/tts", json=self._body(voice, text, ratio, False, seed),
                                     timeout=aiohttp.ClientTimeout(total=300)) as r:
            if r.status != 200:
                raise RuntimeError(f"cosyvoice {r.status}: {(await r.text())[:200]}")
            return audio.read_wav(await r.read())

    async def stream(self, voice, text, ratio=None, chunk=None):
        """Потоком: куски float32 по мере синтеза. Поток — это WAV-заголовок и дальше PCM16.

        chunk передаётся всегда: сервер запоминает chunk_tokens из прошлого запроса и не сбрасывает его."""
        async with self.session.post(f"{self.url}/tts", json=self._body(voice, text, ratio, True, chunk=chunk),
                                     timeout=aiohttp.ClientTimeout(total=None, sock_read=60)) as r:
            if r.status != 200:
                raise RuntimeError(f"cosyvoice {r.status}: {(await r.text())[:200]}")
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

    # --- голоса из записи (editor.py): нужен ONNX-фронтенд на сервере ---
    async def extract(self, name, text, wav):
        """POST /speaker?type=extract: голос из образца (WAV) и его расшифровки, сразу доступен для синтеза."""
        form = aiohttp.FormData()
        form.add_field("name", name)
        form.add_field("text", text)
        form.add_field("audio", wav, filename="sample.wav", content_type="audio/wav")
        async with self.session.post(f"{self.url}/speaker", params={"type": "extract"}, data=form,
                                     timeout=aiohttp.ClientTimeout(total=300)) as r:
            if r.status != 200:
                raise RuntimeError(f"cosyvoice {r.status}: {(await r.text())[:200]}")

    async def save_speaker(self, name, path):
        """POST /speaker/save: голос в .gguf; path — на машине сервера."""
        async with self.session.post(f"{self.url}/speaker/save", json=dict(name=name, path=path),
                                     timeout=aiohttp.ClientTimeout(total=60)) as r:
            if r.status != 200:
                raise RuntimeError(f"cosyvoice {r.status}: {(await r.text())[:200]}")

    async def delete_speaker(self, name):
        with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError):
            async with self.session.delete(f"{self.url}/speaker/{name}", timeout=aiohttp.ClientTimeout(total=15)):
                pass

    async def stop(self):
        """POST /tts/stop: сервер дорабатывает текущий шаг и останавливается (только в режиме --webui)."""
        try:
            async with self.session.post(f"{self.url}/tts/stop", timeout=aiohttp.ClientTimeout(total=15)) as r:
                return r.status == 200
        except Exception:
            return False
