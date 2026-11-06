"""Эмбеддинги говорящего CAM++ локально — той же моделью, что у cosyvoice-server.

Модуль для synth.py и tools/sources/score-speaker.py, сам по себе не запускается.
Модель: models/frontend-onnx/campplus*.onnx. С серверными эмбеддингами совпадает в среднем на 0,99.
Кеш: voice-clips/embeddings.json {ключ: {stamp, emb}}, ключ — путь от корня проекта; stamp (размер и
время файла) сбрасывает запись, когда файл пересоздан, например после нового синтеза.
Нужны numpy, onnxruntime и ffmpeg в PATH.
"""
import glob, json, os, subprocess, time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # tools/<группа>/ -> корень проекта
CACHE = os.path.join(ROOT, "voice-clips", "embeddings.json")


def mel_banks(n_mels=80, n_fft=512, sr=16000, low=20.0):
    """Треугольные мел-фильтры как в kaldi (torchaudio.compliance.kaldi.fbank)."""
    mel = lambda f: 1127.0 * np.log(1.0 + f / 700.0)
    lo, hi = mel(low), mel(sr / 2)
    centers = lo + (hi - lo) / (n_mels + 1) * np.arange(n_mels + 2)
    fft_mel = mel(sr / n_fft * np.arange(n_fft // 2))
    left, center, right = centers[:-2, None], centers[1:-1, None], centers[2:, None]
    banks = np.maximum(0.0, np.minimum((fft_mel - left) / (center - left), (right - fft_mel) / (right - center)))
    return np.pad(banks, ((0, 0), (0, 1)))  # бин Найквиста с нулевым весом


BANKS = mel_banks()
WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 399)) ** 0.85  # окно Пови


def fbank(x):
    """80 лог-мел полос, кадр 25 мс, шаг 10 мс, минус среднее по времени — вход CAM++ в CosyVoice."""
    n = 1 + (len(x) - 400) // 160
    frames = x[np.arange(400)[None, :] + 160 * np.arange(n)[:, None]].astype(np.float64)
    frames -= frames.mean(1, keepdims=True)
    frames = np.concatenate([frames[:, :1] * 0.03, frames[:, 1:] - 0.97 * frames[:, :-1]], 1)
    power = np.abs(np.fft.rfft(frames * WINDOW, 512)) ** 2
    feat = np.log(np.maximum(power @ BANKS.T, np.finfo(np.float32).eps))
    return (feat - feat.mean(0)).astype(np.float32)


def save_cache(path, data):
    """Запись через временный файл. На Windows свежий файл иногда держит антивирус или индексатор
    (WinError 32 на os.replace), поэтому замена повторяется."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    for attempt in range(20):
        try:
            return os.replace(tmp, path)
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.25)


def stamp(path):
    st = os.stat(path)
    return f"{st.st_size}-{int(st.st_mtime)}"


def key_of(path):
    return os.path.relpath(os.path.abspath(path), ROOT).replace("\\", "/")


def unit(v):
    return v / np.linalg.norm(v)


class Embedder:
    """embed(путь) -> единичный вектор; cache=None — без кеша на диске."""

    def __init__(self, cache=CACHE):
        import onnxruntime
        model = sorted(glob.glob(os.path.join(ROOT, "models", "frontend-onnx", "campplus*.onnx")))[0]
        self.session = onnxruntime.InferenceSession(model, providers=["CPUExecutionProvider"])
        self.path = cache
        self.cache = json.load(open(cache, encoding="utf-8")) if cache and os.path.exists(cache) else {}
        self.dirty = False

    def __call__(self, path):
        key, st = key_of(path), stamp(path)
        hit = self.cache.get(key)
        if not hit or hit["stamp"] != st:
            # как на сервере: сначала 24 кГц (так режутся образцы), затем 16 кГц для CAM++
            pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", "24000", "-f", "wav", "-"],
                                 capture_output=True, check=True).stdout
            raw = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-ar", "16000", "-f", "f32le", "-"],
                                 input=pcm, capture_output=True, check=True).stdout
            emb = self.session.run(None, {"input": fbank(np.frombuffer(raw, np.float32))[None]})[0][0]
            self.dirty = True
            hit = self.cache[key] = dict(stamp=st, emb=[round(float(x), 5) for x in emb])
        return unit(np.array(hit["emb"]))

    def center(self, paths):
        return unit(np.mean([self(p) for p in paths], axis=0))

    def save(self):
        if self.path and self.dirty:
            save_cache(self.path, self.cache)
            self.dirty = False
