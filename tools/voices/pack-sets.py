"""Коллекции голосов для импорта в stream-tts: voice-sets/<модель>/<источник>.zip — один zip на пару «модель +
источник», он же набор в админке («Наборы голосов», streamtts/voicesets.py). Внутри zip — папка <источник>/ с файлами
голосов (<голос>.gguf или <голос>.espeech) и manifest.json ({голос: {title, post}}). Установка коллекции заменяет
голоса этой модели в наборе <источник> и не трогает голоса другой модели.

usage: python tools/voices/pack-sets.py [<модель>/<источник> …] [--list] [--root <корень>]
  без аргументов — все коллекции; --list — только показать, что войдёт, без записи;
  --root — корень проекта с voice-clips/, voice-refs/, voices/ (по умолчанию этот; из worktree — основная копия).

Откуда голоса:
  cosyvoice  голоса — ключи voice-refs/cosyvoice/<источник>/manifest.json (pick.py --install), файл —
             voices/<голос>.gguf или voices/<набор>/<голос>.gguf (его пишет scripts/import-voices.sh);
  espeech    голоса — ключи voice-clips/espeech/<источник>/manifest.json (tools/wc3/lora-clips.py,
             tools/sources/extract-morrowind.py), файл — voice-clips/espeech/<источник>/<голос>/<голос>.espeech;
             выбор на слух — picks.json рядом: {голос: файл от папки голоса (другой адаптер или образец) или null —
             не ставить}. Название голоса — как у CosyVoice того же источника, если там такой голос есть.
Голос без файла пропускается с предупреждением. Zip пишется во временный файл и заменяет прежний целиком.
Только стандартная библиотека Python."""
import argparse, json, os, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # tools/<группа>/ -> корень проекта
KEEP = ("title", "post")   # поля манифеста, нужные stream-tts; остальное (откуда образец, оценки) — внутреннее


def load(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def entry(meta):
    return {k: meta[k] for k in KEEP if meta.get(k)}


def cosyvoice(src):
    """[(голос, файл, запись манифеста)] и предупреждения."""
    VOICES = os.path.join(ROOT, "voices")
    man = load(os.path.join(ROOT, "voice-refs", "cosyvoice", src, "manifest.json"), {})
    out, warn = [], []
    for v in sorted(man):
        hits = [p for p in [os.path.join(VOICES, v + ".gguf")] if os.path.isfile(p)] or \
            sorted(os.path.join(VOICES, d, v + ".gguf") for d in os.listdir(VOICES)
                   if os.path.isfile(os.path.join(VOICES, d, v + ".gguf")))
        if hits:
            out.append((v, hits[0], entry(man[v])))
        else:
            warn.append(f"{v}: нет voices/{v}.gguf (scripts/import-voices.sh voice-refs/cosyvoice/{src})")
    return out, warn


def espeech(src):
    base = os.path.join(ROOT, "voice-clips", "espeech", src)
    man = load(os.path.join(base, "manifest.json"), {})
    picks = load(os.path.join(base, "picks.json"), {})
    titles = load(os.path.join(ROOT, "voice-refs", "cosyvoice", src, "manifest.json"), {})
    out, warn = [], []
    for v in sorted(man):
        f = picks.get(v, f"{v}.espeech")
        if f is None:
            continue
        path = os.path.join(base, v, f)
        if os.path.isfile(path):
            out.append((v, path, entry(dict(man[v], title=titles.get(v, {}).get("title") or man[v].get("title")))))
        else:
            warn.append(f"{v}: нет {os.path.relpath(path, ROOT)}")
    for v in sorted(set(picks) - set(man)):
        warn.append(f"{v}: есть в picks.json, нет в manifest.json")
    return out, warn


# модель -> (голоса коллекции, папка с источниками: в каждом manifest.json)
MODELS = {"cosyvoice": (cosyvoice, ("voice-refs", "cosyvoice")), "espeech": (espeech, ("voice-clips", "espeech"))}


def collections():
    """[(модель, источник)]: папки источников с manifest.json."""
    out = []
    for model, (_, where) in MODELS.items():
        root = os.path.join(ROOT, *where)
        if os.path.isdir(root):
            out += [(model, s) for s in sorted(os.listdir(root)) if os.path.isfile(os.path.join(root, s, "manifest.json"))]
    return out


def pack(model, src, dry):
    voices, warn = MODELS[model][0](src)
    for w in warn:
        print(f"  ! {w}")
    dst = os.path.join(ROOT, "voice-sets", model, src + ".zip")
    size = sum(os.path.getsize(p) for _, p, _ in voices)
    print(f"{os.path.relpath(dst, ROOT)}: голосов {len(voices)}, {size / 2**20:.1f} МБ")
    if dry or not voices:
        return
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".tmp"
    # голоса уже сжаты плохо (веса fp16, коды GGUF) — без сжатия: zip собирается и ставится быстро
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for v, path, _ in voices:
            z.write(path, f"{src}/{v}{os.path.splitext(path)[1]}")
        z.writestr(f"{src}/manifest.json", json.dumps({v: e for v, _, e in voices}, ensure_ascii=False, indent=1))
    os.replace(tmp, dst)


def main():
    global ROOT
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("which", nargs="*", help="<модель>/<источник>")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--root", default=ROOT)
    a = ap.parse_args()
    ROOT = os.path.abspath(a.root)
    have = collections()
    todo = have
    if a.which:
        todo = [tuple(w.strip("/").split("/", 1)) for w in a.which]
        unknown = [f"{m}/{s}" for m, s in todo if (m, s) not in have]
        if unknown:
            sys.exit(f"Нет коллекций: {' '.join(unknown)}. Есть: {' '.join(f'{m}/{s}' for m, s in have)}")
    for model, src in todo:
        pack(model, src, a.list)


if __name__ == "__main__":
    main()
