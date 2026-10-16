"""Образец голоса жителя Minecraft для zero-shot: звуки жителя один за другим, как поток речи.

usage: python tools/meme/villager-ref.py <выход.wav> [--len 10] [--gap 0.04-0.09] [--phrase 5] [--pitch 0.92-1.1]
                              [--text hm|syl] [--seed 1]

Берутся idle, yes, no и haggle из voice-origs/minecraft-villager/ (ассеты Minecraft). У каждого звука
отрезается медленное нарастание (начало — с уровня ATTACK_DB от пика, фейд 5 мс) и затухающий хвост,
так что атака короткая и звуки идут плотно. Высота и скорость каждого звука немного меняются (как в игре),
порядок случайный без повторов подряд. Паузы между звуками — --gap, после каждых --phrase звуков
пауза чуть длиннее, как между фразами.

Рядом пишется <выход>.txt — «текст» образца для import-voices.sh / meme-pick.py:
  hm   один слог-междометие на звук («Хм», «Хрм», «Мх-м»), фразы через точку;
  syl  два-три коротких слога на звук («хму-хм», «хрн-хм») — ближе к числу слогов в речи.
Нужен numpy и ffmpeg.
"""
import argparse, glob, os, re, subprocess

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # tools/<группа>/ -> корень проекта
SOUNDS = os.path.join(ROOT, "voice-origs", "minecraft-villager")
SR = 24000
HOP = 120                  # 5 мс
ATTACK_DB = -15
TAIL_DB = -30
WORDS = {
    "hm": {"idle": ["Хм-м", "Мх-м"], "yes": ["Хм", "Ха-м"], "no": ["Хм-хм", "Мм-хм"], "haggle": ["Хрм", "Хр-рм"]},
    "syl": {"idle": ["хму-хм", "мхы-м"], "yes": ["хм-ха", "хам-м"], "no": ["хым-хм", "мн-хм"], "haggle": ["хрн-хм", "хр-рм"]},
}


def read(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).copy()


def write(path, x):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", "-",
                    "-c:a", "pcm_s16le", path], input=np.clip(x, -1, 1).astype(np.float32).tobytes(), check=True)


def tight(x):
    """Звук без медленного нарастания и хвоста: начало с ATTACK_DB, конец на TAIL_DB, короткие фейды."""
    n = len(x) // HOP
    env = np.sqrt((x[:n * HOP].reshape(n, HOP) ** 2).mean(1) + 1e-12)
    db = 20 * np.log10(env / env.max())
    a = max(0, int(np.argmax(db > ATTACK_DB)) - 1) * HOP
    b = (n - int(np.argmax(db[::-1] > TAIL_DB))) * HOP
    y = x[a:b].copy()
    fi, fo = int(0.005 * SR), min(int(0.015 * SR), len(y) // 3)
    y[:fi] *= np.linspace(0, 1, fi)
    y[-fo:] *= np.linspace(1, 0, fo)
    return y / np.abs(y).max()


def span(s):
    lo, hi = map(float, s.split("-"))
    return lo, hi


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dst")
    ap.add_argument("--len", type=float, default=10.0)
    ap.add_argument("--gap", default="0.04-0.09")
    ap.add_argument("--phrase", type=int, default=5)
    ap.add_argument("--pitch", default="0.92-1.1")
    ap.add_argument("--text", choices=list(WORDS), default="hm")
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    rng = np.random.default_rng(a.seed)
    gap, pitch = span(a.gap), span(a.pitch)

    pool = [(re.sub(r"\d*\.ogg$", "", os.path.basename(p)), tight(read(p)))
            for p in sorted(glob.glob(os.path.join(SOUNDS, "*.ogg")))]
    pool = [(k, x) for k, x in pool if k in WORDS[a.text]]
    out, words, phrase, last, total = [], [], [], None, 0.0
    while total < a.len:
        i = rng.choice([j for j in range(len(pool)) if j != last])
        last = i
        kind, x = pool[i]
        rate = rng.uniform(*pitch)  # >1 — выше и быстрее
        y = np.interp(np.arange(0, len(x) - 1, rate), np.arange(len(x)), x) * rng.uniform(0.75, 1.0)
        out.append(y)
        phrase.append(WORDS[a.text][kind][rng.integers(2)])
        if len(phrase) >= a.phrase:
            words.append(" ".join(phrase).capitalize() + rng.choice([".", "!", "?"], p=[0.5, 0.3, 0.2]))
            phrase = []
            g = rng.uniform(gap[1], gap[1] * 2.5)
        else:
            g = rng.uniform(*gap)
        out.append(np.zeros(int(g * SR), np.float32))
        total += (len(y) + int(g * SR)) / SR
    if phrase:
        words.append(" ".join(phrase).capitalize() + ".")
    write(a.dst, np.concatenate(out[:-1]) * 0.9)
    text = " ".join(words)
    with open(os.path.splitext(a.dst)[0] + ".txt", "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(f"{a.dst}  {sum(len(x) for x in out[:-1]) / SR:.1f} с, звуков {len(out) // 2}\n{text}")


if __name__ == "__main__":
    main()
