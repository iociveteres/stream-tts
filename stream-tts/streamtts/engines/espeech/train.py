"""Обучение LoRA голоса ESpeech (вкладка «Голоса» → «LoRA» у голоса, интерфейс — static/train.js; docs/stream-tts.md,
«Обучение LoRA голоса»).

Обучение идёт на espeech-server (POST /train и др., docs/espeech.md, «Обучение LoRA в сервере»): stream-tts хранит
набор реплик и присылает его серверу целиком при старте, ход, прослушивание, сохранение и сброс — запросы к нему.
Сервер набор не хранит (это отдельный процесс, набор ему уходит по HTTP), одно обучение за раз на сервер.

Набор — свой у каждого голоса, <данные>/espeech/train/<голос>/, переживает перезапуск:
- set.json — реплики [{id, text, sec, name, src?, start?, end?}] (src/start/end — откуда вырезана) и записи для
  нарезки [{id, name, dur}];
- items/<id>.wav — реплика, 24 кГц моно PCM16: так её читает сервер сам, без ffmpeg (у рантайма
  ESpeech его может не быть, у stream-tts он есть всегда — им кодируются mp3 примеров);
- sources/<id>.wav — запись, из которой реплики вырезаются на волне (как в редакторе голоса: до 15 минут);
- cache/<снимок>-<шаг>-<шагов синтеза>-<фраза>.mp3 — тестовые фразы с адаптером обучения (none — голос без LoRA).
Реплика — 0,3–30 с (как у сервера, espeech/pairs.py), текст — ровно то, что звучит; без «+» его разметит сервер
(нормализатор и RUAccent со словарём пользователя), кнопка «Ударения» показывает разметку заранее (/accent).

Результаты обучения (снимки адаптера) живут в памяти процесса сервера до сохранения в файл голоса: перезапуск
встроенного сервера (смена модели и устройства, проверка скорости всех устройств, выгрузка модели, наборы голосов,
пакет фронтенда, CUDA) их теряет. Поэтому guard(): пока обучение идёт, такие действия отказывают (остановка — одна
кнопка и сохраняет результат, а случайный клик стоил бы до 15 минут видеокарты); после конца с несохранёнными
результатами — спрашивают подтверждение. Если сервер всё-таки перезапустился (упал, закрыта программа), status()
сообщает, что результаты пропали (lost)."""
import asyncio, contextlib, glob, hashlib, io, json, logging, math, os, re, secrets, shutil, wave

import aiohttp
import numpy as np

from ... import audio, config, store
from ...config import SR
from ...i18n import tr
from ...voices import DEMO_SEED
from ..client import PREVIEW
from .client import TrainError

log = logging.getLogger("train")

MIN_SEC, MAX_SEC = 0.3, 30.0              # реплика — как у сервера (espeech/pairs.py)
MAX_ITEMS = 1000
MAX_TOTAL_SEC = 60 * 60                   # тело POST /train у сервера — до 512 МБ; час реплик в WAV — 173 МБ
MAX_SOURCES, MAX_SOURCE_SEC = 10, 15 * 60  # записи для нарезки — как в редакторе голоса (editor.py)
SOURCE_MIN = 1.0
RECOMMENDED_SEC = 60                      # хватает минуты реплик (samples/espeech-lora-small): 15–20 реплик
MAX_STEPS = 20000                         # как trainjob.MAX_STEPS
FADE = 0.01                               # с: без щелчков на краях вырезанной реплики
# Шагов по объёму — только оценка для подсказки (сервер считает сам: espeech/train.py default_steps, деление на
# проверку — pairs.split_val): 1000 на минуту обучающих реплик, округлено до 100, от 500 до 3000
STEPS_PER_MIN, STEPS_MIN, STEPS_MAX, FPS = 1000, 500, 3000, SR / 256
VAL_MIN_ITEMS, VAL_MIN_N, VAL_MAX_N = 30, 5, 12
BUSY = ("running", "stopping")
SNAPSHOTS = ("last", "best")
ID = re.compile(r"[0-9a-f]{8}")
SAFE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}", re.ASCII)


class Refused(ValueError):
    """Отказ с кодом для страницы (unsaved — предложить сбросить результаты и начать заново)."""

    def __init__(self, text, code=""):
        super().__init__(text)
        self.code = code


def estimate_steps(secs):
    """Шагов по объёму у сервера — примерно: какие реплики он отложит на проверку, решает случай."""
    frames = sorted(int(s * FPS) + 1 for s in secs)
    if len(frames) >= VAL_MIN_ITEMS:
        n = min(VAL_MAX_N, max(VAL_MIN_N, len(frames) // 10))
        drop = {round(i * len(frames) / n) for i in range(n)}
        frames = [f for i, f in enumerate(frames) if i not in drop]
    return int(min(STEPS_MAX, max(STEPS_MIN, round(STEPS_PER_MIN * sum(frames) / FPS / 60, -2))))


def unsaved(t):
    """Есть результаты, которые не записаны в голос (после ошибки снимки тоже можно сохранить)."""
    return t.get("state") in ("done", "error") and bool(t.get("snapshots")) and not t.get("saved")


def wav_bytes(x):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(audio.to_pcm16(x))
    return buf.getvalue()


def _pos(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def _hash(text):
    return hashlib.sha1(json.dumps(text, ensure_ascii=False).encode()).hexdigest()[:12]


class Training:
    def __init__(self, settings, tts, voices, editor=None):
        self.s, self.tts, self.voices, self.editor = settings, tts, voices, editor
        self.root = os.path.join(config.DATA, "espeech", "train")
        self.started = None   # голос обучения, запущенного отсюда: сервер ответил idle — результаты пропали
        self.lost = None

    # --- доступность ---
    @property
    def available(self):
        return self.tts.id == "espeech" and bool(self.tts.capabilities.get("train"))

    def require(self):
        if not self.available:
            raise ValueError(tr("обучение LoRA есть только у модели ESpeech, сервер которой его поддерживает"))

    # --- набор реплик ---
    def _dir(self, voice):
        self.require()
        if voice not in self.voices.names():
            raise ValueError(tr("нет такого голоса"))
        name = voice if SAFE.fullmatch(voice) else "v-" + hashlib.sha1(voice.encode()).hexdigest()[:12]
        return os.path.join(self.root, name)

    @staticmethod
    def _load(d):
        s = store.read_json(os.path.join(d, "set.json"), None) or {}
        return dict(dict(items=[], sources=[]), **s)

    @staticmethod
    def _save(d, s):
        os.makedirs(d, exist_ok=True)
        store.write_json(os.path.join(d, "set.json"), s)

    def public(self, voice):
        d = self._dir(voice)
        s = self._load(d)
        secs = [i["sec"] for i in s["items"]]
        return dict(voice=voice, items=s["items"], sources=s["sources"], total_sec=round(sum(secs), 1),
                    est_steps=estimate_steps(secs) if secs else None, recommended_sec=RECOMMENDED_SEC,
                    min_sec=MIN_SEC, max_sec=MAX_SEC, max_steps=MAX_STEPS)

    def item_path(self, voice, iid):
        p = os.path.join(self._dir(voice), "items", iid + ".wav")
        if not ID.fullmatch(iid) or not os.path.isfile(p):
            raise ValueError(tr("нет такой реплики"))
        return p

    def source_path(self, voice, sid):
        p = os.path.join(self._dir(voice), "sources", sid + ".wav")
        if not ID.fullmatch(sid) or not os.path.isfile(p):
            raise ValueError(tr("нет такой записи"))
        return p

    @staticmethod
    def _room(s, sec):
        if len(s["items"]) >= MAX_ITEMS:
            raise ValueError(tr("реплик в наборе не больше {n}", n=MAX_ITEMS))
        if sum(i["sec"] for i in s["items"]) + sec > MAX_TOTAL_SEC:
            raise ValueError(tr("в наборе не больше {n} минут реплик", n=MAX_TOTAL_SEC // 60))

    @staticmethod
    async def _decode(tmp, limit):
        """Любой звук или видео → float32 24 кГц моно, не длиннее limit (+1 с: чтобы узнать, что длиннее)."""
        try:
            raw = await audio.ffmpeg(["-i", tmp, "-vn", "-t", str(limit + 1), "-ac", "1", "-ar", str(SR),
                                      "-f", "f32le", "-"])
        except RuntimeError as e:
            raise ValueError(tr("не удалось прочитать звук: {e}", e=str(e).removeprefix("ffmpeg: ").strip()))
        return np.frombuffer(raw, np.float32).copy()

    @staticmethod
    def _write(path, x):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "wb") as f:
            f.write(wav_bytes(x))
        os.replace(path + ".tmp", path)

    async def add_item(self, voice, tmp, filename):
        """Файл реплики целиком; текст (пара «имя.txt») админка присылает следом — update()."""
        d = self._dir(voice)
        x = await self._decode(tmp, MAX_SEC)
        sec = len(x) / SR
        if sec < MIN_SEC:
            raise ValueError(tr("в файле нет звука или реплика короче 0,3 с"))
        if sec > MAX_SEC:
            raise ValueError(tr("реплика длиннее 30 с: добавьте файл как запись для нарезки и вырежьте реплики на волне"))
        s = self._load(d)
        self._room(s, sec)
        item = dict(id=secrets.token_hex(4), text="", sec=round(sec, 2), name=os.path.basename(filename or "audio")[:120])
        self._write(os.path.join(d, "items", item["id"] + ".wav"), x)
        s["items"].append(item)
        self._save(d, s)
        log.info("обучение %s: реплика %s, %.1f с", voice, item["name"], sec)
        return item

    async def add_source(self, voice, tmp, filename):
        d = self._dir(voice)
        if len(self._load(d)["sources"]) >= MAX_SOURCES:
            raise ValueError(tr("записей в наборе не больше {n}", n=MAX_SOURCES))
        x = await self._decode(tmp, MAX_SOURCE_SEC)
        if len(x) < SR * SOURCE_MIN:
            raise ValueError(tr("в файле нет звука или он короче {n} с", n=int(SOURCE_MIN)))
        cut = len(x) > SR * MAX_SOURCE_SEC
        x = x[:SR * MAX_SOURCE_SEC]
        src = dict(id=secrets.token_hex(4), name=os.path.basename(filename or "audio")[:120], dur=round(len(x) / SR, 2))
        self._write(os.path.join(d, "sources", src["id"] + ".wav"), x)
        s = self._load(d)
        s["sources"].append(src)
        self._save(d, s)
        log.info("обучение %s: запись %s, %.1f с%s", voice, src["name"], src["dur"], " (обрезана)" if cut else "")
        return dict(source=src, cut=cut)

    def cut(self, voice, sid, start, end, text=""):
        """Реплика — отрезок записи (копия: удаление записи набор не трогает)."""
        d = self._dir(voice)
        s = self._load(d)
        src = next((x for x in s["sources"] if x["id"] == sid), None)
        if not src:
            raise ValueError(tr("нет такой записи"))
        start, end = max(0.0, float(start)), min(float(src["dur"]), float(end))
        if not (math.isfinite(start) and math.isfinite(end)) or end - start < MIN_SEC:
            raise ValueError(tr("реплика короче 0,3 с"))
        if end - start > MAX_SEC:
            raise ValueError(tr("реплика длиннее 30 с: выделите отрезок короче"))
        self._room(s, end - start)
        with wave.open(self.source_path(voice, sid)) as w:
            a, b = int(start * SR), min(w.getnframes(), int(end * SR))
            w.setpos(a)
            x = audio.from_pcm16(w.readframes(b - a)).copy()
        n = min(len(x) // 2, int(FADE * SR))
        if n:
            x[:n] *= np.linspace(0, 1, n)
            x[-n:] *= np.linspace(1, 0, n)
        item = dict(id=secrets.token_hex(4), text=str(text or "")[:2000], sec=round(len(x) / SR, 2),
                    name=f"{src['name']} {_pos(start)}–{_pos(end)}", src=sid, start=round(start, 2), end=round(end, 2))
        self._write(os.path.join(d, "items", item["id"] + ".wav"), x)
        s["items"].append(item)
        self._save(d, s)
        return item

    def update(self, voice, items):
        """Тексты реплик: [{id, text}] — только перечисленные."""
        d = self._dir(voice)
        s = self._load(d)
        texts = {str(i["id"]): str(i.get("text", ""))[:2000] for i in items}
        for it in s["items"]:
            if it["id"] in texts:
                it["text"] = texts[it["id"]]
        self._save(d, s)

    def delete_items(self, voice, ids):
        d = self._dir(voice)
        s = self._load(d)
        ids = {str(i) for i in ids}
        s["items"] = [i for i in s["items"] if i["id"] not in ids]
        self._save(d, s)
        for i in ids:
            if ID.fullmatch(i):
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(d, "items", i + ".wav"))

    def delete_source(self, voice, sid):
        d = self._dir(voice)
        s = self._load(d)
        s["sources"] = [x for x in s["sources"] if x["id"] != sid]
        for it in s["items"]:   # реплики остаются: они уже вырезаны в items/
            if it.get("src") == sid:
                for k in ("src", "start", "end"):
                    it.pop(k, None)
        self._save(d, s)
        if ID.fullmatch(sid):
            with contextlib.suppress(OSError):
                os.remove(os.path.join(d, "sources", sid + ".wav"))

    def clear(self, voice):
        """Набор голоса целиком: реплики, записи, кэш прослушивания."""
        shutil.rmtree(self._dir(voice), ignore_errors=True)

    # --- обучение на сервере ---
    def _explain(self, e):
        """TrainError сервера → Refused с текстом на языке интерфейса (тексты сервера — по-русски, в лог)."""
        texts = {
            "busy": tr("на сервере синтеза уже идёт обучение"),
            "unsaved": tr("есть несохранённые результаты прошлого обучения: сохраните их в голос или сбросьте"),
            "unknown_voice": tr("сервер синтеза не знает этот голос"),
            "voice_not_saved": tr("голос есть только в памяти сервера: сначала сохраните его в редакторе"),
            "voice_missing": tr("файла голоса больше нет на сервере синтеза"),
            "no_snapshot": tr("нет такого результата обучения"),
            "too_large": tr("набор слишком большой для сервера синтеза"),
        }
        log.warning("обучение: сервер отказал (%s): %s", e.code, e)
        text = texts.get(e.code) or tr("сервер синтеза не принял запрос: {e}", e=str(e))
        return Refused(text, e.code)

    def _public(self, t):
        """Ход обучения для страницы: тексты ошибок и примечаний — на языке интерфейса."""
        t = dict(t)
        if t.get("error"):
            t["text"] = {
                "oom": tr("Не хватило видеопамяти: закройте игры и программы на видеокарте и запустите обучение снова")
                if t.get("checkpointing") else tr("Не хватило видеопамяти: закройте игры и программы на видеокарте и "
                                                  "запустите обучение снова — или включите экономию видеопамяти"),
                "nan": tr("Обучение разошлось (лосс — не число): запустите его снова"),
            }.get(t["error"], tr("Обучение прервано ошибкой сервера синтеза (подробности — в его логе)"))
        data = t.get("data") or {}
        if data and not data.get("val_items"):
            t["val_text"] = tr("Без проверки: для неё нужно от {n} реплик. Нет ни «лучшего» снимка, ни признака «пора "
                               "остановиться» — шаги по объёму набора и так попадают около лучшего.",
                               n=(t.get("limits") or {}).get("val_min_items", VAL_MIN_ITEMS))
        t["lost"] = self.lost
        return t

    async def status(self):
        """GET /train сервера: ход, пределы (limits), видеопамять (gpu); lost — результаты пропали вместе с сервером."""
        self.require()
        try:
            t = await self.tts.train_status()
        except TrainError as e:
            raise self._explain(e)
        if t.get("state") == "idle" and self.started:
            self.lost, self.started = self.started, None
            log.warning("обучение %s: результаты пропали — сервер синтеза перезапускался", self.lost)
        elif t.get("state") != "idle":
            self.lost = None
        return self._public(t)

    async def start(self, voice, steps=None, checkpointing=False, discard=False):
        """Набор целиком одним запросом. discard — сначала выбросить несохранённые результаты прошлого обучения."""
        d = self._dir(voice)
        items = self._load(d)["items"]
        if not items:
            raise ValueError(tr("в наборе нет реплик"))
        empty = [str(n) for n, it in enumerate(items, 1) if not it["text"].strip()]
        if empty:
            raise ValueError(tr("нет текста у реплик: {n}", n=", ".join(empty[:20]) + ("…" if len(empty) > 20 else "")))
        if steps not in (None, ""):
            steps = int(steps)
            if not 1 <= steps <= MAX_STEPS:
                raise ValueError(tr("шагов — от 1 до {n}", n=MAX_STEPS))
        else:
            steps = None
        try:
            prev = await self.tts.train_status()
            if discard:
                await self.tts.train_discard()
            form = aiohttp.FormData()
            form.add_field("voice", voice)
            form.add_field("checkpointing", "1" if checkpointing else "0")
            if steps:
                form.add_field("steps", str(steps))
            if prev.get("error") == "nan":   # разошлось — другой шум, а не тот же путь
                form.add_field("seed", str(int(prev.get("seed") or 1) + 1))
            for it in items:
                with open(os.path.join(d, "items", it["id"] + ".wav"), "rb") as f:
                    form.add_field("audio", f.read(), filename=it["id"] + ".wav", content_type="audio/wav")
                form.add_field("text", " ".join(it["text"].split()))
            # подготовка мела и проверка на шаге 0 — в рабочем потоке сервера: синтез ждёт её так же, как в очереди
            async with self.tts.lane.hold(PREVIEW, tr("обучение LoRA: подготовка")):
                t = await self.tts.train_start(form)
        except TrainError as e:
            raise self._explain(e)
        self.started, self.lost = voice, None
        self._forget_snapshots()
        log.info("обучение %s: старт, реплик %d (%.1f мин), шагов %s, экономия видеопамяти %s", voice, len(items),
                 sum(i["sec"] for i in items) / 60, steps or "по объёму", checkpointing)
        return self._public(t)

    async def _call(self, fn, *args):
        self.require()
        try:
            return await fn(*args)
        except TrainError as e:
            raise self._explain(e)

    async def stop(self):
        return self._public(await self._call(self.tts.train_stop))

    async def save(self, snapshot):
        """Снимок — в файл голоса обучения (сервер сам копирует прежний в .bak перед первым сохранением)."""
        if snapshot not in SNAPSHOTS:
            raise ValueError(tr("снимок: last или best"))
        t = await self._call(self.tts.train_status)
        r = await self._call(self.tts.train_save, snapshot)
        voice = t.get("voice")
        self.started = None           # результат в файле: перезапуск сервера больше ничего не теряет
        self.voices.forget([voice])   # пример голоса — заново, уже с адаптером
        self.voices.wake.set()
        if self.editor:
            self.editor.forget_ref(voice)
        log.info("обучение %s: снимок %s (шаг %s) сохранён в голос", voice, snapshot, r.get("step"))
        return dict({k: v for k, v in r.items() if k != "ok"}, voice=voice)   # {snapshot, step, path, backup}

    async def discard(self):
        t = await self._call(self.tts.train_discard)
        self.started = self.lost = None
        self._forget_snapshots()
        return self._public(t)

    def _forget_snapshots(self):
        """Прослушивание снимков прошлого обучения (они выброшены); голос без LoRA (none-*) остаётся."""
        for f in glob.glob(os.path.join(self.root, "*", "cache", "*.mp3")):
            if not os.path.basename(f).startswith("none-"):
                with contextlib.suppress(OSError):
                    os.remove(f)

    async def synth(self, voice, adapter, index):
        """Тестовая фраза голосом обучения: снимок last / best или без LoRA (none). → путь к mp3."""
        d = self._dir(voice)
        phrases = self.s["test_phrases"]
        if not 0 <= index < len(phrases):
            raise ValueError(tr("нет такой фразы"))
        if adapter not in ("none",) + SNAPSHOTS:
            raise ValueError(tr("снимок: last, best или none"))
        text = phrases[index]
        t = await self._call(self.tts.train_status)
        step = 0
        if adapter != "none":
            if not t.get("snapshots"):
                raise ValueError(tr("нет результата обучения"))
            if t.get("voice") != voice:
                raise ValueError(tr("результаты обучения на сервере — у голоса {voice}", voice=t.get("voice") or "—"))
            if adapter not in (t.get("snapshots") or []):
                raise ValueError(tr("нет лучшего снимка: обучение без проверки") if adapter == "best" else
                                 tr("нет результата обучения"))
            step = (t.get("best") or {}).get("step") if adapter == "best" else t.get("step")
        # «последний» во время обучения меняется с каждым шагом — не кэшируется
        live = adapter == "last" and t.get("state") in BUSY
        out = os.path.join(d, "cache", (f"live-{adapter}" if live else
                                        f"{adapter}-{step}-{self.s['espeech_steps']}-{_hash(text)}") + ".mp3")
        if not live and os.path.exists(out):
            return out
        try:
            async with self.tts.lane.hold(PREVIEW, tr("прослушивание обучения {voice}", voice=voice)):
                x = await self.tts.synth_adapter(voice, text, adapter, seed=DEMO_SEED)
        except TrainError:   # снимок пропал между запросами (сброс, новый старт)
            raise ValueError(tr("нет результата обучения"))
        y, _ = await asyncio.to_thread(audio.normalize, x, config.TARGET_LUFS)
        data = await audio.encode_mp3(y)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out + ".tmp", "wb") as f:
            f.write(data)
        os.replace(out + ".tmp", out)
        return out

    # --- перезапуски встроенного сервера ---
    async def server_state(self):
        """Кратко о обучении на сервере (GET /train) или None: модель не ESpeech, сервер не отвечает."""
        if self.tts.id != "espeech" or not self.tts.capabilities.get("train"):
            return None
        try:
            return await self.tts.train_status(timeout=3)
        except Exception:
            return None

    async def guard(self, force=False):
        """Перед действием, которое перезапускает встроенный сервер синтеза. → None (можно) или (текст, можно ли
        продолжить с подтверждением)."""
        t = await self.server_state()
        if not t:
            return None
        voice = t.get("voice")
        if t.get("state") in BUSY:
            return tr("Идёт обучение LoRA голоса {voice}: перезапуск сервера синтеза его прервёт. Остановите обучение "
                      "(результат останется), сохраните его в голос и тогда повторите.", voice=voice), False
        if unsaved(t):
            if not force:
                return tr("Результаты обучения LoRA голоса {voice} не сохранены в голос: перезапуск сервера синтеза их "
                          "выбросит. Продолжить?", voice=voice), True
            log.info("обучение %s: несохранённые результаты выбрасываются перезапуском сервера", voice)
            self.started = None   # потеря подтверждена: «результаты пропали» не показывать
        return None
