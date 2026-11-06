"""Голоса ESpeech: файл `.espeech` и реестр голосов сервера.

Файл `voices/<имя>.espeech` или `voices/<набор>/<имя>.espeech` (один уровень подпапок, как у `.gguf` CosyVoice; имя
голоса — имя файла) — safetensors:
- `ref` — образец, int16, 24 кГц моно, 4–12 с (лучше 6–7: длина образца прямо определяет скорость синтеза);
- `lora.<блок>.<q|k|v|o|ff1|ff2>.<A|B>` — необязательный адаптер голоса (fp16, lora.py);
- метаданные (строки): `format` = espeech-voice, `version` = 1, `text` — ровно то, что звучит в образце, с ударениями
  «+» (иначе хвост текста «утекает» в начало синтеза), `tempo` — кадров мела на байт UTF-8 текста, посчитанный по
  полному исходному образцу (короткий кусок бывает быстрее или медленнее средней речи голоса); необязательные
  `title`, `variant`, `lora_rank`, `lora_alpha`, `lora_steps`, `lora_data_sec` (секунд реплик в обучении),
  `lora_date` (когда сохранён адаптер, POST /train/save).

Реестр перечитывает папку при каждом списке голосов (GET /speaker) и при синтезе неизвестным голосом: новые файлы
подхватываются без перезапуска. Подготовленный голос (мел образца на устройстве) кешируется до изменения файла."""
import glob, io, logging, os, threading, wave

import numpy as np
import torch
from safetensors import safe_open
from safetensors.torch import save_file

from . import model as M

log = logging.getLogger("voices")
FORMAT, VERSION = "espeech-voice", "1"
EXT = ".espeech"
MAX_REF_SEC = 12.0   # f5-tts обрезает образец длиннее 12 с
REF_PAD = 0.05       # с тишины после образца на синтезе (Voice)


def ref_text(text):
    """Текст образца как у f5-tts preprocess_ref_audio_text: в конце «. » (точка или пробел после знака)."""
    text = text.strip()
    if text.endswith("."):
        return text + " "
    return text + ". "


def tempo_of(samples, text):
    """Кадров на байт: длина образца в кадрах / байт текста образца (с «+» и «. » в конце, как у f5-tts)."""
    return (samples // M.HOP) / len(ref_text(text).encode())


def read_wav(data):
    """WAV PCM16 моно 24 кГц (bytes) → float32. Другие форматы — ошибка: ресемплинг делает stream-tts."""
    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1 or w.getframerate() != M.SR:
            raise ValueError(f"нужен WAV PCM16 моно {M.SR} Гц, а не {w.getframerate()} Гц, "
                             f"{w.getnchannels()} кан., {w.getsampwidth() * 8} бит")
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


def to_int16(x):
    """Обратное к чтению (int16 / 32768): пересохранённый образец не меняется ни в одном отсчёте."""
    return torch.from_numpy(np.clip(np.round(np.asarray(x, np.float64) * 32768), -32768, 32767).astype(np.int16))


def save(path, ref, text, tempo=None, meta=None, lora=None):
    """Записать голос. ref — float32 24 кГц, text — с ударениями, tempo — None: по самому образцу;
    lora — {"lora.…": тензор} или None."""
    md = dict(format=FORMAT, version=VERSION, text=text.strip(),
              tempo=f"{tempo if tempo else tempo_of(len(ref), text):.6f}")
    md.update({k: str(v) for k, v in (meta or {}).items() if v is not None})
    tensors = {"ref": to_int16(ref)}
    for k, v in (lora or {}).items():
        tensors[k] = v.to(torch.float16).contiguous()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    save_file(tensors, tmp, metadata=md)
    os.replace(tmp, path)


def load(path):
    """→ (ref float32, метаданные, {"lora.…": тензор fp16 на CPU} или {})."""
    with safe_open(path, "pt") as f:
        md = f.metadata() or {}
        if md.get("format") != FORMAT:
            raise ValueError(f"{path}: не голос ESpeech (format={md.get('format')!r})")
        ref = f.get_tensor("ref").numpy().astype(np.float32) / 32768
        lora = {k: f.get_tensor(k) for k in f.keys() if k.startswith("lora.")}
    if not md.get("text"):
        raise ValueError(f"{path}: нет текста образца")
    return ref, md, lora


def save_adapter(src, dst, tensors, meta):
    """Голос src с новым адаптером → dst (обучение LoRA: CLI и POST /train/save). Образец, текст, темп и прочие
    метаданные — из src (читается сейчас: правки голоса во время обучения не теряются), прежние lora_* заменяются."""
    ref, md, _ = load(src)
    extra = {k: v for k, v in md.items()
             if k not in ("format", "version", "text", "tempo") and not k.startswith("lora_")}
    extra.update(meta)
    save(dst, ref, md["text"], float(md["tempo"]), extra, tensors)


class Voice:
    """Голос, готовый к синтезу: мел образца на устройстве и всё для длительности."""

    def __init__(self, name, tts, ref, md, path=None, has_lora=False):
        self.name = name
        self.path = path                       # None — голос из образца в памяти (POST /speaker), ещё не сохранён
        self.ref = None if path else ref
        self.text = ref_text(md["text"])
        # Тишина после образца, как в f5-tts (preprocess_ref_audio_text): первый кадр синтеза — последний кадр мела
        # образца, и у образца, оборванного на звуке, каждый кусок начинался со щелчка 30–80 мс (голоса WC3).
        cond = np.r_[np.asarray(ref, np.float32), np.zeros(int(REF_PAD * M.SR), np.float32)]
        self.cond, self.rms = tts.cond(cond)
        self.ref_len = len(cond) // M.HOP
        self.rate = float(md.get("tempo") or tempo_of(len(ref), md["text"]))   # кадров на байт
        self.ref_sec = len(ref) / M.SR
        self.title = md.get("title", "")
        self.has_lora = has_lora   # адаптер читается из файла при смене голоса (~8 мс), в памяти не держится
        self.meta = md

    @property
    def bps(self):
        """Байт текста на секунду звука при темпе голоса."""
        return M.FPS / self.rate


class Registry:
    def __init__(self, tts, voices_dir):
        self.tts = tts
        self.dir = voices_dir
        self.files = {}     # имя → путь
        self.cache = {}     # имя → (mtime, Voice)
        self.memory = {}    # имя → Voice: голоса из образца (POST /speaker), ещё не сохранённые
        self.hidden = {}    # имя → mtime файла: выгружен DELETE /speaker, пока файл не изменится
        self.lock = threading.Lock()

    def scan(self):
        found = {}
        if self.dir and os.path.isdir(self.dir):
            files = sorted(glob.glob(os.path.join(self.dir, "*" + EXT))) + \
                sorted(glob.glob(os.path.join(self.dir, "*", "*" + EXT)))
            for f in files:
                name = os.path.splitext(os.path.basename(f))[0]
                if name in found:
                    continue   # как у CosyVoice: первый найденный (сначала корень папки)
                try:
                    mt = os.path.getmtime(f)
                except OSError:
                    continue
                if self.hidden.get(name) == mt:
                    continue
                self.hidden.pop(name, None)
                found[name] = f
        with self.lock:
            self.files = found
            for name in list(self.cache):
                if name not in found:
                    del self.cache[name]
        return found

    def names(self):
        self.scan()
        return sorted(set(self.files) | set(self.memory))

    def get(self, name):
        """→ Voice или KeyError."""
        if name in self.memory:
            return self.memory[name]
        path = self.files.get(name)
        if path is None or not os.path.isfile(path):
            path = self.scan().get(name)
            if path is None:
                raise KeyError(name)
        mt = os.path.getmtime(path)
        hit = self.cache.get(name)
        if hit and hit[0] == mt:
            return hit[1]
        ref, md, lora = load(path)
        v = Voice(name, self.tts, ref, md, path, bool(lora))
        self.cache[name] = (mt, v)
        log.info("голос %s: образец %.1f с, %s", name, v.ref_sec, "с LoRA" if lora else "без LoRA")
        return v

    def lora_of(self, v):
        """Адаптер голоса: (тензоры, метаданные) или (None, None)."""
        if v.has_lora:
            _, md, lora = load(v.path)
            return lora, md
        return None, None

    def add(self, name, ref, text, tempo=None, meta=None):
        """Голос из образца в памяти (до сохранения). 409 у вызывающего, если имя занято."""
        md = dict(meta or {}, text=text, tempo=str(tempo) if tempo else "")
        v = Voice(name, self.tts, ref, md)
        self.memory[name] = v
        return v

    def save(self, name, path):
        """Голос — в файл path (POST /speaker/save); голос из файла копируется вместе с адаптером."""
        v = self.memory.get(name) or self.get(name)
        if v.path:
            ref, md, lora = load(v.path)
            extra = {k: x for k, x in md.items() if k not in ("format", "version", "text", "tempo")}
            save(path, ref, md["text"], float(md["tempo"]), extra, lora)
        else:
            save(path, v.ref, v.meta["text"], v.rate, {k: v.meta.get(k) for k in ("title", "variant")})

    def delete(self, name):
        """Выгрузить голос: из памяти, а голос из файла скрыть, пока файл не изменится (как DELETE у CosyVoice:
        файл остаётся, после перезапуска голос вернётся). → был ли такой голос."""
        if self.memory.pop(name, None) is not None:
            return True
        path = self.files.get(name) or self.scan().get(name)
        if path is None:
            return False
        with self.lock:
            self.hidden[name] = os.path.getmtime(path)
            self.files.pop(name, None)
            self.cache.pop(name, None)
        return True
