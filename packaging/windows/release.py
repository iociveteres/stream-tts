"""Файлы релиза на GitHub: latest.json для проверки обновлений (stream-tts/streamtts/updates.py) и текст релиза.

usage: python packaging/windows/release.py <тег> [--repo owner/name] [--check]

Тег — v<версия>, версия должна совпадать с __version__ в stream-tts/streamtts/__init__.py (--check проверяет только
это: CI делает так до долгой сборки). Без --check нужен собранный установщик dist/StreamTTS-Setup-<версия>.exe
(build-exe.py --setup). Что нового — сообщение аннотированного тега (git tag -a v0.2.0 -m "…"), оно же текст релиза.
Результат: dist/latest.json (версия, дата, страница релиза, что нового, установщик с размером и sha256) и
dist/notes.md. repo — где публикуется релиз (в CI — GITHUB_REPOSITORY); он должен совпадать с REPO в updates.py,
иначе программа ищет обновления в другом месте. Нужен только Python 3 (стандартная библиотека) и git.
"""
import argparse, datetime, hashlib, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PKG = os.path.join(ROOT, "stream-tts", "streamtts")
DIST = os.path.join(HERE, "dist")


def const(file, name):
    with open(os.path.join(PKG, file), encoding="utf-8") as f:
        return re.search(rf'^{name} = "(.+?)"', f.read(), re.M)[1]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def tag_message(tag):
    """Сообщение аннотированного тега без подписи; у лёгкого тега — пусто."""
    if subprocess.run(["git", "cat-file", "-t", tag], cwd=ROOT, capture_output=True, text=True).stdout.strip() != "tag":
        return ""
    out = subprocess.run(["git", "tag", "-l", "--format=%(contents)", tag], cwd=ROOT, capture_output=True,
                         text=True, encoding="utf-8", check=True).stdout
    return out.split("-----BEGIN PGP SIGNATURE-----")[0].strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("tag")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--check", action="store_true", help="только сверить тег с версией")
    a = ap.parse_args()

    version = const("__init__.py", "__version__")
    if a.tag != f"v{version}":
        sys.exit(f"тег {a.tag}, а версия программы {version}: поправьте __version__ в streamtts/__init__.py "
                 f"или тег (v{version})")
    repo = const("updates.py", "REPO")
    if a.repo and a.repo != repo:
        sys.exit(f"релиз публикуется в {a.repo}, а программа ищет обновления в {repo} (REPO в updates.py)")
    if a.check:
        print(f"тег {a.tag} совпадает с версией {version}")
        return

    setup = os.path.join(DIST, f"StreamTTS-Setup-{version}.exe")
    if not os.path.isfile(setup):
        sys.exit(f"нет {setup}: сначала build-exe.py --setup")
    name = os.path.basename(setup)
    notes = tag_message(a.tag)
    base = f"https://github.com/{repo}/releases"
    latest = dict(
        version=version,
        date=datetime.date.today().isoformat(),
        url=f"{base}/tag/{a.tag}",
        notes=notes,
        files=dict(setup=dict(name=name, url=f"{base}/download/{a.tag}/{name}", size=os.path.getsize(setup),
                              sha256=sha256(setup))),
    )
    with open(os.path.join(DIST, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(latest, f, ensure_ascii=False, indent=1)
    with open(os.path.join(DIST, "notes.md"), "w", encoding="utf-8") as f:
        f.write(notes or f"StreamTTS {version}")
    print(json.dumps(latest, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
