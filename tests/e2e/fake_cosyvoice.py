"""Поддельный сервер синтеза для e2e-тестов stream-tts: те же адреса API, вместо речи — синус. По умолчанию —
cosyvoice-server; FakeCosy(engine="espeech") — espeech-server (espeech/server.py): /status с engine, capabilities и
params (схема nfe_steps), POST /accent, словарь ударений пользователя (GET/POST /accents, DELETE /accents/<слово>:
перекрывает разметку, синтез записывает в tts «accented» — как прочитал бы текст), поток кусками по SPEECH_PIECE с,
chunk_tokens не нужен, nfe_steps проверяется по схеме (400, как у настоящего). Голос из образца у espeech — без
фронтенда; извлечение записывает поля tempo и title, текст без «+» размечается, как в /accent.

У каждого голоса своя частота и громкость (voices, сначала VOICES): по частоте тест узнаёт, каким голосом прозвучала
фраза, а разная громкость проверяет выравнивание до −16 LUFS. Длина звука — как у речи, 12 символов в секунду
(config.CHARS_PER_SEC), умноженная на length_factor. Голос из образца (POST /speaker?type=extract) звучит основной
частотой образца: варианты образца из разных мест записи различаются по звуку.

Каждый запрос к /tts записывается в tts (тело запроса и stopped), остальные изменяющие запросы — в calls
(извлечение голоса, сохранение, удаление, фронтенд). max_active считает, сколько таких запросов шло одновременно: к
настоящему серверу можно только по одному (параллельные запросы его роняют).

ONNX-фронтенд (FakeCosy(frontend=True)): GET /frontend/model отдаёт пути, как сервер с флагами --speech-tokenizer и
--campplus, а извлечение голоса без POST /frontend/model/load — ошибка, как у настоящего сервера. Без фронтенда —
404, как у сборки без этого API.

Ручки, которые тест меняет на ходу (из другого потока: только простые поля, без asyncio-объектов):
- hold — синтез ждёт, пока его не снимут или не придёт /tts/stop (чтобы застать фразу на синтезе); строка вместо
  True — ждут только тексты с этой подстрокой;
- fail — подстрока текста, на которой /tts отвечает 500;
- down — /status отвечает 500 (сервер недоступен);
- length_factor — во сколько раз звук длиннее расчётного (проверка жёсткой обрезки);
- fail_save — /speaker/save отвечает 500."""
import asyncio, contextlib, io, re, struct, time, wave

import numpy as np
from aiohttp import web

SR = 24000
CHARS_PER_SEC = 12.0
TOKENS_PER_SEC = 25.0
# голос -> (частота, Гц; амплитуда). 800 Гц не занимать: это писк вместо мата
VOICES = {"alpha": (220.0, 0.05), "bravo": (330.0, 0.3), "charlie": (440.0, 0.6)}
FRONTEND = dict(speech_tokenizer="/models/frontend-onnx/speech_tokenizer_v3.onnx",
                campplus="/models/frontend-onnx/campplus.onnx")
EXTRACTED_AMP = 0.2


def tone(freq, amp, seconds):
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


def dominant_hz(x):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / SR)[np.argmax(spec)])


ESPEECH_PARAMS = {"nfe_steps": {"type": "choice", "choices": [16, 32, 12], "default": 16, "title": "Шагов синтеза",
                                 "help": "16 — быстро и почти как 32; 32 — максимальное качество, вдвое медленнее; "
                                         "12 — быстрее, звучание заметно другое"}}
SPEECH_PIECE = 1.0   # с звука в куске потока espeech
VOWELS = "аеёиоуыэюя"
WORD = re.compile(r"[А-Яа-яЁё+]+")


class FakeCosy:
    def __init__(self, frontend=False, engine="cosyvoice", voices=None):
        self.engine = engine
        self.voices = dict(voices or VOICES)
        self.frontend = dict(FRONTEND) if frontend else None
        self.frontend_loaded = False
        self.tts = []
        self.calls = []
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
        self.fail_save = False
        self.chunk_delay = 0.0
        self.user_accents = {}   # слово -> разметка: словарь ударений пользователя (espeech)
        self.tts = []
        self.calls = []
        self.lists = 0       # сколько раз stream-tts перечитал список голосов (GET /speaker)
        self.max_active = self.active
        self.stops = 0
        self._stop = False

    def called(self, what):
        """Записи calls одного вида: extract, save, delete, frontend_load, frontend_unload."""
        return [c for c in self.calls if c["call"] == what]

    @contextlib.contextmanager
    def _busy(self):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            yield
        finally:
            self.active -= 1

    # --- запуск в цикле событий теста ---
    async def start(self):
        app = web.Application(client_max_size=64 << 20)
        app.router.add_get("/status", self.status)
        app.router.add_get("/frontend/model", self.frontend_paths)
        app.router.add_post("/frontend/model/load", self.frontend_load)
        app.router.add_post("/frontend/model/unload", self.frontend_unload)
        app.router.add_get("/speaker", self.speakers)
        app.router.add_post("/speaker", self.speaker_extract)
        app.router.add_post("/speaker/save", self.speaker_save)
        app.router.add_delete("/speaker/{name}", self.speaker_delete)
        app.router.add_post("/tts", self.synth)
        app.router.add_post("/tts/stop", self.stop)
        if self.engine == "espeech":
            app.router.add_post("/accent", self.accent)
            app.router.add_get("/accents", self.accents)
            app.router.add_post("/accents", self.accents_put)
            app.router.add_delete("/accents/{word}", self.accents_delete)
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
        d = dict(model_loaded=True, model="fake", frontend_available=self.frontend_loaded)
        if self.engine == "espeech":
            d.update(engine="espeech", api_version=1, params=ESPEECH_PARAMS,
                     capabilities=dict(stream=True, accent=True, extract=True, train=False, voice_formats=["espeech"],
                                       user_accents=True))
        return web.json_response(d)

    def _accent(self, text):
        """Как у espeech-server: цифры словами (только 2 — для теста), «+» перед первой гласной каждого слова, поверх —
        словарь пользователя (регистр — из текста). Слово, где ударение уже стоит, остаётся как есть (accent.keep_marked):
        повторная разметка не трогает поправленное вручную."""
        norm = text.replace("2", "два")
        acc = " ".join(w if "+" in w else next((w[:i] + "+" + w[i:] for i, ch in enumerate(w) if ch.lower() in VOWELS), w)
                       for w in norm.split())

        def user(m):
            w = m.group(0)
            v = self.user_accents.get(w.replace("+", "").lower())
            if not v:
                return w
            letters, out = iter(w.replace("+", "")), []
            for c in v:
                out.append(c if c == "+" else next(letters))
            return "".join(out)
        return norm, WORD.sub(user, acc)

    async def accent(self, request):
        text = str((await request.json()).get("text", ""))
        self.calls.append(dict(call="accent", text=text, ts=time.time()))
        norm, acc = self._accent(text)
        return web.json_response(dict(normalized=norm, accented=acc))

    async def accents(self, request):
        return web.json_response(dict(words=[dict(word=k, accented=v) for k, v in sorted(self.user_accents.items())]))

    async def accents_put(self, request):
        v = str((await request.json()).get("accented", "")).strip().lower()
        self.calls.append(dict(call="accents_put", accented=v, ts=time.time()))
        if not re.fullmatch(r"[а-яё+]+", v) or not any(c == "+" and v[i + 1:i + 2] in tuple(VOWELS)
                                                     for i, c in enumerate(v)):
            return web.Response(status=400, text="одно слово русскими буквами, ударение — «+» перед гласной")
        self.user_accents[v.replace("+", "")] = v
        return web.json_response(dict(ok=True, word=v.replace("+", ""), accented=v))

    async def accents_delete(self, request):
        word = request.match_info["word"]
        self.calls.append(dict(call="accents_delete", word=word, ts=time.time()))
        if self.user_accents.pop(word, None) is None:
            return web.Response(status=404, text="нет такого слова")
        return web.json_response(dict(ok=True))

    # --- ONNX-фронтенд и голоса из образца ---
    async def frontend_paths(self, request):
        if not self.frontend:
            return web.Response(status=404)
        return web.json_response(self.frontend)

    async def frontend_load(self, request):
        if not self.frontend:
            return web.Response(status=404)
        body = await request.json()
        with self._busy():
            self.calls.append(dict(call="frontend_load", body=body, ts=time.time()))
            if body != self.frontend:
                return web.Response(status=400, text="wrong frontend paths")
            await asyncio.sleep(0.05)
            self.frontend_loaded = True
        return web.json_response(dict(ok=True))

    async def frontend_unload(self, request):
        with self._busy():
            self.calls.append(dict(call="frontend_unload", ts=time.time()))
            self.frontend_loaded = False
        return web.json_response(dict(ok=True))

    async def speaker_extract(self, request):
        """POST /speaker?type=extract, multipart: name, text, audio (WAV)."""
        if request.query.get("type") != "extract":
            return web.Response(status=400, text="only type=extract")
        form = await request.post()
        with self._busy():
            name, text, f = form.get("name", ""), form.get("text", ""), form.get("audio")
            data = f.file.read() if hasattr(f, "file") else b""
            rec = dict(call="extract", name=name, text=text, ts=time.time(), bytes=len(data))
            if self.engine == "espeech":
                rec.update(tempo=form.get("tempo"), title=form.get("title"))
                if "+" not in text:
                    text = rec["accented"] = self._accent(text)[1]
            self.calls.append(rec)
            if not self.frontend_loaded and self.engine != "espeech":
                return web.Response(status=400, text="frontend not loaded")
            if name in self.voices:
                return web.Response(status=409, text=f"speaker {name} exists")
            try:
                with wave.open(io.BytesIO(data)) as w:
                    rec.update(sr=w.getframerate(), channels=w.getnchannels(), width=w.getsampwidth(),
                               frames=w.getnframes())
                    x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
            except (wave.Error, EOFError) as e:
                return web.Response(status=400, text=f"bad wav: {e}")
            rec["hz"] = round(dominant_hz(x)) if len(x) else 0
            await asyncio.sleep(0.05)
            self.voices[name] = (float(rec["hz"]), EXTRACTED_AMP)
        return web.json_response(dict(ok=True, name=name, text=text))

    async def speaker_save(self, request):
        body = await request.json()
        with self._busy():
            self.calls.append(dict(call="save", name=body.get("name"), path=body.get("path"), ts=time.time()))
            if self.fail_save:
                return web.Response(status=500, text="fake save failure")
            if body.get("name") not in self.voices:
                return web.Response(status=404, text="unknown speaker")
        return web.json_response(dict(ok=True))

    async def speakers(self, request):
        self.lists += 1
        return web.json_response(sorted(self.voices))

    async def speaker_delete(self, request):
        name = request.match_info["name"]
        self.calls.append(dict(call="delete", name=name, ts=time.time()))
        if self.voices.pop(name, None) is None:
            return web.Response(status=404, text="unknown speaker")
        return web.json_response(dict(ok=True))

    async def stop(self, request):
        self.stops += 1
        self._stop = True
        return web.json_response(dict(ok=True))

    def _held(self, text):
        return self.hold is True or bool(isinstance(self.hold, str) and self.hold and self.hold in text)

    async def _wait_hold(self, text):
        while self._held(text) and not self._stop:
            await asyncio.sleep(0.02)

    async def synth(self, request):
        body = await request.json()
        rec = dict(body, ts=time.time(), stopped=False, done=False)
        if self.engine == "espeech":
            rec["accented"] = self._accent(str(body.get("text", "")))[1]
        self.tts.append(rec)
        self._stop = False   # /tts/stop останавливает только текущий синтез
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            text, voice = body.get("text", ""), body.get("voice", "")
            steps = ESPEECH_PARAMS["nfe_steps"]["choices"]
            if self.engine == "espeech" and body.get("nfe_steps") not in (None, *steps):
                return web.Response(status=400, text="nfe_steps: 16 / 32 / 12")
            if voice not in self.voices:
                return web.Response(status=404, text=f"unknown voice {voice}")
            if self.fail and self.fail in text:
                return web.Response(status=500, text="fake failure")
            freq, amp = self.voices[voice]
            x = tone(freq, amp, len(text) / CHARS_PER_SEC * self.length_factor)
            await self._wait_hold(text)
            if not body.get("stream"):
                if self._stop:   # остановленный синтез отдаёт то, что успел
                    rec["stopped"] = True
                    x = x[:SR // 10]
                return web.Response(body=wav(x), content_type="audio/wav")
            resp = web.StreamResponse(headers={"Content-Type": "audio/wav"})
            await resp.prepare(request)
            await resp.write(stream_header())
            step = int(SPEECH_PIECE * SR) if self.engine == "espeech" else                 int(int(body.get("chunk_tokens") or 28) / TOKENS_PER_SEC * SR)
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
