"""Собирает образец голоса из папки реплик с lines.json (fetch-wiki-lines.py, transcribe-lines.py).

Использование:
    python tools/meme/build-ref.py <папка-реплик> <имя-голоса> [опции]

    --pick 3,17,42        взять эти реплики (номера из lines.json) в этом порядке
    --exclude РЕГЭКСП     не брать файлы, чьё имя подходит (смех, боль, смерть…)
    --replace A=B         заменить в тексте (латиница в имени героя и т. п.), можно несколько раз
    --target 12-18        длина образца без --pick, с
    --min-sim 0.6         только фразы, похожие на голос (поле sim от score-speaker.py)
    --out voice-refs/meme папка для <имя>.wav + <имя>.txt (формат import-voices.sh)

Без --pick берутся самые длинные по тексту реплики, разные по смыслу, пока образец не наберёт
длину. Склейка и обрезка тишины — как в cut-clips.py (24 кГц моно, пауза GAP).
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

GAP = 0.25
MIN_LETTERS = 12          # реплики короче («Ага», «Э-э») почти не дают голосу текста


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    return float(r.stdout)


def letters(text):
    return re.sub(r"[^а-яa-z]", "", text.lower().replace("ё", "е"))


def sentence(text):
    text = text.strip()
    return text if re.search(r"[.!?…。！？ー][»\"]?$", text) else text + "."


def trimmed(src, tmp):
    dst = os.path.join(tmp, os.path.basename(src) + ".wav")
    ffmpeg("-i", src, "-af",
           "silenceremove=start_periods=1:start_threshold=-50dB,areverse,"
           "silenceremove=start_periods=1:start_threshold=-50dB,areverse",
           "-ac", "1", "-ar", "24000", dst)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("name")
    ap.add_argument("--pick")
    ap.add_argument("--exclude")
    ap.add_argument("--replace", action="append", default=[])
    ap.add_argument("--target", default="12-18")
    ap.add_argument("--min-sim", type=float)
    ap.add_argument("--out", default=os.path.join("voice-refs", "meme"))
    a = ap.parse_args()
    lo, hi = map(float, a.target.split("-"))

    with open(os.path.join(a.src, "lines.json"), encoding="utf-8") as f:
        lines = json.load(f)
    for i, ln in enumerate(lines):
        ln["n"] = i
        for r in a.replace:
            old, new = r.split("=", 1)
            ln["text"] = ln["text"].replace(old, new)

    tmp = tempfile.mkdtemp(prefix="ref-")
    if a.pick:
        chosen = [lines[int(i)] for i in a.pick.split(",")]
        for ln in chosen:
            ln["wav"] = trimmed(os.path.join(a.src, ln["file"]), tmp)
            ln["dur"] = duration(ln["wav"])
    else:
        pool = [ln for ln in lines if len(letters(ln["text"])) >= MIN_LETTERS and not ln.get("bad")
                and not (a.exclude and re.search(a.exclude, ln["file"], re.I))
                and not (a.min_sim and ln.get("sim", 0) < a.min_sim)]
        pool.sort(key=lambda ln: -len(letters(ln["text"])))
        chosen, seen, total = [], set(), -GAP
        for ln in pool:
            key = letters(ln["text"])
            if key in seen:
                continue
            ln["wav"] = trimmed(os.path.join(a.src, ln["file"]), tmp)
            ln["dur"] = duration(ln["wav"])
            if ln["dur"] > hi or total + GAP + ln["dur"] > hi:
                continue
            chosen.append(ln)
            seen.add(key)
            total += GAP + ln["dur"]
            if total >= lo:
                break
        chosen.sort(key=lambda ln: ln["n"])
    if not chosen:
        sys.exit("Не из чего собрать образец")

    os.makedirs(a.out, exist_ok=True)
    dst = os.path.join(a.out, a.name)
    inputs, filt = [], []
    for i, ln in enumerate(chosen):
        inputs += ["-i", ln["wav"]]
        pad = "anull" if i == len(chosen) - 1 else f"apad=pad_dur={GAP}"
        filt.append(f"[{i}:a]{pad}[a{i}]")
    filt.append("".join(f"[a{i}]" for i in range(len(chosen))) + f"concat=n={len(chosen)}:v=0:a=1[out]")
    ffmpeg(*inputs, "-filter_complex", ";".join(filt), "-map", "[out]", "-ac", "1", "-ar", "24000",
           dst + ".wav")
    text = " ".join(sentence(ln["text"]) for ln in chosen)
    with open(dst + ".txt", "w", encoding="utf-8") as f:
        f.write(text + "\n")

    man_path = os.path.join(a.out, "manifest.json")
    man = json.load(open(man_path, encoding="utf-8")) if os.path.exists(man_path) else {}
    man[a.name] = dict(duration=round(duration(dst + ".wav"), 2), installed=str(datetime.date.today()),
                       source=[os.path.join(a.src, ln["file"]).replace("\\", "/") for ln in chosen],
                       text=text)
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"{dst}.wav  {man[a.name]['duration']} с, реплик {len(chosen)}: "
          + ",".join(str(ln["n"]) for ln in chosen))
    print(text)


if __name__ == "__main__":
    main()
