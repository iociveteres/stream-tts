"""Эффект «житель Minecraft»: синтез речи частично заменяется звуками жителя из игры.

usage: python effects/villager-fx.py <вход.wav> <выход.wav> [--mode hum|half|mumble] [--seed N]
       python effects/villager-fx.py <вход.wav> <выход.wav> --mode cut --text "<фраза>"

Речь делится на слоги и слова по огибающей громкости: границы идут по паузам и по провалам
огибающей. Дальше в зависимости от режима:
  hum     речь остаётся, «хм» жителя добавляется в начале, в длинных паузах и в конце;
  half    треть кусков (сначала длинные) заменяется звуками жителя, речь звучит в нос;
  mumble  все куски заменяются звуками жителя в ритме речи, сама речь еле слышна под ними.
  cut     только обрезка: синтез голосом из одних звуков жителя (villager-ref.py) не умеет закончить фразу
          и бормочет до предела длины, поэтому он обрезается по длине, которую фраза заняла бы в речи
          (CHARS_PER_SEC), в ближайшем провале громкости, с затуханием; синтез не длиннее 1,8 расчётной
          длины не трогается.
Звук жителя подгоняется под длину куска изменением скорости вместе с высотой (в игре высота тоже
плавает), а по громкости — под сам кусок. Звуки берутся из voice-origs/minecraft-villager/ (ассеты
Minecraft: idle, yes, no, haggle). Нужны numpy и ffmpeg. Результат: 24 кГц моно.
"""
import argparse, glob, os, subprocess, zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # effects/ -> корень проекта
SOUNDS = os.path.join(ROOT, "voice-origs", "minecraft-villager")
SR = 24000
HOP = 240                      # 10 мс
POOL = ("idle", "yes", "no", "haggle")
SPEECH_GAIN = {"hum": 1.0, "half": 0.9, "mumble": 0.12}
CHARS_PER_SEC = 12.0           # темп речи голоса жителя (S00): 63–86 символов за 4,8–8,5 с
REPLACE = {"hum": 0.0, "half": 0.35, "mumble": 1.0}


def read(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).copy()


def write(path, x):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
                    "-c:a", "pcm_s16le", path], input=np.clip(x, -1, 1).astype(np.float32).tobytes(), check=True)


def envelope(x):
    n = len(x) // HOP
    env = np.sqrt((x[:n * HOP].reshape(n, HOP) ** 2).mean(1) + 1e-10)
    return np.convolve(env, np.ones(3) / 3, mode="same")


def chunks(x):
    """Куски речи (начало, конец) в отсчётах: слоги и слова."""
    env = envelope(x)
    loud = env > env.max() * 10 ** (-32 / 20)
    out, i = [], 0
    while i < len(env):
        if not loud[i]:
            i += 1
            continue
        j = i
        while j < len(env) and (loud[j] or loud[j:j + 6].any()):  # паузы короче 60 мс не делят
            j += 1
        out += split(env, i, j)
        i = j
    return [(a * HOP, b * HOP) for a, b in out]


def split(env, a, b, min_len=15):
    """Делит длинный кусок по провалам огибающей (граница слога), куски не короче 150 мс."""
    if b - a <= 2 * min_len:
        return [(a, b)]
    seg = env[a:b]
    k = int(np.argmin(seg[min_len:-min_len])) + min_len
    if seg[k] > 0.45 * min(seg[:k].max(), seg[k:].max()):
        return [(a, b)]
    return split(env, a, a + k, min_len) + split(env, a + k, b, min_len)


def fit(snd, length, rng):
    """Звук жителя на длину length: скорость вместе с высотой в пределах 0,75–1,4, остаток — обрезка/тишина."""
    rate = np.clip(len(snd) / length, 0.75, 1.4) * rng.uniform(0.95, 1.08)
    idx = np.arange(0, len(snd) - 1, rate)
    y = np.interp(idx, np.arange(len(snd)), snd)[:length]
    fade = min(len(y) // 4, int(0.02 * SR))
    if fade:
        y[:fade] *= np.linspace(0, 1, fade)
        y[-fade:] *= np.linspace(1, 0, fade)
    return y


def nasal(x):
    """Речь «в нос»: срез низа и верха и подъём около 1 кГц."""
    p = os.path.join(os.environ.get("TEMP", "/tmp"), f"vfx-{os.getpid()}.wav")
    write(p, x)
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-af",
                          "highpass=f=300,lowpass=f=3400,equalizer=f=1000:t=q:w=1.2:g=6",
                          "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], capture_output=True, check=True).stdout
    os.remove(p)
    return np.frombuffer(raw, np.float32).copy()


def cut(x, chars):
    """Обрезка бормотания по длине фразы: в самом тихом месте окна ±0,4 с вокруг расчётной длины."""
    target = int((chars / CHARS_PER_SEC + 0.3) * SR / HOP)
    env = envelope(x)
    if len(env) < 1.8 * target:  # уложился в длину фразы (это речь, а не бормотание до предела) — не трогать
        return x
    lo, hi = max(1, target - 40), min(len(env), target + 40)
    end = (lo + int(np.argmin(env[lo:hi]))) * HOP
    y = x[:end].copy()
    fade = min(int(0.08 * SR), len(y))
    y[-fade:] *= np.linspace(1, 0, fade)
    return y


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--mode", choices=list(REPLACE) + ["cut"], default="half")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--text", default="")
    a = ap.parse_args()

    speech = read(a.src)
    if a.mode == "cut":
        return write(a.dst, cut(speech, len(a.text)))
    seed = a.seed if a.seed is not None else zlib.crc32(speech[::97].tobytes())  # один вход — один результат
    rng = np.random.default_rng(seed)
    sounds = {k: [read(p) for p in sorted(glob.glob(os.path.join(SOUNDS, k + "*.ogg")))] for k in POOL}
    pick = lambda kinds: (lambda lst: lst[rng.integers(len(lst))])([s for k in kinds for s in sounds[k]])

    parts = chunks(speech)
    if a.mode != "hum":
        speech = nasal(speech) * 1.4
    pad = int(0.6 * SR)
    out = np.zeros(len(speech) + 2 * pad, np.float32)
    voice = speech * SPEECH_GAIN[a.mode]

    # Какие куски заменить: при half — сначала длинные (это слова), чтобы речь угадывалась по коротким
    n_rep = round(len(parts) * REPLACE[a.mode])
    order = sorted(range(len(parts)), key=lambda i: -(parts[i][1] - parts[i][0]) + rng.uniform(0, 0.08 * SR))
    replace = set(order[:n_rep])
    for i, (s, e) in enumerate(parts):
        if i in replace:
            rms = np.sqrt((speech[s:e] ** 2).mean() + 1e-10)
            y = fit(pick(POOL), e - s, rng)
            y *= rms / (np.sqrt((y ** 2).mean()) + 1e-10)
            out[pad + s:pad + s + len(y)] += y
            if a.mode == "half":
                voice[s:e] *= 0.3
    out[pad:pad + len(voice)] += voice

    # «Хм» жителя: в начале, в паузах длиннее 0,25 с и в конце
    peak = np.abs(speech).max()
    hums = [(pad - int(0.55 * SR), ("idle",))] if parts else []
    for (s0, e0), (s1, e1) in zip(parts, parts[1:]):
        if s1 - e0 > 0.25 * SR and a.mode != "mumble":
            hums.append((pad + e0 + int(0.03 * SR), ("haggle", "idle")))
    if parts:
        hums.append((pad + parts[-1][1] + int(0.05 * SR), ("yes", "no")))
    for at, kinds in hums:
        snd = pick(kinds)
        at = max(at, 0)
        y = (snd * (0.5 * peak / (np.abs(snd).max() + 1e-10)))[:len(out) - at]
        out[at:at + len(y)] += y

    nz = np.nonzero(np.abs(out) > 1e-4)[0]
    out = out[nz[0]:nz[-1] + 1] if len(nz) else out
    write(a.dst, out / max(1.0, np.abs(out).max() / 0.95))


if __name__ == "__main__":
    main()
