"""Поддельный cosyvoice-server для e2e-тестов stream-tts: те же адреса API, вместо речи — синус.

У каждого голоса своя частота и громкость (VOICES): по частоте тест узнаёт, каким голосом прозвучала фраза, а разная
громкость проверяет выравнивание до −16 LUFS. Длина звука — как у речи, 12 символов в секунду (config.CHARS_PER_SEC),
умноженная на length_factor.

Каждый запрос к /tts записывается в tts (тело запроса и stopped), а max_active считает, сколько синтезов шло
одновременно: к настоящему серверу можно только по одному (параллельные запросы его роняют).

Ручки, которые тест меняет на ходу (из другого потока: только простые поля, без asyncio-объектов):
- hold — синтез ждёт, пока его не снимут или не придёт /tts/stop (чтобы застать фразу на синтезе);
- fail — подстрока текста, на которой /tts отвечает 500;
- down — /status отвечает 500 (сервер недоступен);
- length_factor — во сколько раз звук длиннее расчётного (проверка жёсткой обрезки)."""
import asyncio, io, struct, time, wave

import numpy as np
from aiohttp import web

SR = 24000
CHARS_PER_SEC = 12.0
TOKENS_PER_SEC = 25.0
# голос -> (частота, Гц; амплитуда). 800 Гц не занимать: это писк вместо мата
VOICES = {"alpha": (220.0, 0.05), "bravo": (330.0, 0.3), "charlie": (440.0, 0.6)}


def tone(voice, seconds):
    freq, amp = VOICES[voice]
    t = np.arange(max(1, int(seconds * SR))) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def pcm16(x):
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def wav(x):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm16(x))
    return buf.getvalue()


def stream_header():
    """WAV-заголовок потока: длина неизвестна, как у настоящего сервера."""
    return (b"RIFF" + struct.pack("<I", 0xFFFFFFFF) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, SR, SR * 2, 2, 16) + b"data" + struct.pack("<I", 0xFFFFFFFF))


class FakeCosy:
    def __init__(self):
        self.voices = dict(VOICES)
        self.tts = []
        self.active = self.max_active = 0
        self.stops = 0
        self.reset()
        self.runner = None
        self.url = ""

    def reset(self):
        """Ручки — в исходное состояние, записи запросов — заново."""
        self.hold = False
        self.fail = ""
        self.down = False
        self.length_factor = 1.0
        self.chunk_delay = 0.0
        self.tts = []
        self.max_active = self.active
        self.stops = 0
        self._stop = False

    # --- запуск в цикле событий теста ---
    async def start(self):
        app = web.Application()
        app.router.add_get("/status", self.status)
        app.router.add_get("/frontend/model", self.no_frontend)
        app.router.add_get("/speaker", self.speakers)
        app.router.add_delete("/speaker/{name}", self.speaker_delete)
        app.router.add_post("/tts", self.synth)
        app.router.add_post("/tts/stop", self.stop)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def close(self):
        if self.runner:
            await self.runner.cleanup()

    # --- API ---
    async def status(self, request):
        if self.down:
            return web.Response(status=500, text="down")
        return web.json_response(dict(model_loaded=True, model="fake", frontend_available=False))

    async def no_frontend(self, request):
        """Сборка без ONNX-фронтенда: голоса из записи не создаются (редактор проверят отдельные тесты)."""
        return web.Response(status=404)

    async def speakers(self, request):
        return web.json_response(sorted(self.voices))

    async def speaker_delete(self, request):
        self.voices.pop(request.match_info["name"], None)
        return web.json_response(dict(ok=True))

    async def stop(self, request):
        self.stops += 1
        self._stop = True
        return web.json_response(dict(ok=True))

    async def _wait_hold(self):
        while self.hold and not self._stop:
            await asyncio.sleep(0.02)

    async def synth(self, request):
        body = await request.json()
        rec = dict(body, ts=time.time(), stopped=False, done=False)
        self.tts.append(rec)
        self._stop = False   # /tts/stop останавливает только текущий синтез
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            text, voice = body.get("text", ""), body.get("voice", "")
            if voice not in self.voices:
                return web.Response(status=404, text=f"unknown voice {voice}")
            if self.fail and self.fail in text:
                return web.Response(status=500, text="fake failure")
            x = tone(voice, len(text) / CHARS_PER_SEC * self.length_factor)
            await self._wait_hold()
            if not body.get("stream"):
                if self._stop:   # остановленный синтез отдаёт то, что успел
                    rec["stopped"] = True
                    x = x[:SR // 10]
                return web.Response(body=wav(x), content_type="audio/wav")
            resp = web.StreamResponse(headers={"Content-Type": "audio/wav"})
            await resp.prepare(request)
            await resp.write(stream_header())
            step = int(int(body.get("chunk_tokens") or 28) / TOKENS_PER_SEC * SR)
            for i in range(0, len(x), step):
                if self._stop:
                    rec["stopped"] = True
                    break
                await resp.write(pcm16(x[i:i + step]))
                if self.chunk_delay:
                    await asyncio.sleep(self.chunk_delay)
            await resp.write_eof()
            return resp
        finally:
            rec["done"] = True
            self.active -= 1
