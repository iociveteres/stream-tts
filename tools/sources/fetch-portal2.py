"""Русские реплики персонажа Portal 2: текст из субтитров установленной игры, звук с CDN Portal Wiki.

Использование (только стандартная библиотека):
    python tools/sources/fetch-portal2.py "<Portal 2>" <папка> [--speaker glados] [--file-prefix GLaDOS]
        [--delay 0.3] [--limit N]

Русской озвучки в установке может не быть (portal2_russian/ скачивается только при русском языке игры), а субтитры
есть всегда: portal2/resource/subtitles_russian.txt (UTF-16), ключи «<персонаж>.<реплика>», текст с пометками
<clr:…> и «ГЛаДОС: » — они убираются. Звук — файлы вики <Префикс>_<реплика>_ru.wav. API Portal Wiki быстро
отвечает 429 (fetch-wiki-lines.py, docs/voice-pipeline.md), поэтому путь к файлу считается как в MediaWiki
(img/<md5[0]>/<md5[:2]>/<имя>) и берётся прямо с CDN i1.theportalwiki.net, с паузой --delay между файлами.
Файла нет на вики (404) — реплика пропускается.

Выход — <папка>/NNN_<имя>.wav и lines.json [{file, text, src, key}] в формате fetch-wiki-lines.py: дальше
tools/sources/lora-lines.py (текст «given» сверяется с faster-whisper) или build-ref.py. Уже скачанные файлы
не качаются повторно.
"""
import argparse, hashlib, json, os, re, sys, time, urllib.error, urllib.request

CDN = "https://i1.theportalwiki.net/img"
UA = {"User-Agent": "Mozilla/5.0 (local-voice-gen; voice lines for personal TTS)"}


def subtitles(game, speaker):
    path = os.path.join(game, "portal2", "resource", "subtitles_russian.txt")
    text = open(path, encoding="utf-16").read()
    out = {}
    for key, val in re.findall(r'^\s*"([^"\[]+)"\s+"(.*)"\s*$', text, re.M):
        sp, _, name = key.partition(".")
        if sp.lower() != speaker or not name:
            continue
        t = re.sub(r"<[^>]*>", "", val)
        t = re.sub(r"^[^:]{1,20}:\s*", "", t)  # «ГЛаДОС: »
        t = " ".join(t.replace("\\n", " ").split())
        if t:
            out[name.lower()] = t
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("game")
    ap.add_argument("out")
    ap.add_argument("--speaker", default="glados")
    ap.add_argument("--file-prefix", default="GLaDOS")
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--limit", type=int)
    a = ap.parse_args()

    subs = subtitles(a.game, a.speaker)
    print(f"субтитров {a.speaker}: {len(subs)}")
    os.makedirs(a.out, exist_ok=True)
    lj = os.path.join(a.out, "lines.json")
    old = {x["key"]: x for x in json.load(open(lj, encoding="utf-8"))} if os.path.exists(lj) else {}
    lines, missing = [], 0
    for name, text in list(subs.items())[:a.limit]:
        fname = f"{a.file_prefix}_{name}_ru.wav"
        h = hashlib.md5(fname.encode()).hexdigest()
        src = f"{CDN}/{h[0]}/{h[:2]}/{fname}"
        fn = old[name]["file"] if name in old else f"{len(lines):03d}_{fname}"
        path = os.path.join(a.out, fn)
        if not os.path.exists(path):
            try:
                with urllib.request.urlopen(urllib.request.Request(src, headers=UA), timeout=60) as r:
                    data = r.read()
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    print("429 — пауза 60 с", file=sys.stderr)
                    time.sleep(60)
                missing += 1
                continue
            open(path, "wb").write(data)
            time.sleep(a.delay)
        lines.append(dict(file=fn, text=text, src=src, key=name))
        if len(lines) % 100 == 0:
            print(f"  {len(lines)}…", flush=True)
    json.dump(lines, open(lj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{len(lines)} реплик -> {a.out}, нет на вики: {missing}")


if __name__ == "__main__":
    main()
