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
             "samples/", "stream-tts-data/", "runtime/", ".venv/", "packaging/windows/build/",
             "packaging/windows/dist/", "packaging/windows/.inno/", "handoff/")
MAX_FILE = 4 * 1024 * 1024  # крупнее — почти наверняка данные, а не код


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout


def file_list():
    tracked = git("ls-files").splitlines()
    fresh = git("ls-files", "--others", "--exclude-standard").splitlines()
    files, skipped = [], []
    for name in dict.fromkeys(tracked + fresh):
        path = name.replace("/", os.sep)
        if any(name.startswith(bad) for bad in FORBIDDEN):
            skipped.append(name)
            continue
        if not os.path.isfile(os.path.join(ROOT, path)):
            continue
        if os.path.getsize(os.path.join(ROOT, path)) > MAX_FILE:
            skipped.append(name)
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
                   f"задание: docs/refactor-brief.md\n")

    print(f"коммит {sha}: {len(files)} файлов, {total / 1024:.0f} КБ без сжатия")
    for name in skipped:
        print(f"  пропущен: {name}")
    for path in (bundle, zip_path):
        print(f"{os.path.getsize(path) / 1024 / 1024:.2f} МБ  {os.path.relpath(path, ROOT)}")


if __name__ == "__main__":
    main()
