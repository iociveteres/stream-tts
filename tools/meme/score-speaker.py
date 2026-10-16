"""Похожесть каждой фразы из lines.json на эталонный голос (эмбеддинги CAM++, как в synth-ab.py).

Использование (venv проекта: numpy, onnxruntime):
    .venv/Scripts/python.exe tools/meme/score-speaker.py <папка-с-lines.json> <эталон.wav|mp3> [ещё эталоны...]
        [--iterate]

Пишет в каждую запись lines.json поле sim (косинус к среднему эмбеддингу эталонов).
--iterate: после первого прохода берёт центр 10 самых похожих фраз и пересчитывает sim
относительно него — так эталоном становится сам голос из записи, а не короткий образец.
Дальше build-ref.py --min-sim 0.6 берёт только фразы этого голоса.
"""
import argparse
import importlib.util
import json
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # tools/<группа>/ -> корень проекта
spec = importlib.util.spec_from_file_location("synth_ab", os.path.join(ROOT, "tools", "wc3", "synth-ab.py"))
ab = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ab)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("refs", nargs="+")
    ap.add_argument("--iterate", action="store_true")
    a = ap.parse_args()

    embed = ab.Embedder()
    embed.cache = {}  # общий кеш synth-ab.py (embeddings.json) не трогаем
    center = ab.unit(sum(embed(r, r) for r in a.refs))

    path = os.path.join(a.src, "lines.json")
    lines = json.load(open(path, encoding="utf-8"))
    embs = [embed(ln["file"], os.path.join(a.src, ln["file"])) for ln in lines]
    sims = np.array([float(e @ center) for e in embs])
    if a.iterate and len(lines) >= 10:
        top = np.argsort(-sims)[:10]
        center = ab.unit(sum(embs[i] for i in top))
        sims = np.array([float(e @ center) for e in embs])
    for ln, s in zip(lines, sims):
        ln["sim"] = round(float(s), 3)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(lines, f, ensure_ascii=False, indent=1)

    for i in np.argsort(-sims):
        ln = lines[i]
        print(f"{i:3d} sim={ln['sim']:.2f} {ln['text'][:90]}")


if __name__ == "__main__":
    main()
