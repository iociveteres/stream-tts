"""Клиент cosyvoice-server и очередь доступа к нему: строго один запрос за раз."""
import asyncio, contextlib, heapq, itertools, time

import aiohttp

from . import audio

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

    async def start(self):
        self.session = aiohttp.ClientSession(headers=self.headers)

    async def close(self):
        if self.session:
            await self.session.close()

    async def status(self):
        try:
            async with self.session.get(f"{self.url}/status", timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status == 401:
                    raise RuntimeError("неверный COSYVOICE_API_KEY")
                r.raise_for_status()
                self.info = await r.json()
            self.ok, self.error = bool(self.info.get("model_loaded")), ""
            if not self.ok:
                self.error = "модель не загружена"
        except Exception as e:
            self.ok, self.error = False, str(e) or type(e).__name__
        self.checked = time.time()
        return self.ok

    async def speakers(self):
        async with self.session.get(f"{self.url}/speaker", timeout=aiohttp.ClientTimeout(total=10)) as r:
            r.raise_for_status()
            return sorted(await r.json())

    def _body(self, voice, text, ratio, stream, seed=None):
        body = dict(text=text, voice=voice, mode="zero-shot", response_format="wav", stream=stream)
        if ratio:
            body["max_token_text_ratio"] = ratio
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

    async def stream(self, voice, text, ratio=None):
        """Потоком: куски float32 по мере синтеза. Поток — это WAV-заголовок и дальше PCM16."""
        async with self.session.post(f"{self.url}/tts", json=self._body(voice, text, ratio, True),
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

    async def stop(self):
        """POST /tts/stop: сервер дорабатывает текущий шаг и останавливается (только в режиме --webui)."""
        try:
            async with self.session.post(f"{self.url}/tts/stop", timeout=aiohttp.ClientTimeout(total=15)) as r:
                return r.status == 200
        except Exception:
            return False
