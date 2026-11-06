"""Сборка StreamTTS.exe: Nuitka standalone (папка с exe), по желанию с рантаймом рядом и установщиком.

usage: packaging/windows/.venv/Scripts/python.exe packaging/windows/build-exe.py [--runtime core|all] [--setup]
           [--no-build] [--jobs N]

Запускать python из сборочного venv (packaging/windows/requirements.txt): Nuitka компилирует тем интерпретатором,
которым запущена, и берёт пакеты из него. Компилятор — MSVC (Visual Studio или Build Tools с C++): MinGW Nuitka для
Python 3.13+ не поддерживает. Первая сборка — несколько минут, повторные быстрее (кэш Nuitka в %LOCALAPPDATA%).
Результат: packaging/windows/dist/StreamTTS/ (StreamTTS.exe, DLL Python, streamtts/static, effects/, licenses/ —
тексты лицензий Python, пакетов и runtime Nuitka; лицензии рантайма — в runtime/licenses/). В exe скомпилирован и
supertonic-server (пакет supertonic/ и нормализатор из espeech/, чистый Python): его запускает StreamTTS.exe
--supertonic (StreamTTS.py); веса модели программа качает по кнопке.
--runtime core|all кладёт в dist/StreamTTS/runtime/ рантайм из build/runtime (build-runtime.py): core — без CUDA,
all — с CUDA (нужен build-runtime.py --cuda). Так собранную программу можно запустить без установщика; установщик
(StreamTTS.iss) берёт рантайм сам, из build/runtime/core.
--setup собирает установщик dist/StreamTTS-Setup-<версия>.exe (версия — __version__ в streamtts/__init__.py; Inno
Setup 6.5+: ISCC из переменной ISCC, портативной установки в packaging/windows/.inno, PATH или стандартных папок
установки), --no-build — только установщик из уже собранного dist/StreamTTS. Установщик ничего не качает (модель и
CUDA программа качает сама на вкладке «Сервер»); из streamtts/downloads.json он через /D получает версию и файлы CUDA
(чтобы сохранить при обновлении CUDA, скачанную в программе) и размеры для сводки перед установкой.
"""
import argparse, importlib.metadata as md, json, os, re, shutil, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SRC = os.path.join(ROOT, "stream-tts")
DIST = os.path.join(HERE, "dist")
APP = os.path.join(DIST, "StreamTTS")
# Версия — одна на всё: streamtts/__init__.py (её же показывает админка и сверяет CI с тегом релиза)
VERSION = re.search(r'__version__ = "(.+?)"', open(os.path.join(SRC, "streamtts", "__init__.py"), encoding="utf-8").read())[1]
# Пакеты, у которых лицензии лежат в папке пакета, а не в dist-info/licenses. Их зависимости не обходятся:
# onnxruntime требует flatbuffers, packaging и protobuf только для инструментов (onnxruntime.tools и т. п.), а
# `import onnxruntime` их не тянет, и Nuitka их в программу не кладёт.
OWN_LICENSES = {"onnxruntime": ("LICENSE", "ThirdPartyNotices.txt")}
# Модули espeech/, нужные supertonic-server (docs/supertonic.md, «Текст»): чистый Python и numpy, без torch
ESPEECH_MODULES = ("normalize", "numbers", "numerals", "latin", "accent", "wire")


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
        # supertonic-server (StreamTTS.exe --supertonic): пакет и модули espeech, которые он импортирует (нормализатор
        # русского, ударения «+», WAV); остальное в espeech/ — torch, его в программе нет
        "--include-package=supertonic",
        f"--include-module={','.join('espeech.' + m for m in ESPEECH_MODULES)}",
        # accent.py грузит RUAccent только в функции, которую supertonic-server не зовёт
        "--nofollow-import-to=ruaccent,torch",
        # консольное приложение: закрыть окно — выключить (desktop.py)
        "--windows-console-mode=force",
        "--company-name=StreamTTS", "--product-name=StreamTTS", f"--file-version={VERSION}",
        f"--product-version={VERSION}", "--file-description=StreamTTS: озвучка наград Twitch",
        "--nofollow-import-to=tkinter,unittest,pydoc_data",
        # separate.py: нужен только InferenceSession; инструменты onnxruntime тянут onnx, sympy, protobuf
        "--nofollow-import-to=onnxruntime.tools,onnxruntime.transformers,onnxruntime.quantization",
    ]


def requirements(path):
    """Имена пакетов из requirements.txt (с -r)."""
    names = []
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line.startswith("-r"):
            names += requirements(os.path.join(os.path.dirname(path), line[2:].strip()))
        elif line:
            names.append(re.match(r"[A-Za-z0-9._-]+", line).group())
    return names


def copy_licenses():
    """Тексты лицензий того, что Nuitka положила в программу, — в dist/StreamTTS/licenses/.

    LICENSE проекта (MIT, как StreamTTS-LICENSE.txt), уведомление о коде Supertone в supertonic-server
    (supertonic-THIRD-PARTY.txt, MIT), Python (LICENSE.txt установки, на которой venv: там же OpenSSL, libffi, SQLite
    и прочие), пакеты из stream-tts/requirements.txt с зависимостями (их *.dist-info/licenses; у numpy там и OpenBLAS, и рантайм GCC),
    исключение Nuitka для её runtime и списки THIRD-PARTY*.txt. Лицензии рантайма — в runtime/licenses/
    (build-runtime.py)."""
    lic = os.path.join(APP, "licenses")
    shutil.rmtree(lic, ignore_errors=True)
    os.makedirs(lic)
    for name in ("THIRD-PARTY.txt", "THIRD-PARTY.en.txt"):
        shutil.copy(os.path.join(HERE, name), lic)
    shutil.copy(os.path.join(ROOT, "LICENSE"), os.path.join(lic, "StreamTTS-LICENSE.txt"))
    # код supertonic-server — перенос helper.py Supertone (MIT); нормализатор espeech — код проекта
    shutil.copy(os.path.join(ROOT, "supertonic", "THIRD-PARTY.txt"), os.path.join(lic, "supertonic-THIRD-PARTY.txt"))
    os.makedirs(os.path.join(lic, "python"))
    shutil.copy(os.path.join(sys.base_prefix, "LICENSE.txt"), os.path.join(lic, "python"))

    def dist_licenses(dist, only=None):
        files = [f for f in dist.files or () if f.parts[0].endswith(".dist-info") and f.parts[1:2] == ("licenses",)
                 and (only is None or f.name == only)]
        own = OWN_LICENSES.get(dist.name.lower(), ())
        if not files and own:  # лицензии в папке пакета, а не в dist-info
            files = [f for f in dist.files or () if len(f.parts) == 2 and f.name in own]
        if not files:
            sys.exit(f"{dist.name}: нет файлов лицензий в dist-info/licenses")
        for f in files:
            dst = os.path.join(lic, dist.name.lower(), *(f.parts[2:] or f.parts[1:]))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(f.locate(), dst)

    # Зависимости без extra; не установленные — с маркером, ложным для этой платформы и версии Python
    todo, seen = requirements(os.path.join(SRC, "requirements.txt")), set()
    while todo:
        name = todo.pop().lower().replace("_", "-")
        if name in seen:
            continue
        seen.add(name)
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        dist_licenses(dist)
        if name not in OWN_LICENSES:
            todo += [re.match(r"[A-Za-z0-9._-]+", r).group() for r in dist.requires or () if "extra ==" not in r]
    dist_licenses(md.distribution("nuitka"), "LICENSE-RUNTIME.txt")
    print(f"Лицензии: {lic} ({', '.join(sorted(os.listdir(lic)))})")


def iscc_defines():
    """/D для StreamTTS.iss из downloads.json: версия и файлы CUDA, размеры загрузок для сводки (в МБ)."""
    with open(os.path.join(SRC, "streamtts", "downloads.json"), encoding="utf-8") as f:
        dl = json.load(f)
    cuda, sizes = dl["cuda"]["windows"], [m["size"] for m in dl["model"]["quants"].values()]
    mb = lambda n: round(n / 2**20)
    d = dict(AppVersion=VERSION, CudaVersion=cuda["version"], CudaFiles=",".join(cuda["files"]),
             CudaMB=mb(sum(a["size"] for a in cuda["archives"])), CudaUnpackedMB=mb(cuda["unpacked"]),
             ModelMinMB=mb(min(sizes)), ModelMaxMB=mb(max(sizes)))
    return [f"/D{k}={v}" for k, v in d.items()]


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
        # пакет streamtts — из исходников, supertonic и espeech — из корня проекта
        env = dict(os.environ, PYTHONPATH=os.pathsep.join((SRC, ROOT)))
        subprocess.run(nuitka_args(a.jobs), cwd=HERE, env=env, check=True)
        os.replace(os.path.join(DIST, "StreamTTS.dist"), APP)
    copy_licenses()

    if a.runtime:
        rt = os.path.join(HERE, "build", "runtime")
        if not os.path.isdir(os.path.join(rt, "core")):
            sys.exit(f"нет {os.path.join(rt, 'core')}: сначала packaging/windows/build-runtime.py")
        shutil.rmtree(os.path.join(APP, "runtime"), ignore_errors=True)  # GGML грузит все ggml-*.dll из папки
        for comp in ("core", "cuda") if a.runtime == "all" else ("core",):
            if not os.path.isdir(os.path.join(rt, comp)):
                sys.exit(f"нет {os.path.join(rt, comp)}: сначала packaging/windows/build-runtime.py --cuda")
            shutil.copytree(os.path.join(rt, comp), os.path.join(APP, "runtime"), dirs_exist_ok=True)
        if a.runtime == "all":
            with open(os.path.join(SRC, "streamtts", "downloads.json"), encoding="utf-8") as f:
                version = json.load(f)["cuda"]["windows"]["version"]
            with open(os.path.join(APP, "runtime", "cuda-version.txt"), "w", encoding="utf-8") as f:
                f.write(version)
    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(APP) for f in fs)
    print(f"Собрано: {APP} ({size / 2**20:.0f} МБ)")

    if a.setup:
        subprocess.run([iscc(), *iscc_defines(), os.path.join(HERE, "StreamTTS.iss")], check=True)


if __name__ == "__main__":
    main()
