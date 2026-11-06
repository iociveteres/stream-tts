"""Сборка архива кода для передачи внешнему исполнителю.

Кладёт в handoff/ две вещи: git bundle с историей (клонируется как репозиторий) и zip рабочего дерева —
только файлы из git и новые файлы, не попавшие под .gitignore. Данные (модели, голоса, voice-clips,
stream-tts-data, .venv, сборки установщика) в архив не попадают по определению: их нет в списке файлов.

usage: python scripts/pack-handoff.py [--out ПАПКА]

Выход: handoff/stream-tts-<коммит>.bundle, handoff/stream-tts-<коммит>.zip, handoff/BASE.txt.
"""
import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "handoff")
# Защита от случайной утечки данных: даже если файл попал в git, такие пути в архив не кладём.
FORBIDDEN = ("models/", "voices/", "voice-clips/", "voice-refs/", "voice-sets/", "voice-origs/",
             "samples/", "stream-tts-data/", "runtime/", "espeech-runtime/", "packaging/windows/build/",
             "packaging/windows/dist/", "packaging/windows/.inno/", "handoff/")
MAX_FILE = 4 * 1024 * 1024  # крупнее — почти наверняка данные, а не код


def forbidden(name):
    """Запрещённая папка, в которой лежит файл, или None.

    Окружения Python — на любой глубине и под любым именем (.venv, .venv311): .gitignore знает только
    .venv/, а незакоммиченный espeech/.venv311 (~19 тыс. файлов) иначе ушёл бы в zip как «новые файлы»."""
    parts = name.split("/")
    for i, part in enumerate(parts[:-1]):
        if part.startswith(".venv"):
            return "/".join(parts[:i + 1]) + "/"
    return next((bad for bad in FORBIDDEN if name.startswith(bad)), None)


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout


def file_list():
    tracked = git("ls-files").splitlines()
    fresh = git("ls-files", "--others", "--exclude-standard").splitlines()
    files, skipped = [], []
    for name in dict.fromkeys(tracked + fresh):
        path = name.replace("/", os.sep)
        bad = forbidden(name)
        if bad:
            skipped.append(bad)
            continue
        if not os.path.isfile(os.path.join(ROOT, path)):
            continue
        if os.path.getsize(os.path.join(ROOT, path)) > MAX_FILE:
            skipped.append(name + " (больше 4 МБ)")
            continue
        files.append(name)
    return files, skipped


def main():
    out = OUT
    if "--out" in sys.argv:
        out = os.path.abspath(sys.argv[sys.argv.index("--out") + 1])
    os.makedirs(out, exist_ok=True)

    sha = git("rev-parse", "--short", "HEAD").strip()
    base = f"stream-tts-{sha}"
    files, skipped = file_list()

    bundle = os.path.join(out, base + ".bundle")
    git("bundle", "create", bundle, "--all")

    zip_path = os.path.join(out, base + ".zip")
    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for name in files:
            full = os.path.join(ROOT, name.replace("/", os.sep))
            z.write(full, base + "/" + name)
            total += os.path.getsize(full)
        z.writestr(base + "/BASE.txt",
                   f"коммит: {git('rev-parse', 'HEAD').strip()}\n"
                   f"файлов: {len(files)}\n"
                   "задание: docs/local/refactor-brief.md — не в git, передать отдельно\n")

    print(f"коммит {sha}: {len(files)} файлов, {total / 1024:.0f} КБ без сжатия")
    # Пропущенное — по запрещённым папкам: в окружении Python тысячи файлов
    groups = {}
    for key in skipped:
        groups[key] = groups.get(key, 0) + 1
    for key, n in sorted(groups.items()):
        print(f"  пропущен: {key}" + (f" ({n} файлов)" if n > 1 else ""))
    for path in (bundle, zip_path):
        try:
            shown = os.path.relpath(path, ROOT)
        except ValueError:  # --out на другом диске Windows
            shown = path
        print(f"{os.path.getsize(path) / 1024 / 1024:.2f} МБ  {shown}")


if __name__ == "__main__":
    main()
