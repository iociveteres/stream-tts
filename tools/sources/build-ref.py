"""Собирает вариант образца из папки реплик с lines.json (fetch-wiki-lines.py, transcribe-lines.py).

Использование:
    python tools/sources/build-ref.py <папка-реплик> <набор> <голос> <id> [опции]

    --pick 3,17,42        взять эти реплики (номера из lines.json) в этом порядке
    --exclude РЕГЭКСП     не брать файлы, чьё имя подходит (смех, боль, смерть…)
    --replace A=B         заменить в тексте (латиница в имени героя и т. п.), можно несколько раз
    --target 12-18        длина образца без --pick, с
    --min-sim 0.6         только фразы, похожие на голос (поле sim от score-speaker.py)
    --energy 0-0.6        только фразы с такой энергией (поле arousal от tools/voices/emotions.py --lines):
                          так собирается образец с ровной подачей, например спокойный или с напором
    --label "…"           подпись варианта на странице выбора
    --install <имя>       поставить вариант под другим именем (например <голос>-angry)
    --title "…"           название голоса на странице (для нового голоса)

Без --pick берутся самые длинные по тексту реплики, разные по смыслу, пока образец не наберёт
длину. Результат — вариант voice-clips/cosyvoice/<набор>/<голос>/<id>.wav + .txt в alts.json набора
(склейка как в tools/voices/pick.py --add). Дальше synth.py и страница pick.py: docs/voice-pipeline.md.
"""
import argparse
import importlib.util
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("common", os.path.join(os.path.dirname(HERE), "common.py"))
common = importlib.util.module_from_spec(spec)  # ROOT, load, ffmpeg, звук, JSON
spec.loader.exec_module(common)
ROOT = common.ROOT
pick = common.load("tools/voices/pick.py")

GAP = pick.GAP
MIN_LETTERS = 12          # реплики короче («Ага», «Э-э») почти не дают голосу текста


def letters(text):
    return re.sub(r"[^а-яa-z]", "", text.lower().replace("ё", "е"))


def sentence(text):
    text = text.strip()
    return text if re.search(r"[.!?…。！？ー][»\"]?$", text) else text + "."


def trimmed(src, tmp):
    dst = os.path.join(tmp, os.path.basename(src) + ".wav")
    common.ffmpeg("-i", src, "-af",
           "silenceremove=start_periods=1:start_threshold=-50dB,areverse,"
           "silenceremove=start_periods=1:start_threshold=-50dB,areverse",
           "-ac", "1", "-ar", "24000", dst)
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("set")
    ap.add_argument("voice")
    ap.add_argument("id")
    ap.add_argument("--pick")
    ap.add_argument("--exclude")
    ap.add_argument("--replace", action="append", default=[])
    ap.add_argument("--target", default="12-18")
    ap.add_argument("--min-sim", type=float)
    ap.add_argument("--energy")
    ap.add_argument("--label")
    ap.add_argument("--install")
    ap.add_argument("--title")
    a = ap.parse_args()
    lo, hi = map(float, a.target.split("-"))
    e_lo, e_hi = map(float, a.energy.split("-")) if a.energy else (0.0, 1.0)

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
            ln["dur"] = common.duration(ln["wav"])
    else:
        pool = [ln for ln in lines if len(letters(ln["text"])) >= MIN_LETTERS and not ln.get("bad")
                and not (a.exclude and re.search(a.exclude, ln["file"], re.I))
                and not (a.min_sim and ln.get("sim", 0) < a.min_sim)
                and (not a.energy or e_lo <= ln.get("arousal", -1) <= e_hi)]
        pool.sort(key=lambda ln: -len(letters(ln["text"])))
        chosen, seen, total = [], set(), -GAP
        for ln in pool:
            key = letters(ln["text"])
            if key in seen:
                continue
            ln["wav"] = trimmed(os.path.join(a.src, ln["file"]), tmp)
            ln["dur"] = common.duration(ln["wav"])
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

    text = " ".join(sentence(ln["text"]) for ln in chosen)
    print("реплики " + ",".join(str(ln["n"]) for ln in chosen) + f", {sum(ln['dur'] for ln in chosen):.1f} с")
    print(text)
    pick.add(a.set, a.voice, a.id, [os.path.join(a.src, ln["file"]) for ln in chosen], text,
             label=a.label, install=a.install, title=a.title)


if __name__ == "__main__":
    main()
