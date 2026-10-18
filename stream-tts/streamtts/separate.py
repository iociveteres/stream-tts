"""Голос без музыки для образца в редакторе: htdemucs (Demucs v4) в ONNX через onnxruntime, на процессоре.

Модель — дополнительный пакет «music» (install.PACKAGES): htdemucs_int8.onnx из HRSadeghi/demucs-onnx, граф без
STFT и ISTFT — ONNX их не выражает (ISTFT нет вовсе). Спектрограмма считается здесь на numpy так же, как
HTDemucs._spec/_ispec, а нарезка на сегменты с треугольным перекрытием и нормировка — как demucs.apply_model
(перенесено из stft.py и separator.py demucs-onnx, MIT). Вход и выход — 24 кГц моно; модель работает на 44,1 кГц
стерео, поэтому ffmpeg переводит туда и обратно, а моно подаётся в оба канала.

Замер (2026-10-18, Ryzen 5 5600G, onnxruntime 1.31): отрывок 20 с — ~8 с плюс ~3 с загрузка модели, пик памяти
~1,1 ГБ; fp16-граф совпадает с htdemucs из torch (shifts=0) до 68 дБ, int8 — до 34 дБ, на слух не отличить, при этом
fp16 на треть медленнее. Без арены памяти onnxruntime пик 0,6 ГБ, но в 1,7 раза медленнее, поэтому модель
выгружается после простоя (IDLE)."""
import asyncio, importlib.util, logging, os, time

import numpy as np

from . import audio, paths
from .config import SR

log = logging.getLogger("separate")

MODEL = "demucs/htdemucs_int8.onnx"   # относительно paths.MODELS, как файлы пакета
MSR = 44100
SEGMENT, NFFT, HOP, FRAMES = 343980, 4096, 1024, 336   # htdemucs.json из demucs-onnx
VOCALS = 3                                             # drums, bass, other, vocals
OVERLAP = 0.25                                         # как у demucs по умолчанию
IDLE = 120                                             # с: потом сессия выгружается и отдаёт ~1 ГБ


def model_path():
    return os.path.join(paths.MODELS, *MODEL.split("/"))


def runtime_ok():
    """onnxruntime есть в программе (в образе Docker его нет)."""
    return importlib.util.find_spec("onnxruntime") is not None


def available():
    return runtime_ok() and os.path.isfile(model_path())


class Spectrogram:
    """STFT/ISTFT htdemucs для сегмента SEGMENT отсчётов: окно Ханна (периодическое), нормировка 1/sqrt(NFFT),
    отражённые края, без бина Найквиста и двух крайних кадров с каждой стороны."""

    def __init__(self):
        n, hop = NFFT, HOP
        self.pad = hop // 2 * 3
        self.right = self.pad + FRAMES * hop - SEGMENT
        self.total = FRAMES + 4
        self.window = (0.5 * (1 - np.cos(2 * np.pi * np.arange(n) / n))).astype(np.float32)
        self.out_len = n + hop * (self.total - 1)
        env = np.zeros(self.out_len, np.float32)
        w2 = (self.window ** 2).reshape(n // hop, hop)
        for k in range(n // hop):
            env.reshape(-1, hop)[k:k + self.total] += w2[k]
        self.env = env
        self.offset = n // 2 + self.pad

    def forward(self, mix):
        """[C, SEGMENT] -> [2C, F, T]: каналы как пары (вещественная, мнимая часть)."""
        n, hop = NFFT, HOP
        x = np.pad(mix, [(0, 0), (self.pad, self.right)], mode="reflect")
        x = np.pad(x, [(0, 0), (n // 2, n // 2)], mode="reflect")
        start = 2 * hop
        frames = np.lib.stride_tricks.sliding_window_view(
            x[:, start:start + (FRAMES - 1) * hop + n], n, axis=-1)[:, ::hop, :]
        z = np.swapaxes(np.fft.rfft(frames * self.window, axis=-1)[..., :n // 2], -1, -2) / np.sqrt(n)
        out = np.empty((2 * len(mix), n // 2, FRAMES), np.float32)
        out[0::2], out[1::2] = z.real, z.imag
        return out

    def inverse(self, spec):
        """[2C, F, T] -> [C, SEGMENT]."""
        n, hop = NFFT, HOP
        c = len(spec) // 2
        z = np.zeros((c, n // 2 + 1, self.total), np.complex64)
        z[:, :n // 2, 2:2 + FRAMES] = spec[0::2] + 1j * spec[1::2]
        y = np.fft.irfft(z, n=n, axis=1).astype(np.float32) * np.float32(np.sqrt(n)) * self.window[None, :, None]
        out = np.zeros((c, self.out_len), np.float32)
        o3 = out.reshape(c, -1, hop)
        for k in range(n // hop):   # hop делит NFFT: сложение с перекрытием — NFFT/HOP сдвинутых сложений
            o3[:, k:k + self.total] += np.swapaxes(y[:, k * hop:(k + 1) * hop, :], 1, 2)
        sl = slice(self.offset, self.offset + SEGMENT)
        return out[:, sl] / self.env[sl]


def _vocals(session, spectro, wav):
    """[2, L] 44,1 кГц -> вокальная дорожка [2, L]. Сегменты с перекрытием OVERLAP, вес — треугольник."""
    total = wav.shape[1]
    ref = wav.mean(0)
    mean, std = float(ref.mean()), float(ref.std()) + 1e-8
    ramp = np.concatenate([np.arange(1, SEGMENT // 2 + 1), np.arange(SEGMENT - SEGMENT // 2, 0, -1)])
    weight = (ramp / ramp.max()).astype(np.float32)
    acc = np.zeros((2, total), np.float32)
    wsum = np.zeros(total, np.float32)
    for off in range(0, total, int((1 - OVERLAP) * SEGMENT)):
        length = min(total - off, SEGMENT)
        delta = SEGMENT - length          # последний сегмент короче: по центру, края — нули
        start = off - delta // 2
        lo, hi = max(0, start), min(total, start + SEGMENT)
        mix = np.zeros((2, SEGMENT), np.float32)
        mix[:, lo - start:hi - start] = (wav[:, lo:hi] - mean) / std
        spec, w = session.run(None, {"mix": mix[None], "mag": spectro.forward(mix)[None]})
        # сеть делит ответ между спектральной и волновой ветвями, дорожка — их сумма
        v = w[0, VOCALS] + spectro.inverse(spec[0, VOCALS])
        trim = delta // 2
        acc[:, off:off + length] += v[:, trim:trim + length] * weight[:length]
        wsum[off:off + length] += weight[:length]
    return acc / wsum * std + mean


class Separator:
    def __init__(self):
        self.session = None
        self.spectro = Spectrogram()
        self.lock = asyncio.Lock()   # одна обработка за раз: каждая занимает все ядра
        self.used = 0.0
        self._timer = None

    def _load(self):
        import onnxruntime as ort
        t = time.monotonic()
        self.session = ort.InferenceSession(model_path(), providers=["CPUExecutionProvider"])
        log.info("модель %s загружена за %.1f с", MODEL, time.monotonic() - t)

    async def vocals(self, x):
        """float32 24 кГц моно -> голос без музыки, той же длины."""
        if not available():
            raise RuntimeError("нет модели или onnxruntime")
        async with self.lock:
            up = await audio.ffmpeg(["-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-", "-ar", str(MSR),
                                     "-f", "f32le", "-"], x.astype(np.float32).tobytes())
            y = np.frombuffer(up, np.float32)
            t = time.monotonic()
            if self.session is None:
                await asyncio.to_thread(self._load)
            v = await asyncio.to_thread(_vocals, self.session, self.spectro, np.stack([y, y]))
            log.info("музыка убрана: %.1f с звука за %.1f с", len(x) / SR, time.monotonic() - t)
            self._idle()
            down = await audio.ffmpeg(["-f", "f32le", "-ar", str(MSR), "-ac", "1", "-i", "-", "-ar", str(SR),
                                       "-f", "f32le", "-"], v.mean(0).astype(np.float32).tobytes())
        out = np.frombuffer(down, np.float32)
        return np.pad(out, (0, max(0, len(x) - len(out))))[:len(x)].copy()

    def _idle(self):
        if self._timer:
            self._timer.cancel()
        self._timer = asyncio.get_running_loop().call_later(IDLE, self.unload)

    def unload(self):
        if self.session is not None and not self.lock.locked():
            self.session = None
            log.info("модель %s выгружена", MODEL)
