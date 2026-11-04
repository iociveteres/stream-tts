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
- fail_save — /speaker/save отвечает 500;
- save_delay — с, ответ /speaker/save задерживается (голос извлечён, но ещё не сохранён).

Обучение LoRA (FakeCosy(engine="espeech", train=True), как espeech/trainjob.py; capabilities.train): POST /train
(multipart: voice, пары audio + text, steps, checkpointing, seed; реплики — только WAV PCM16 моно 24 кГц: stream-tts
сам приводит звук, у сервера ffmpeg может не быть), GET /train (ход, limits, gpu), /train/stop, /train/save,
/train/discard, adapter в /tts (last / best / none) и training в /status. Шаги идут сами по train_rate за тик 10 мс;
лосс каждые 50 шагов, проверка каждые 100 — при ≥ 30 репликах (5–12 отложенных). Снимок узнаётся по звуку: last —
частота голоса × 1,1, best — × 1,05; сохранённый снимок звучит у голоса и без adapter. Ручки обучения:
- train_rate — шагов за тик;
- train_oom — на первом шаге без экономии видеопамяти ошибка oom (с ней — обучение идёт);
- train_overfit_at — после этого шага проверочный лосс растёт (признак «пора остановиться»);
- train_free_mb — свободная видеопамять в gpu (меньше 2800 — checkpointing_advised)."""
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
ADAPTER_HZ = {"last": 1.1, "best": 1.05}   # частота голоса со снимком обучения: тест узнаёт снимок по звуку
TRAIN_LIMITS = dict(min_sec=0.3, max_sec=30.0, recommended_min_sec=60, max_steps=20000, val_min_items=30,
                    vram_need_mb=dict(default=2800, checkpointing=500))
OOM_TEXT = "Не хватило видеопамяти: закройте игры и программы на видеокарте и запустите обучение снова"


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


ESPEECH_PARAMS = {"nfe_steps": {"type": "choice", "choices": [12, 16, 32], "default": 16, "title": "Качество синтеза",
                                 "help": "12 — быстрее, но заметно теряется качество; 16 — оптимально; "
                                         "32 — наибольшее качество, но вдвое медленнее"}}
SPEECH_PIECE = 1.0   # с звука в куске потока espeech
VOWELS = "аеёиоуыэюя"
WORD = re.compile(r"[А-Яа-яЁё+]+")


class FakeCosy:
    def __init__(self, frontend=False, engine="cosyvoice", voices=None, train=False):
        self.engine = engine
        self.can_train = train and engine == "espeech"
        self.trained = {}    # голос -> снимок, сохранённый в его файл (/train/save)
        self.train_task = None
        self._train_reset()
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
        self.save_delay = 0.0
        self.chunk_delay = 0.0
        self.user_accents = {}   # слово -> разметка: словарь ударений пользователя (espeech)
        self.train_rate = 10
        self.train_oom = False
        self.train_overfit_at = None
        self.train_free_mb = 6000
        self.trained = {}
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
    async def start(self, port=0):
        app = web.Application(client_max_size=256 << 20)
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
        if self.can_train:
            app.router.add_post("/train", self.train_start)
            app.router.add_get("/train", self.train_get)
            app.router.add_post("/train/stop", self.train_stop)
            app.router.add_post("/train/save", self.train_save)
            app.router.add_post("/train/discard", self.train_discard)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        return self

    async def close(self):
        if self.train_task:
            self.train_task.cancel()
        if self.runner:
            await self.runner.cleanup()

    # --- API ---
    async def status(self, request):
        if self.down:
            return web.Response(status=500, text="down")
        d = dict(model_loaded=True, model="fake", frontend_available=self.frontend_loaded)
        if self.engine == "espeech":
            d.update(engine="espeech", api_version=1, params=ESPEECH_PARAMS,
                     capabilities=dict(stream=True, accent=True, extract=True, train=self.can_train,
                                       voice_formats=["espeech"], user_accents=True),
                     training=dict(state=self.tr["state"], voice=self.tr["voice"], step=self.tr["step"],
                                   steps=self.tr["steps"]))
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
            await asyncio.sleep(self.save_delay)
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
            adapter = body.get("adapter") or None
            if self.can_train:
                if adapter not in (None, "none", "last", "best"):
                    return web.Response(status=400, text="adapter: last, best или none")
                if adapter in ADAPTER_HZ:
                    if adapter not in self._snapshots():
                        return web.json_response(dict(error="no_snapshot", message="нет результата обучения"),
                                                 status=409)
                    freq *= ADAPTER_HZ[adapter]
                elif adapter is None and voice in self.trained:
                    freq *= ADAPTER_HZ[self.trained[voice]]
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

    # --- обучение LoRA (espeech/trainjob.py) ---
    def _train_reset(self):
        self.tr = dict(state="idle", voice=None, step=0, steps=0, loss=None, s_per_step=None, eta_sec=None,
                       elapsed_sec=None, seed=1, checkpointing=False, data={}, val_history=[], loss_history=[],
                       best=None, overfit=False, overfit_step=None, finished=None, saved=None, memory={},
                       error=None, message=None)
        self.snaps = set()

    def _snapshots(self):
        t = self.tr
        if t["state"] in ("running", "stopping"):
            return ["last"] + (["best"] if t["best"] else [])
        return [k for k in ("last", "best") if k in self.snaps]

    def _train_status(self):
        return dict(self.tr, snapshots=self._snapshots(), limits=TRAIN_LIMITS,
                    gpu=dict(free_mb=self.train_free_mb, total_mb=8192,
                             checkpointing_advised=self.train_free_mb < TRAIN_LIMITS["vram_need_mb"]["default"]))

    @staticmethod
    def _terr(status, code, message):
        return web.json_response(dict(error=code, message=message), status=status)

    async def train_get(self, request):
        return web.json_response(self._train_status())

    async def train_start(self, request):
        t = self.tr
        if t["state"] in ("running", "stopping"):
            return self._terr(409, "busy", f"уже идёт обучение голоса {t['voice']}")
        if t["state"] == "done" and self.snaps and not t["saved"]:
            return self._terr(409, "unsaved", f"есть несохранённые результаты обучения голоса {t['voice']}")
        form = await request.post()
        voice = str(form.get("voice", "")).strip()
        audios, texts = form.getall("audio", []), [str(x) for x in form.getall("text", [])]
        rec = dict(call="train", voice=voice, ts=time.time(), items=len(audios), texts=texts, formats=[],
                   steps=form.get("steps"), checkpointing=form.get("checkpointing"), seed=form.get("seed"))
        self.calls.append(rec)
        if not voice:
            return self._terr(400, "no_voice", "voice обязателен")
        if voice not in self.voices:
            return self._terr(404, "unknown_voice", f"нет голоса {voice}")
        if not audios:
            return self._terr(400, "empty", "нет реплик")
        if len(audios) != len(texts):
            return self._terr(400, "bad_pairs", f"частей audio {len(audios)}, а text {len(texts)}")
        secs, bad = [], []
        for i, (f, text) in enumerate(zip(audios, texts), 1):
            try:
                with wave.open(io.BytesIO(f.file.read())) as w:
                    fmt = (w.getframerate(), w.getnchannels(), w.getsampwidth())
                    sec = w.getnframes() / w.getframerate()
            except (wave.Error, EOFError, AttributeError) as e:
                bad.append(f"реплика {i}: не WAV ({e})")
                continue
            rec["formats"].append(fmt)
            if fmt != (SR, 1, 2):   # у сервера без ffmpeg — только WAV PCM16 моно 24 кГц
                bad.append(f"реплика {i}: нужен WAV PCM16 моно 24000 Гц, а ffmpeg нет")
            elif not 0.3 <= sec <= 30:
                bad.append(f"реплика {i}: {sec:.1f} с, нужно 0.3–30 с")
            elif not text.strip():
                bad.append(f"реплика {i}: пустой текст")
            secs.append(sec)
        if bad:
            return self._terr(400, "bad_items", "; ".join(bad))
        n = len(secs)
        nval = min(12, max(5, n // 10)) if n >= 30 else 0
        steps = int(form["steps"]) if form.get("steps") else \
            int(min(3000, max(500, round(1000 * sum(secs[nval:]) / 60, -2))))
        ckpt = str(form.get("checkpointing", "")) in ("1", "true", "on", "yes")
        self._train_reset()
        t = self.tr
        t.update(state="running", voice=voice, steps=steps, seed=int(form.get("seed") or 1), checkpointing=ckpt,
                 elapsed_sec=0, data=dict(train_items=n - nval, train_sec=round(sum(secs[nval:]), 1), val_items=nval,
                                          val_sec=round(sum(secs[:nval]), 1),
                                          val_note=None if nval else "без проверки: нужно от 30 реплик",
                                          batches_per_epoch=1, steps_default=not form.get("steps")))
        if nval:
            self._val(0)
        self.train_task = asyncio.ensure_future(self._train_loop())
        return web.json_response(self._train_status())

    def _val(self, step):
        t, k, over = self.tr, step // 100, self.train_overfit_at
        if over is None or step <= over:
            v = 0.59 - 0.002 * k
        else:
            v = 0.59 - 0.002 * (over // 100) + 0.004 * (k - over // 100)
        t["val_history"].append(dict(step=step, val=round(v, 4)))
        if t["best"] is None or v < t["best"]["val"]:
            t["best"] = dict(step=step, val=round(v, 4))
        if t["overfit_step"] is None:   # как train.overfit: два замера подряд выше минимума на ≥ 0,003
            best, run = None, 0
            for x in t["val_history"]:
                if best is not None and x["val"] >= best + 0.003:
                    run += 1
                    if run >= 2:
                        t["overfit"], t["overfit_step"] = True, x["step"]
                        break
                else:
                    run = 0
                best = x["val"] if best is None else min(best, x["val"])

    async def _train_loop(self):
        t, t0 = self.tr, time.monotonic()
        while t["state"] == "running" and t["step"] < t["steps"]:
            await asyncio.sleep(0.01)
            if t["state"] != "running":
                return
            if self.train_oom and not t["checkpointing"]:
                self._train_end("error", error="oom", message=OOM_TEXT + " — или включите экономию видеопамяти")
                return
            for _ in range(self.train_rate):
                if t["step"] >= t["steps"]:
                    break
                t["step"] += 1
                if t["step"] % 50 == 0:
                    t["loss_history"].append(dict(step=t["step"], loss=round(0.7 - t["step"] / 100000, 4),
                                                  s_per_step=0.3, peak_mb=3100, reserved_mb=3400))
                if t["step"] % 100 == 0 and t["data"]["val_items"]:
                    self._val(t["step"])
            t.update(loss=0.65, s_per_step=0.3, elapsed_sec=round(time.monotonic() - t0),
                     eta_sec=round((t["steps"] - t["step"]) * 0.3))
        if t["state"] == "running":
            self._train_end("done", finished="steps")

    def _train_end(self, state, **kw):
        t = self.tr
        self.snaps = ({"last"} if t["step"] else set()) | ({"best"} if t["best"] else set())
        t.update(state=state, eta_sec=None, memory=dict(peak_mb=3100, reserved_mb=3400), **kw)

    async def train_stop(self, request):
        if self.tr["state"] == "running":
            self.calls.append(dict(call="train_stop", ts=time.time()))
            self._train_end("done", finished="stopped")
        return web.json_response(self._train_status())

    async def train_save(self, request):
        which = str((await request.json()).get("snapshot") or "last")
        t = self.tr
        if which not in ("last", "best"):
            return self._terr(400, "bad_snapshot", "snapshot: last или best")
        if t["state"] in ("running", "stopping"):
            return self._terr(409, "busy", "обучение ещё идёт: сначала /train/stop")
        if which not in self.snaps:
            return self._terr(409, "no_snapshot", "нет результата обучения")
        step = t["step"] if which == "last" else t["best"]["step"]
        path = f"/voices/{t['voice']}.espeech"
        backup = t["saved"]["backup"] if t["saved"] else path + ".bak"
        t["saved"] = dict(snapshot=which, step=step, path=path, backup=backup)
        self.trained[t["voice"]] = which
        self.calls.append(dict(call="train_save", voice=t["voice"], snapshot=which, step=step, ts=time.time()))
        return web.json_response(dict(ok=True, **t["saved"]))

    async def train_discard(self, request):
        if self.train_task:
            self.train_task.cancel()
        self.calls.append(dict(call="train_discard", ts=time.time()))
        self._train_reset()
        return web.json_response(self._train_status())


async def serve(argv):
    """python -m espeech --port N …: поддельный espeech-server с обучением отдельным процессом (режим managed в
    test_train.py: stream-tts запускает его вместо настоящего). Скорость шагов — FAKE_TRAIN_RATE."""
    import argparse, os
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    args, _ = ap.parse_known_args(argv)
    fake = FakeCosy(engine="espeech", train=True)
    fake.train_rate = int(os.environ.get("FAKE_TRAIN_RATE", "10"))
    await fake.start(args.port)
    print(f"fake espeech-server: {fake.url}", flush=True)
    await asyncio.Event().wait()
