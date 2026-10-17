"""Сборка StreamTTS.exe: Nuitka standalone (папка с exe), по желанию с рантаймом рядом и установщиком.

usage: packaging/windows/.venv/Scripts/python.exe packaging/windows/build-exe.py [--runtime core|all] [--setup]
           [--no-build] [--jobs N]

Запускать python из сборочного venv (packaging/windows/requirements.txt): Nuitka компилирует тем интерпретатором,
которым запущена, и берёт пакеты из него. Компилятор — MSVC (Visual Studio или Build Tools с C++): MinGW Nuitka для
Python 3.13+ не поддерживает. Первая сборка — несколько минут, повторные быстрее (кэш Nuitka в %LOCALAPPDATA%).
Результат: packaging/windows/dist/StreamTTS/ (StreamTTS.exe, DLL Python, streamtts/static, effects/).
--runtime core|all кладёт в dist/StreamTTS/runtime/ рантайм из build/runtime (build-runtime.py): core — без CUDA,
all — с CUDA. Так собранную программу можно запустить без установщика; установщик (StreamTTS.iss) берёт рантайм
сам, из build/runtime.
--setup собирает установщик dist/StreamTTS-Setup-<VERSION>.exe (Inno Setup 6: ISCC из переменной ISCC, портативной
установки в packaging/windows/.inno, PATH или стандартных папок установки), --no-build — только установщик из уже
собранного dist/StreamTTS.
"""
import argparse, os, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(ROOT, "stream-tts")
DIST = os.path.join(HERE, "dist")
APP = os.path.join(DIST, "StreamTTS")
VERSION = "0.1.0"


def nuitka_args(jobs):
    return [
        sys.executable, "-m", "nuitka", os.path.join(HERE, "StreamTTS.py"),
        # папка — dist/StreamTTS.dist (--output-folder-name в Nuitka 4.3 на неё не влияет), main() её переименует
        "--mode=standalone", f"--output-dir={DIST}", "--remove-output",
        "--output-filename=StreamTTS.exe", "--msvc=latest", "--assume-yes-for-downloads",
        f"--jobs={jobs}",
        # streamtts.__main__ desktop.py импортирует внутри main(), static/ и profanity.txt — данные пакета
        "--include-package=streamtts", "--include-package-data=streamtts",
        # эффект грузится importlib по пути (paths.resource -> папка exe), его импорты Nuitka не видит
        f"--include-data-files={os.path.join(ROOT, 'effects', 'villager-fx.py')}=effects/villager-fx.py",
        "--include-module=argparse,glob,subprocess,threading",  # zlib встроен в Python
        # консольное приложение: закрыть окно — выключить (desktop.py)
        "--windows-console-mode=force",
        "--company-name=StreamTTS", "--product-name=StreamTTS", f"--file-version={VERSION}",
        f"--product-version={VERSION}", "--file-description=StreamTTS: озвучка наград Twitch",
        "--nofollow-import-to=tkinter,unittest,pydoc_data",
    ]


def iscc():
    pf = [os.environ.get(v, "") for v in ("ProgramFiles(x86)", "ProgramFiles")]
    pf.append(os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"))
    local = os.path.join(HERE, ".inno", "ISCC.exe")  # innosetup-6.x.exe /PORTABLE=1 /CURRENTUSER /DIR=.inno
    for p in [os.environ.get("ISCC", ""), local, shutil.which("iscc") or ""] + [
            os.path.join(d, "Inno Setup 6", "ISCC.exe") for d in pf if d]:
        if p and os.path.isfile(p):
            return p
    sys.exit("нет Inno Setup 6 (ISCC.exe): поставьте его или укажите путь в переменной ISCC")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runtime", choices=("core", "all"), help="положить рантайм из build/runtime рядом с exe")
    ap.add_argument("--setup", action="store_true", help="собрать установщик (Inno Setup)")
    ap.add_argument("--no-build", action="store_true", help="не пересобирать exe")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    a = ap.parse_args()

    if not a.no_build:
        for d in (APP, os.path.join(DIST, "StreamTTS.dist")):
            shutil.rmtree(d, ignore_errors=True)
        env = dict(os.environ, PYTHONPATH=SRC)  # пакет streamtts — из исходников
        subprocess.run(nuitka_args(a.jobs), cwd=HERE, env=env, check=True)
        os.replace(os.path.join(DIST, "StreamTTS.dist"), APP)

    if a.runtime:
        rt = os.path.join(HERE, "build", "runtime")
        if not os.path.isdir(os.path.join(rt, "core")):
            sys.exit(f"нет {os.path.join(rt, 'core')}: сначала packaging/windows/build-runtime.py")
        for comp in ("core", "cuda") if a.runtime == "all" else ("core",):
            shutil.copytree(os.path.join(rt, comp), os.path.join(APP, "runtime"), dirs_exist_ok=True)
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(APP) for f in fs)
    print(f"Собрано: {APP} ({size / 2**20:.0f} МБ)")

    if a.setup:
        subprocess.run([iscc(), f"/DAppVersion={VERSION}", os.path.join(HERE, "StreamTTS.iss")], check=True)


if __name__ == "__main__":
    main()
