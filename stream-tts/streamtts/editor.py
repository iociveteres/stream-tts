"""Новый голос из записи (вкладка «Голоса» → «Добавить голос»): черновик, варианты образца, тестовый синтез.

Черновик один на программу, папка <данные>/draft/:
- sources/<id>.wav — загруженные записи после ffmpeg (24 кГц моно PCM16, не длиннее MAX_SOURCE_SEC);
- draft.json — записи, варианты образца, голос для сравнения, имя и набор будущего голоса;
- cache/<ключ>-<фраза>.mp3 — тестовый синтез, выровненный по громкости, как примеры голосов (ref-<голос>-… —
  голос для сравнения).
Вариант образца — отрезок записи, его расшифровка и обработка (шумоподавление ffmpeg afftdn). На cosyvoice-server
он регистрируется при первом синтезе (POST /speaker?type=extract, нужен ONNX-фронтенд) под служебным именем
_draft-<ключ>, где ключ — хеш параметров: изменённый вариант регистрируется заново, прежний выгружается.
Служебные голоса в список голосов не попадают (voices.refresh). Синтез — с приоритетом «послушать» и тем же seed,
что у примеров, поэтому варианты различаются только образцом.

Сохранение: образец регистрируется под настоящим именем, POST /speaker/save пишет .gguf. Со встроенным сервером —
в paths.VOICES/<набор>/ с manifest.json набора; с внешним (Docker) — в COSYVOICE_VOICES (по умолчанию /voices) без
подпапки (cosyvoice/entrypoint.sh склеивает имя голоса из подпапок), а набор записывается в
<данные>/sets/<набор>/manifest.json. Образец и расшифровка остаются в <данные>/samples/<голос>.wav и .txt."""
import asyncio, contextlib, glob, hashlib, io, json, logging, math, os, re, secrets, shutil, wave

import numpy as np

from . import audio, config, install, paths, store
from .config import SR
from .cosy import PREVIEW
from .i18n import tr
from .voices import DEMO_SEED, DRAFT_PREFIX

log = logging.getLogger("editor")

MAX_SOURCE_SEC = 15 * 60   # длиннее не декодируется: образец всё равно 10–20 с
MAX_SOURCES = 10
MAX_VARIANTS = 12
MAX_PHRASES = 8
SAMPLE_MIN, SAMPLE_MAX = 2.0, 30.0   # с; советуем 10–20 (docs/voice-pipeline.md), длиннее синтез медленнее
DENOISE = "afftdn=nf=-25"            # шумоподавление ffmpeg: фон и шипение, музыку не убирает
FADE = 0.01                          # с: без щелчков на краях отрезка
ID = re.compile(r"[\w-]{1,24}")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", re.ASCII)


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
    def __init__(self, settings, cosy, voices, runtime):
        self.s, self.cosy, self.voices, self.runtime = settings, cosy, voices, runtime
        self.dir = os.path.join(config.DATA, "draft")
        self.path = os.path.join(self.dir, "draft.json")
        self.registered = {}   # id варианта -> служебное имя на сервере (прежнее выгружается при смене)

    # --- черновик ---
    def _load(self):
        d = store.read_json(self.path, None) or {}
        return dict(dict(sources=[], variants=[], compare="", name="", set=""), **d)

    def _save(self, d):
        os.makedirs(self.dir, exist_ok=True)
        store.write_json(self.path, d)

    def _src_path(self, sid):
        return os.path.join(self.dir, "sources", sid + ".wav")

    def _cache(self, name):
        return os.path.join(self.dir, "cache", name + ".mp3")

    @staticmethod
    def key(v):
        return _hash(v["src"], round(v["start"], 2), round(v["end"], 2), " ".join(v["text"].split()), v["denoise"])

    def public(self):
        d = self._load()
        phrases = [_hash(p) for p in self.s["test_phrases"]]
        ready = lambda prefix: [i for i, h in enumerate(phrases) if os.path.exists(self._cache(f"{prefix}-{h}"))]
        for v in d["variants"]:
            v["ready"] = ready(self.key(v))
        d["compare_ready"] = ready("ref-" + d["compare"]) if d["compare"] else []
        d["frontend"] = bool(self.cosy.info.get("frontend_available"))
        d["sets"] = self.sets()
        d["managed"] = self.runtime is not None
        return d

    def sets(self):
        names = set(self.voices._read_manifests()[2].values()) | {s["name"] for s in install.list_sets()}
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
            variants.append(dict(id=vid, src=src, start=round(start, 2), end=round(end, 2),
                                 text=str(v.get("text", ""))[:1000], denoise=bool(v.get("denoise"))))
        d["variants"] = variants
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
        """Синтез вариантов, которых больше нет (сравнение с голосами остаётся: он не устаревает)."""
        keep = {self.key(v) for v in d["variants"]}
        for f in glob.glob(os.path.join(self.dir, "cache", "*.mp3")):
            name = os.path.basename(f)
            if not name.startswith("ref-") and name.split("-")[0] not in keep:
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
        self._save(d)
        self._prune(d)
        with contextlib.suppress(OSError):
            os.remove(self._src_path(sid))

    def source_path(self, sid):
        if not ID.fullmatch(sid) or not os.path.isfile(self._src_path(sid)):
            raise ValueError(tr("нет такой записи"))
        return self._src_path(sid)

    def _variant(self, vid):
        v = next((v for v in self._load()["variants"] if v["id"] == vid), None)
        if not v:
            raise ValueError(tr("нет такого варианта"))
        return v

    async def sample(self, v):
        """Образец варианта: отрезок записи, края с затуханием, по желанию шумоподавление. float32 24 кГц."""
        with wave.open(self.source_path(v["src"])) as w:
            a, b = int(v["start"] * SR), min(w.getnframes(), int(v["end"] * SR))
            w.setpos(a)
            x = audio.from_pcm16(w.readframes(b - a)).copy()
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
        dur = v["end"] - v["start"]
        if dur < SAMPLE_MIN or dur > SAMPLE_MAX:
            raise ValueError(tr("образец {dur} с: нужно от {min} до {max} с", dur=round(dur, 1),
                                min=int(SAMPLE_MIN), max=int(SAMPLE_MAX)))
        if not v["text"].strip():
            raise ValueError(tr("нет расшифровки образца"))
        if not self.cosy.info.get("frontend_available"):
            raise ValueError(tr("на сервере синтеза нет фронтенда для создания голосов: скачайте пакет"))

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
            async with self.cosy.lane.hold(PREVIEW, tr("черновик голоса")):
                if name not in await self.cosy.speakers():
                    old = self.registered.get(vid)
                    if old and old != name:
                        await self.cosy.delete_speaker(old)
                    await self.cosy.extract(name, " ".join(v["text"].split()), wav)
                    log.info("черновик: вариант %s зарегистрирован как %s", vid, name)
                self.registered[vid] = name
                x = await self.cosy.synth(name, text, self.s["token_ratio"], seed=DEMO_SEED)
        else:
            if voice not in self.voices.names():
                raise ValueError(tr("нет такого голоса"))
            out = self._cache(f"ref-{voice}-{_hash(text)}")
            if os.path.exists(out):
                return out
            async with self.cosy.lane.hold(PREVIEW, tr("пример {voice}", voice=voice)):
                x = await self.cosy.synth(voice, text, self.s["token_ratio"], seed=DEMO_SEED)
            if self.voices.post(voice):
                x = await self.voices.apply_post(voice, x, text)
        y, _ = await asyncio.to_thread(audio.normalize, x, config.TARGET_LUFS)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(await audio.encode_mp3(y))
        return out

    # --- сохранение и удаление ---
    async def save(self, vid, name, set_name):
        v = self._variant(vid)
        self._check(v)
        if not NAME.fullmatch(name):
            raise ValueError(tr("имя голоса: латиница, цифры, «-», «_» и «.», до 64 символов"))
        set_name = install.SET_NAME.sub("-", set_name.lower()).strip("-_")
        if not set_name:
            raise ValueError(tr("укажите набор: латиница, цифры, «-» и «_»"))
        taken = {n.lower() for n in self.voices.names()} | {n.lower() for n in install._existing_voices(None)}
        if name.lower() in taken:
            raise ValueError(tr("голос {voice} уже есть: выберите другое имя", voice=name))
        x = await self.sample(v)
        text = " ".join(v["text"].split())
        if self.runtime:
            folder = os.path.join(paths.VOICES, set_name)
            target = os.path.join(folder, name + ".gguf")
            manifest = os.path.join(folder, "manifest.json")
        else:
            folder = None
            target = paths.env("COSYVOICE_VOICES", "/voices").rstrip("/") + f"/{name}.gguf"
            manifest = os.path.join(config.DATA, "sets", set_name, "manifest.json")
        if folder:
            os.makedirs(folder, exist_ok=True)
        async with self.cosy.lane.hold(PREVIEW, tr("сохранение голоса {voice}", voice=name)):
            if name in await self.cosy.speakers():
                raise ValueError(tr("голос {voice} уже есть: выберите другое имя", voice=name))
            await self.cosy.extract(name, text, _wav_bytes(x))
            try:
                await self.cosy.save_speaker(name, target)
            except Exception:
                await self.cosy.delete_speaker(name)
                raise
        os.makedirs(os.path.dirname(manifest), exist_ok=True)
        meta = store.read_json(manifest, {})
        meta[name] = dict(meta.get(name) or {}, source="editor")
        store.write_json(manifest, meta)
        samples = os.path.join(config.DATA, "samples")
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
        self.registered.clear()
        shutil.rmtree(self.dir, ignore_errors=True)
        if self.cosy.ok:
            async with self.cosy.lane.hold(PREVIEW, tr("черновик голоса")):
                for n in await self.cosy.speakers():
                    if n.startswith(DRAFT_PREFIX):
                        await self.cosy.delete_speaker(n)
