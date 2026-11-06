"""Звук: громкость по ITU-R BS.1770 (LUFS), ограничитель пиков, перекодирование через ffmpeg."""
import asyncio, io, math, wave

import numpy as np

from . import paths
from .config import SR


def _biquad_response(b, a, n):
    """Комплексная АЧХ биквада на частотах rfft длины n."""
    z = np.exp(-1j * np.pi * np.arange(n // 2 + 1) / (n // 2))
    return (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)


def _k_weighting(sr):
    """Коэффициенты K-фильтра для произвольной частоты (как в libebur128)."""
    f0, g, q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    k = math.tan(math.pi * f0 / sr)
    vh = 10 ** (g / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    shelf = ([(vh + vb * k / q + k * k) / a0, 2 * (k * k - vh) / a0, (vh - vb * k / q + k * k) / a0],
             [1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])
    f0, q = 38.13547087602444, 0.5003270373238773
    k = math.tan(math.pi * f0 / sr)
    a0 = 1 + k / q + k * k
    highpass = ([1.0, -2.0, 1.0], [1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0])
    return shelf, highpass


def loudness(x, sr=SR):
    """Интегральная громкость моно-сигнала в LUFS (стробирование −70 LUFS и −10 LU). Тишина: -inf."""
    x = np.asarray(x, np.float64)
    block = int(0.4 * sr)
    if len(x) < block:
        x = np.pad(x, (0, block - len(x)))
    # K-фильтр через БПФ: хвост IIR затухает за доли секунды, запас 1 с
    n = 1 << int(math.ceil(math.log2(len(x) + sr)))
    spec = np.fft.rfft(x, n)
    for b, a in _k_weighting(sr):
        spec *= _biquad_response(b, a, n)
    y = np.fft.irfft(spec, n)[:len(x)]
    step = block // 4
    sq = np.concatenate(([0.0], np.cumsum(y * y)))
    starts = np.arange(0, len(y) - block + 1, step)
    power = (sq[starts + block] - sq[starts]) / block
    lk = -0.691 + 10 * np.log10(power + 1e-20)
    gated = power[lk > -70]
    if not len(gated):
        return float("-inf")
    rel = -0.691 + 10 * np.log10(gated.mean()) - 10
    gated = power[(lk > -70) & (lk > rel)]
    return float(-0.691 + 10 * np.log10(gated.mean()))


def db(gain_db):
    return 10 ** (gain_db / 20)


def limit(x, ceiling=0.89, sr=SR):
    """Ограничитель пиков с упреждением 5 мс и восстановлением ~80 мс (до −1 дБFS)."""
    hop = int(0.005 * sr)
    n = -(-len(x) // hop)
    pad = np.pad(np.abs(x), (0, n * hop - len(x)))
    peak = pad.reshape(n, hop).max(1)
    g = np.minimum(1.0, ceiling / np.maximum(peak, 1e-9))
    if g.min() >= 1.0:
        return x
    g = np.minimum(g, np.minimum(np.roll(g, -1), np.roll(g, 1)))  # упреждение на блок в обе стороны
    rel = math.exp(-1 / 16)                                         # 16 блоков по 5 мс
    for i in range(1, n):
        g[i] = min(g[i], g[i - 1] * rel + (1 - rel))
    curve = np.interp(np.arange(n * hop), np.arange(n) * hop + hop / 2, g)[:len(x)]
    return np.clip(x * curve, -1.0, 1.0)


def normalize(x, target, extra_db=0.0):
    """До target LUFS (+ ручная поправка) с ограничением пиков. Возвращает (сигнал, исходная громкость)."""
    lufs = loudness(x)
    if not math.isfinite(lufs):
        return x, lufs
    goal = target + extra_db
    y = limit(x * db(goal - lufs))
    for _ in range(2):  # ограничитель срезает часть громкости, досогласуем
        diff = goal - loudness(y)
        if abs(diff) < 0.3:
            break
        y = limit(y * db(diff))
    return y, lufs


_beep = None


def beep():
    """Писк вместо мата: 800 Гц, 0,35 с, на BEEP_BELOW_DB тише речи, с паузами по краям."""
    global _beep
    if _beep is None:
        from .config import BEEP_BELOW_DB, TARGET_LUFS
        t = np.arange(int(0.35 * SR)) / SR
        tone = np.sin(2 * np.pi * 800 * t)
        ramp = int(0.015 * SR)
        tone[:ramp] *= np.linspace(0, 1, ramp)
        tone[-ramp:] *= np.linspace(1, 0, ramp)
        tone *= db(TARGET_LUFS - BEEP_BELOW_DB - loudness(tone))
        gap = np.zeros(int(0.04 * SR))
        _beep = np.concatenate([gap, tone, gap]).astype(np.float32)
    return _beep


def soft_clip(x, knee=0.85):
    """Для потока, где упреждения нет: выше knee пики мягко сжимаются, ничего не выходит за ±1."""
    a = np.abs(x)
    over = a > knee
    if not over.any():
        return x
    y = x.copy()
    k = 1 - knee
    y[over] = np.sign(x[over]) * (knee + k * np.tanh((a[over] - knee) / k))
    return y


def fade_out(x, sec=0.04, sr=SR):
    n = min(len(x), int(sec * sr))
    if n:
        x = x.copy()
        x[-n:] *= np.linspace(1, 0, n)
    return x


def to_pcm16(x):
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def from_pcm16(b):
    return np.frombuffer(b, "<i2").astype(np.float32) / 32768


def pcm16(x):
    """Как запись в WAV PCM16 через ffmpeg и чтение обратно: округление к ближайшему, насыщение."""
    return (np.clip(np.rint(np.asarray(x, np.float32) * 32768), -32768, 32767) / 32768).astype(np.float32)


def read_wav(data):
    """WAV из ответа cosyvoice (PCM16 моно 24 кГц) в float32."""
    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2 or w.getnchannels() != 1:
            raise ValueError("ожидался PCM16 моно")
        frames = w.readframes(w.getnframes())
        sr = w.getframerate()
    x = from_pcm16(frames)
    return x if sr == SR else resample(x, sr, SR)


def resample(x, sr_from, sr_to):
    n = int(round(len(x) * sr_to / sr_from))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


async def ffmpeg(args, data=None):
    proc = await asyncio.create_subprocess_exec(
        paths.ffmpeg(), "-v", "error", "-nostdin", "-y", *args,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate(data)
    if proc.returncode:
        raise RuntimeError(f"ffmpeg: {err.decode(errors='replace')[-300:]}")
    return out


async def decode_file(path):
    """Любой звуковой файл в float32 24 кГц моно."""
    out = await ffmpeg(["-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"])
    return np.frombuffer(out, np.float32).copy()


async def write_wav(path, x):
    await ffmpeg(["-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-", "-c:a", "pcm_s16le", path],
                 x.astype(np.float32).tobytes())


async def encode_mp3(x, bitrate="64k"):
    return await ffmpeg(["-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-", "-c:a", "libmp3lame",
                         "-b:a", bitrate, "-f", "mp3", "-"], x.astype(np.float32).tobytes())
