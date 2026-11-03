"""Реплики для обучения LoRA (общее у tools/espeech/train.py и POST /train сервера): звук, текст, деление на
обучающие и отложенные. Число шагов по объёму — train.default_steps.

- Звук — float32 24 кГц моно: WAV PCM16 моно 24 кГц читается сам, остальное приводит ffmpeg (если он есть).
- Длина реплики — 0,3–30 с: F5 обучали на кусках до ~30 с, а батч обучения — 3000 кадров мела (32 с), то есть
  реплика всегда в него помещается и пик видеопамяти задаёт батч, а не длина реплики (реплика 29,5 с одна в батче —
  2,99 ГБ, три по 10 с — 3,01 ГБ; длиннее батча реплика шла бы отдельным батчем с пиком больше расчётного).
- Текст — ровно то, что звучит. Без «+» он проходит тот же путь, что фраза на синтезе: нормализатор (числа,
  латиница, знаки) и RUAccent; с «+» берётся как есть.
- Отложенные реплики (проверочный лосс и признак «пора остановиться») отнимаются у обучения, а проверка по 1–2
  репликам шумная (samples/espeech-lora-small): их берётся 10 % набора, но не меньше 5 и не больше 12, и только если
  реплик не меньше 30 (тогда 5 отложенных — не больше 17 % набора; 1 минута — это 15–20 реплик, и треть их в проверке
  обеднила бы обучение). Отложенных должно набраться ≥ 20 с звука, иначе проверки нет. Без проверки обучение идёт
  шагами по объёму (train.default_steps)."""
import random, shutil, subprocess

import numpy as np

from . import voices as V

SR = 24000
MIN_SEC, MAX_SEC = 0.3, 30.0
VAL_MIN_ITEMS, VAL_MIN_SEC, VAL_MAX_ITEMS = 30, 20.0, 12
VAL_MIN_N = 5


def read_audio(data):
    """bytes или путь к файлу → float32 24 кГц моно. Не WAV PCM16 моно 24 кГц — через ffmpeg; без него — ValueError."""
    if isinstance(data, str):
        with open(data, "rb") as f:
            data = f.read()
    try:
        return V.read_wav(data)
    except Exception as e:
        if not shutil.which("ffmpeg"):
            raise ValueError(f"нужен WAV PCM16 моно {SR} Гц ({e}); другие форматы приводит ffmpeg, а его нет")
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                       input=data, capture_output=True)
    if p.returncode or not p.stdout:
        raise ValueError("не звук: " + (p.stderr.decode(errors="replace").strip().splitlines() or ["ffmpeg"])[-1])
    return np.frombuffer(p.stdout, np.float32).copy()


def check_audio(audio):
    """→ None или почему реплика не подходит."""
    sec = len(audio) / SR
    if not MIN_SEC <= sec <= MAX_SEC:
        return f"{sec:.1f} с, нужно {MIN_SEC:g}–{MAX_SEC:g} с"
    return None


def clean_text(text):
    return " ".join(str(text).split())


def prepare_text(text, accent, normalize=None):
    """Текст реплики → с ударениями: без «+» — нормализатор (normalize) и RUAccent (accent), как фраза на синтезе;
    с «+» — как есть. → "" — пустой текст."""
    text = clean_text(text)
    if text and "+" not in text:
        if normalize is not None:
            text = clean_text(normalize(text))
        text = accent(text) if text else ""
    return text


def _sec(item):
    """Секунд звука в реплике-кортеже: (имя, звук, текст) у CLI, (звук, текст) у сервера."""
    return next(len(x) for x in item if isinstance(x, np.ndarray)) / SR


def split_val(items, seed=1, n=None, sec=_sec):
    """items — кортежи со звуком → (обучающие, отложенные). n — сколько отложить (None — правило выше). Порядок
    перемешан Random(seed), как у CLI до общего модуля: с тем же --val те же отложенные."""
    items = list(items)
    random.Random(seed).shuffle(items)
    if n is None:
        if len(items) < VAL_MIN_ITEMS:
            return items, []
        n = min(VAL_MAX_ITEMS, max(VAL_MIN_N, len(items) // 10))
        while n < VAL_MAX_ITEMS and sum(sec(x) for x in items[:n]) < VAL_MIN_SEC:
            n += 1
        if sum(sec(x) for x in items[:n]) < VAL_MIN_SEC:
            return items, []
    n = max(0, min(int(n), len(items) - 1))
    return items[n:], items[:n]
