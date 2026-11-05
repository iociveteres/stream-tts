"""Новый голос из записи (вкладка «Голоса» → «Добавить голос»): черновик, варианты образца, тестовый синтез.

Черновик один на программу, папка <данные>/draft/:
- sources/<id>.wav — загруженные записи после ffmpeg (24 кГц моно PCM16, не длиннее MAX_SOURCE_SEC);
- draft.json — записи, варианты образца, общие поля модели (у ESpeech — темп), голос для сравнения, имя и набор
  будущего голоса;
- cache/<ключ>-<фраза>.mp3 — тестовый синтез, выровненный по громкости, как примеры голосов (ref-<голос>-… —
  голос для сравнения);
- cache/vocals-<хеш отрезка>.wav — отрезок без музыки (separate.py), считается один раз: ~10 с на 20 с записи.
Вариант образца — отрезок записи, его расшифровка и обработка: по желанию сначала убирается музыка (htdemucs,
пакет «music»; отрезок берётся с запасом CONTEXT по краям, чтобы сети было что слушать на границах), потом
шумоподавление ffmpeg afftdn. На сервере
выбранной модели он регистрируется при первом синтезе (POST /speaker?type=extract) под служебным именем
_draft-<ключ>, где ключ — хеш параметров: изменённый вариант регистрируется заново, прежний выгружается.
Служебные голоса в список голосов не попадают (voices.refresh). Синтез — с приоритетом «послушать» и тем же seed,
что у примеров, поэтому варианты различаются только образцом.

Особенности модели — в engines/<id>/editor.py (Maker): пределы длины образца, свои поля варианта и общие для всех
вариантов поля черновика (Maker.SHARED, у ESpeech — отрезок и поправка темпа; в ключ и регистрацию идут вместе с
вариантом, _full), проверка (у CosyVoice — ONNX-фронтенд), регистрация образца (у ESpeech — ударения и темп), путь
файла голоса. У Supertonic 3 голоса из записи нет (Maker.EXTRACT): синтез вариантов и сохранение отказывают, админка
показывает вместо редактора подсказку. Черновик общий; ключ варианта и кэш сравнения с голосом у каждой модели свои
(голос с тем же именем звучит у них по-разному), ключ CosyVoice — как до второй модели.

Сохранение: образец регистрируется под настоящим именем, POST /speaker/save пишет файл голоса модели (.gguf,
.espeech) в paths.VOICES/<набор>/ (путь — Maker.target), набор — в manifest.json той же папки. Имя занято, если у выбранной модели уже есть голос с ним;
голос другой модели с тем же именем получает файл этой модели, но в том же наборе. Образец и расшифровка остаются в
<данные>/samples/<голос>.wav и .txt (у моделей, кроме CosyVoice, — в samples/<модель>/)."""
import asyncio, contextlib, glob, hashlib, io, json, logging, math, os, re, secrets, shutil, wave

import numpy as np

from . import audio, config, engines, paths, separate, store, voicesets
from .config import SR
from .engines.client import PREVIEW
from .i18n import tr
from .voices import DEMO_SEED, DRAFT_PREFIX, NAME

log = logging.getLogger("editor")

MAX_SOURCE_SEC = 15 * 60   # длиннее не декодируется: образец всё равно 10–20 с
MAX_SOURCES = 10
MAX_VARIANTS = 12
MAX_PHRASES = 8
SAMPLE_MIN = 2.0           # с: короче запись не принимается; пределы образца — у модели (Maker.SAMPLE_MIN/MAX)
DENOISE = "afftdn=nf=-25"  # шумоподавление ffmpeg: фон и шипение, музыку не убирает
FADE = 0.01                # с: без щелчков на краях отрезка
CONTEXT = 1.0              # с: запас записи по краям отрезка для отделения от музыки
ID = re.compile(r"[\w-]{1,24}")


def _hash(*parts):
    return hashlib.sha1(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:12]


def _wav_bytes(x):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(audio.to_pcm16(x))
    return buf.getvalue()


class Editor:
    def __init__(self, settings, tts, voices, runtime):
        self.s, self.tts, self.voices, self.runtime = settings, tts, voices, runtime
        self.dir = os.path.join(config.DATA, "draft")
        self.path = os.path.join(self.dir, "draft.json")
        self.separator = separate.Separator()
        self._vocals_jobs = {}  # имя в кэше -> задача: синтез всех фраз сразу не запускает отделение повторно
        self.makers = {i: E.Maker(tts) for i, E in engines.ENGINES.items()}

    @property
    def maker(self):
        """Особенности выбранной модели (engines/<id>/editor.py)."""
        return self.makers[self.tts.id]

    # --- черновик ---
    def _load(self):
        d = store.read_json(self.path, None) or {}
        d = dict(dict(sources=[], variants=[], compare="", name="", set=""), **d)
        for m in self.makers.values():
            m.upgrade(d)
        return d

    def _full(self, v, d):
        """Вариант с общими полями черновика (Maker.SHARED): ключ, проверка и регистрация видят их у варианта."""
        return dict(v, **{k: d[k] for m in self.makers.values() for k in m.SHARED if d.get(k) is not None})

    def _save(self, d):
        os.makedirs(self.dir, exist_ok=True)
        store.write_json(self.path, d)

    def _src_path(self, sid):
        return os.path.join(self.dir, "sources", sid + ".wav")

    def _cache(self, name, ext=".mp3"):
        return os.path.join(self.dir, "cache", name + ext)

    def key(self, v, maker=None):
        parts = [v["src"], round(v["start"], 2), round(v["end"], 2), " ".join(v["text"].split()), v["denoise"]]
        if v.get("music"):  # без галочки ключ прежний: синтез черновиков до неё не пропадает
            parts.append("music")
        return _hash(*parts, *(maker or self.maker).key_parts(v))

    @staticmethod
    def vocals_name(v):
        return "vocals-" + _hash(v["src"], round(v["start"], 2), round(v["end"], 2))

    def public(self):
        d = self._load()
        phrases = [_hash(p) for p in self.s["test_phrases"]]
        ready = lambda prefix: [i for i, h in enumerate(phrases) if os.path.exists(self._cache(f"{prefix}-{h}"))]
        for v in d["variants"]:
            v["ready"] = ready(self.key(self._full(v, d)))
        m = self.maker
        d["compare_ready"] = ready(m.ref_name(d["compare"])) if d["compare"] else []
        d["frontend"] = self.tts.frontend_ready
        d["music"] = separate.available()
        d["sets"] = self.sets()
        d["engine"] = dict(id=m.ENGINE, title=self.tts.title, min=m.SAMPLE_MIN, max=m.SAMPLE_MAX, ok=list(m.SAMPLE_OK),
                           packages=list(m.PACKAGES), accent=bool(self.tts.capabilities.get("accent")),
                           extract=m.EXTRACT)
        return d

    def sets(self):
        names = set(self.voices._read_manifests()[2].values()) | {s["name"] for s in voicesets.list_sets()}
        return sorted(n for n in names if n)

    def update(self, data):
        """Варианты, голос для сравнения, имя и набор: админка присылает их целиком при каждом изменении."""
        d = self._load()
        srcs = {s["id"]: s for s in d["sources"]}
        variants = []
        for v in data.get("variants", d["variants"])[:MAX_VARIANTS]:
            vid, src = str(v["id"]), str(v["src"])
            if not ID.fullmatch(vid) or src not in srcs:
                raise ValueError(tr("неверный вариант"))
            start = max(0.0, float(v["start"]))
            end = min(float(srcs[src]["dur"]), float(v["end"]))
            if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
                raise ValueError(tr("неверный отрезок"))
            out = dict(id=vid, src=src, start=round(start, 2), end=round(end, 2), text=str(v.get("text", ""))[:1000],
                       denoise=bool(v.get("denoise")), music=bool(v.get("music")))
            for m in self.makers.values():   # поля всех моделей: при смене модели они не теряются
                out.update(m.fields(v, srcs[src]["dur"]))
            variants.append(out)
        d["variants"] = variants
        for m in self.makers.values():
            for k, x in m.shared(data, srcs).items():
                if x is None:
                    d.pop(k, None)
                else:
                    d[k] = x
        if "compare" in data:
            c = str(data["compare"] or "")
            d["compare"] = c if c in self.voices.names() else ""
        for k in ("name", "set"):
            if k in data:
                d[k] = str(data[k] or "")[:64]
        self._save(d)
        self._prune(d)
        return self.public()

    def _prune(self, d):
        """Синтез и дорожки без музыки вариантов, которых больше нет (сравнение с голосами остаётся: он не
        устаревает)."""
        keep = {self.key(self._full(v, d), m) for v in d["variants"] for m in self.makers.values()}
        vocals = {self.vocals_name(v) + ".wav" for v in d["variants"] if v.get("music")}
        for f in glob.glob(os.path.join(self.dir, "cache", "*.*")):
            name = os.path.basename(f)
            if name.startswith("vocals-"):
                stale = name not in vocals
            else:
                stale = name.endswith(".mp3") and not name.startswith("ref-") and name.split("-")[0] not in keep
            if stale:
                with contextlib.suppress(OSError):
                    os.remove(f)

    def forget_ref(self, voice):
        """Сравнение с голосом voice — заново: его файл изменился (обучение LoRA сохранило адаптер)."""
        for m in self.makers.values():
            for f in glob.glob(os.path.join(self.dir, "cache", glob.escape(m.ref_name(voice)) + "-*.mp3")):
                with contextlib.suppress(OSError):
                    os.remove(f)

    async def add_source(self, tmp, filename):
        d = self._load()
        if len(d["sources"]) >= MAX_SOURCES:
            raise ValueError(tr("записей в черновике не больше {n}", n=MAX_SOURCES))
        # -vn: из видео только звук; -t: часовой стрим не декодируется целиком
        try:
            raw = await audio.ffmpeg(["-i", tmp, "-vn", "-t", str(MAX_SOURCE_SEC + 1), "-ac", "1", "-ar", str(SR),
                                      "-f", "f32le", "-"])
        except RuntimeError as e:
            raise ValueError(tr("не удалось прочитать звук: {e}", e=str(e).removeprefix("ffmpeg: ").strip()))
        x = np.frombuffer(raw, np.float32)
        if len(x) < SR * SAMPLE_MIN:
            raise ValueError(tr("в файле нет звука или он короче {n} с", n=int(SAMPLE_MIN)))
        cut = len(x) > SR * MAX_SOURCE_SEC
        x = x[:SR * MAX_SOURCE_SEC]
        sid = secrets.token_hex(4)
        os.makedirs(os.path.dirname(self._src_path(sid)), exist_ok=True)
        with open(self._src_path(sid), "wb") as f:
            f.write(_wav_bytes(x))
        src = dict(id=sid, name=os.path.basename(filename or "audio")[:120], dur=round(len(x) / SR, 2))
        d["sources"].append(src)
        self._save(d)
        log.info("черновик: запись %s, %.1f с%s", src["name"], src["dur"], " (обрезана)" if cut else "")
        return dict(source=src, cut=cut)

    def delete_source(self, sid):
        d = self._load()
        d["sources"] = [s for s in d["sources"] if s["id"] != sid]
        d["variants"] = [v for v in d["variants"] if v["src"] != sid]
        for m in self.makers.values():   # общие поля с отрезком этой записи (темп ESpeech)
            for k in m.SHARED:
                if isinstance(d.get(k), dict) and d[k].get("src") == sid:
                    del d[k]
        self._save(d)
        self._prune(d)
        with contextlib.suppress(OSError):
            os.remove(self._src_path(sid))

    def source_path(self, sid):
        if not ID.fullmatch(sid) or not os.path.isfile(self._src_path(sid)):
            raise ValueError(tr("нет такой записи"))
        return self._src_path(sid)

    def _variant(self, vid):
        """Вариант с общими полями черновика."""
        d = self._load()
        v = next((v for v in d["variants"] if v["id"] == vid), None)
        if not v:
            raise ValueError(tr("нет такого варианта"))
        return self._full(v, d)

    def _cut(self, sid, start, end):
        with wave.open(self.source_path(sid)) as w:
            a, b = int(start * SR), min(w.getnframes(), int(end * SR))
            w.setpos(a)
            return audio.from_pcm16(w.readframes(b - a)).copy()

    async def _vocals(self, v):
        """Отрезок варианта без музыки: из кэша или через separate.py (одна задача на отрезок)."""
        name = self.vocals_name(v)
        path = self._cache(name, ".wav")
        if os.path.exists(path):
            with wave.open(path) as w:
                return audio.from_pcm16(w.readframes(w.getnframes())).copy()
        job = self._vocals_jobs.get(name)
        if not job:
            job = self._vocals_jobs[name] = asyncio.ensure_future(self._separate(v, path))
            job.add_done_callback(lambda _: self._vocals_jobs.pop(name, None))
        return (await asyncio.shield(job)).copy()

    async def _separate(self, v, path):
        a = max(0.0, v["start"] - CONTEXT)
        x = await self.separator.vocals(self._cut(v["src"], a, v["end"] + CONTEXT))
        n = len(self._cut(v["src"], v["start"], v["end"]))
        off = int(v["start"] * SR) - int(a * SR)
        y = x[off:off + n]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(_wav_bytes(y))
        os.replace(path + ".tmp", path)
        return y

    async def sample(self, v):
        """Образец варианта: отрезок записи, по желанию без музыки, края с затуханием, по желанию шумоподавление.
        float32 24 кГц."""
        if v.get("music"):
            if not separate.available():
                raise ValueError(tr("для «убрать музыку» скачайте пакет «Очистка образца от музыки»"))
            x = await self._vocals(v)
        else:
            x = self._cut(v["src"], v["start"], v["end"])
        n = min(len(x) // 2, int(FADE * SR))
        if n:
            x[:n] *= np.linspace(0, 1, n)
            x[-n:] *= np.linspace(1, 0, n)
        if v["denoise"]:
            out = await audio.ffmpeg(["-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-", "-af", DENOISE,
                                      "-f", "f32le", "-"], x.tobytes())
            x = np.frombuffer(out, np.float32).copy()
        return x

    async def sample_wav(self, vid):
        return _wav_bytes(await self.sample(self._variant(vid)))

    def _check(self, v):
        m, dur = self.maker, v["end"] - v["start"]
        if dur < m.SAMPLE_MIN or dur > m.SAMPLE_MAX:
            raise ValueError(tr("образец {dur} с: нужно от {min} до {max} с", dur=round(dur, 1),
                                min=int(m.SAMPLE_MIN), max=int(m.SAMPLE_MAX)))
        if not v["text"].strip():
            raise ValueError(tr("нет расшифровки образца"))
        m.check(v)

    async def unload_frontend(self, priority=PREVIEW):
        """ONNX-фронтенд CosyVoice: по таймеру простоя и кнопкой на вкладке «Статистика»."""
        await self.makers["cosyvoice"].unload_frontend(priority)

    async def accent(self, text):
        """Расшифровка образца так, как её прочитает модель (кнопка «Ударения»): {normalized, accented}."""
        if not self.tts.capabilities.get("accent"):
            raise ValueError(tr("модель {engine} не показывает ударения", engine=self.tts.title))
        text = " ".join(str(text).split())[:3000]
        if not text:
            raise ValueError(tr("нет расшифровки образца"))
        return await self.tts.accent(text)

    # --- тестовый синтез ---
    async def synth(self, vid, voice, index):
        """Тестовая фраза index вариантом vid или готовым голосом voice (для сравнения). Путь к mp3."""
        phrases = self.s["test_phrases"]
        if not 0 <= index < len(phrases):
            raise ValueError(tr("нет такой фразы"))
        text = phrases[index]
        if vid:
            v = self._variant(vid)
            out = self._cache(f"{self.key(v)}-{_hash(text)}")
            if os.path.exists(out):
                return out
            self._check(v)
            wav = _wav_bytes(await self.sample(v))
            name = DRAFT_PREFIX + self.key(v)
            async with self.tts.lane.hold(PREVIEW, tr("черновик голоса")):
                speakers = await self.tts.speakers()
                if name not in speakers:
                    # Прежние регистрации изменённых и удалённых вариантов — по списку сервера, а не по памяти:
                    # после перезапуска stream-tts они иначе оставались на сервере до «Удалить черновик»
                    d = self._load()
                    keep = {DRAFT_PREFIX + self.key(self._full(x, d)) for x in d["variants"]}
                    for old in speakers:
                        if old.startswith(DRAFT_PREFIX) and old not in keep:
                            await self.tts.delete_speaker(old)
                    await self.maker.register(name, v, " ".join(v["text"].split()), wav, self)
                    log.info("черновик: вариант %s зарегистрирован как %s", vid, name)
                x = await self.tts.synth(name, text, seed=DEMO_SEED)
        else:
            if voice not in self.voices.names():
                raise ValueError(tr("нет такого голоса"))
            out = self._cache(f"{self.maker.ref_name(voice)}-{_hash(text)}")
            if os.path.exists(out):
                return out
            async with self.tts.lane.hold(PREVIEW, tr("пример {voice}", voice=voice)):
                x = await self.tts.synth(voice, text, seed=DEMO_SEED)
            if self.voices.post(voice):
                x = await self.voices.apply_post(voice, x, text)
        y, _ = await asyncio.to_thread(audio.normalize, x, config.TARGET_LUFS)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(await audio.encode_mp3(y))
        return out

    # --- сохранение и удаление ---
    @staticmethod
    def _files(ext):
        """Имена голосов с файлом ext в папке голосов (корень и наборы)."""
        return {os.path.splitext(os.path.basename(f))[0] for f in glob.glob(os.path.join(paths.VOICES, "*" + ext)) +
                glob.glob(os.path.join(paths.VOICES, "*", "*" + ext))}

    def _set_of(self, name):
        """Где уже лежит голос name любой модели: набор, "" — корень папки голосов, None — нигде."""
        for ext in voicesets.VOICE_EXTS:
            if os.path.isfile(os.path.join(paths.VOICES, name + ext)):
                return ""
            hit = glob.glob(os.path.join(paths.VOICES, "*", name + ext))
            if hit:
                return os.path.basename(os.path.dirname(hit[0]))
        return self.voices._read_manifests()[2].get(name) or None

    async def save(self, vid, name, set_name):
        v = self._variant(vid)
        self._check(v)
        if not NAME.fullmatch(name):
            raise ValueError(tr("имя голоса: латиница, цифры, «-», «_» и «.», до 64 символов"))
        set_name = voicesets.SET_NAME.sub("-", set_name.lower()).strip("-_")
        if not set_name:
            raise ValueError(tr("укажите набор: латиница, цифры, «-» и «_»"))
        m = self.maker
        taken = {n.lower() for n in self.voices.names()} | {n.lower() for n in self._files(m.EXT)}
        if name.lower() in taken:
            raise ValueError(tr("голос {voice} уже есть: выберите другое имя", voice=name))
        other = self._set_of(name)   # голос другой модели: файл этой модели — в тот же набор
        if other is not None and other != set_name:
            raise ValueError(tr("голос {voice} другой модели лежит в наборе {set}: сохраните в тот же набор",
                                voice=name, set=other or tr("голоса без набора")))
        x = await self.sample(v)
        text = " ".join(v["text"].split())
        folder = os.path.join(paths.VOICES, set_name)
        manifest = os.path.join(folder, "manifest.json")
        os.makedirs(folder, exist_ok=True)
        target = m.target(name, set_name, folder)
        async with self.tts.lane.hold(PREVIEW, tr("сохранение голоса {voice}", voice=name)):
            if name in await self.tts.speakers():
                raise ValueError(tr("голос {voice} уже есть: выберите другое имя", voice=name))
            async with self.voices.listing:   # голос на сервере, но ещё не в файле: в список не попадает
                text = await m.register(name, v, text, _wav_bytes(x), self)
                try:
                    await self.tts.save_speaker(name, target)
                except Exception:
                    await self.tts.delete_speaker(name)
                    raise
        meta = store.read_json(manifest, {})
        meta[name] = dict(meta.get(name) or {}, source="editor")
        store.write_json(manifest, meta)
        samples = os.path.join(config.DATA, "samples", *([] if m.ENGINE == "cosyvoice" else [m.ENGINE]))
        os.makedirs(samples, exist_ok=True)
        with open(os.path.join(samples, name + ".wav"), "wb") as f:
            f.write(_wav_bytes(x))
        with open(os.path.join(samples, name + ".txt"), "w", encoding="utf-8") as f:
            f.write(text + "\n")
        log.info("голос %s сохранён: %s (набор %s, %.1f с образца)", name, target, set_name, len(x) / SR)
        await self.voices.refresh()
        return dict(name=name, set=set_name, path=target)

    async def discard(self):
        """Черновик целиком: файлы и служебные голоса на сервере."""
        shutil.rmtree(self.dir, ignore_errors=True)
        if self.tts.ok:
            async with self.tts.lane.hold(PREVIEW, tr("черновик голоса")):
                for n in await self.tts.speakers():
                    if n.startswith(DRAFT_PREFIX):
                        await self.tts.delete_speaker(n)
