"""Рантайм stream-tts для Windows: cosyvoice-server с DLL, ffmpeg и рантаймом VC++, код espeech-server; по желанию —
файлы CUDA.

usage: python packaging/windows/build-runtime.py [--out DIR] [--cache DIR] [--crt DIR] [--cuda] [--flat DIR]

Скачивает закреплённые версии (ASSETS, с проверкой sha256) и раскладывает:
  <out>/core/   cosyvoice-server и cosyvoice-cli, ONNX Runtime, GGML (CPU и Vulkan), ICU, FFmpeg (DLL и ffmpeg.exe),
                DLL рантайма VC++ (CRT) — всё, что установщик кладёт в {app}\runtime
  <out>/core/licenses/   тексты лицензий: из архивов (LICENSES) и из репозиториев тех же версий (LICENSE_URLS)
  <out>/core/espeech/    espeech-server без Python и torch: пакет espeech/ (код проекта) и DLL-заглушки stubs/ из
                packaging/windows/stubs/*.def (их пишет pin-espeech.py; сборка — MSVC, нужен C++ из Visual Studio или
                Build Tools). Python, пакеты и torch программа качает сама по кнопке на вкладке «Сервер»
                (engines/espeech/download.py, закреплено в downloads.json, раздел espeech) в папку espeech-runtime.
  --cuda: <out>/cuda/   ggml-cuda.dll и рантайм CUDA 13 (cudart, cuBLAS), ~660 МБ — для build-exe.py --runtime all.
                В установщик CUDA не входит: вкладка «Сервер» качает те же два архива llama.cpp сама.
                Их адреса, размеры и sha256 — в stream-tts/streamtts/downloads.json (cuda.archives), отсюда же их
                берёт и этот скрипт, так что версия CUDA закреплена в одном месте.
--flat DIR собирает core (без espeech/: из исходников espeech-server запускается из espeech/.venv) и файлы CUDA в одну
папку: так выглядит runtime/ в корне проекта для запуска stream-tts из исходников
(docs/stream-tts-deploy.md). По умолчанию out — packaging/windows/build/runtime, загрузки
кешируются в packaging/windows/build/cache. Нужен только Python 3 (стандартная библиотека).

CRT (msvcp140*, vcruntime140*, concrt140) кладётся рядом с cosyvoice-server.exe, чтобы не ставить VC++
Redistributable с правами администратора: Microsoft разрешает такую поставку (app-local, redist.txt Visual Studio).
cosyvoice собран тулсетом 14.51 и со старым рантаймом не запускается, поэтому берётся самая новая копия не ниже
CRT_MIN: папки Microsoft.VC14*.CRT установленных Visual Studio (VC/Redist/MSVC/<версия>/x64) и System32 (туда
его ставит VC++ Redistributable; на windows-latest в GitHub Actions — VS 2022 с redist 14.44, а в System32 —
14.51.36247), или папка из --crt.

Версии проверены 17.10.2026 на RTX 3060 Ti (docs/stream-tts-deploy.md, «Рантайм и пути»): GGML в релизах cosyvoice.cpp не
поставляется, его берут из llama.cpp; общие файлы GGML во всех архивах llama.cpp одинаковы, поэтому ядро берётся
из Vulkan-архива (в нём есть и все ggml-cpu-*), а из CUDA-архива — только ggml-cuda.dll. Сборки FFmpeg у BtbN
со временем удаляются: если ссылка пропала, взять свежую autobuild-* n8.1 lgpl-shared и обновить sha256.
"""
import argparse, contextlib, glob, hashlib, json, os, shutil, subprocess, sys, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")

GH = "https://github.com"
LLAMA = f"{GH}/ggml-org/llama.cpp/releases/download/b11500"
ASSETS = {
    "cosyvoice": (f"{GH}/Lourdle/cosyvoice.cpp/releases/download/v0.2.0/cosyvoice-67d45fe-windows-x64-ffmpeg.zip",
                  "6667b0bc30fcf13d595feb1eff8114a8234186f625486d47cb4efb8e63e0f91a"),
    "vulkan": (f"{LLAMA}/llama-b11500-bin-win-vulkan-x64.zip",
               "aff80157bf74a2f9a450e6946a9ce7bea0f4a47e65da1224ee36f7dcb965763e"),
    "icu": (f"{GH}/unicode-org/icu/releases/download/release-78.3/icu4c-78.3-Win64-MSVC2022.zip",
            "446b671f9437227daa79e221d4521d75793f9ecd65ac44c06e34dd848f201ac2"),
    "ffmpeg": (f"{GH}/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-16-13-04/"
               "ffmpeg-n8.1.3-18-g46d899d7a0-win64-lgpl-shared-8.1.zip",
               "35a7bd207e5dd6c3d1acbe22ed81ae195f081d3527e58f556b15468400252239"),
}
# Закреплённые загрузки программы и установщика: архивы CUDA (llama.cpp) и модели
ROOT = os.path.dirname(os.path.dirname(HERE))
DOWNLOADS = os.path.join(ROOT, "stream-tts", "streamtts", "downloads.json")
with open(DOWNLOADS, encoding="utf-8") as _f:
    _DL = json.load(_f)
CUDA, ESPEECH = _DL["cuda"]["windows"], _DL["espeech"]["runtime"]["windows"]
# Архивы CUDA — по порядку в downloads.json: llama.cpp (ggml-cuda.dll) и cudart; качаются только для --cuda и --flat
CUDA_ASSETS = ("cuda", "cudart")
ASSETS.update({name: (a["url"], a["sha256"]) for name, a in zip(CUDA_ASSETS, CUDA["archives"])})

# (архив, имена файлов в нём — без пути; * в конце — по префиксу), куда: core или cuda (содержимое zip)
LAYOUT = [
    ("cosyvoice", ["cosyvoice-server.exe", "cosyvoice-cli.exe", "cosyvoice.dll", "onnxruntime.dll",
                   "onnxruntime_providers_shared.dll"], "core"),
    ("vulkan", ["ggml.dll", "ggml-base.dll", "ggml-cpu-*", "libomp.dll", "ggml-vulkan.dll", "LICENSE-LLVM-OpenMP"], "core"),
    ("icu", ["icudt78.dll", "icuuc78.dll", "icuin78.dll"], "core"),
    # avdevice, avfilter, swscale нужны только ffmpeg.exe (stream-tts: mp3 примеров, эффекты)
    ("ffmpeg", ["avcodec-62.dll", "avformat-62.dll", "avutil-60.dll", "swresample-6.dll", "avdevice-62.dll",
                "avfilter-11.dll", "swscale-9.dll", "ffmpeg.exe"], "core"),
    *((name, a["files"], "cuda") for name, a in zip(CUDA_ASSETS, CUDA["archives"])),
]
# DLL рантайма VC++: рантайму нужны MSVCP140, MSVCP140_1, VCRUNTIME140, VCRUNTIME140_1 (dumpbin /dependents), torch
# для ESpeech — ещё MSVCP140_ATOMIC_WAIT и VCRUNTIME140_THREADS (импорт torch_cpu.dll; программа копирует CRT к
# встраиваемому Python), остальные библиотеки C++ из того же набора Microsoft.VC14x.CRT — на случай новых версий
CRT = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll", "msvcp140_codecvt_ids.dll",
       "vcruntime140.dll", "vcruntime140_1.dll", "vcruntime140_threads.dll", "concrt140.dll")
CRT_MIN = (14, 51)
# ggml-cuda.dll собран вместе с ggml-base.dll своей сборки llama.cpp, поэтому CUDA берётся из той же сборки, что
# и ядро, а в её версии (cuda-version.txt) — и CUDA, и сборка
if f"/{LLAMA.rsplit('/', 1)[-1]}/" not in CUDA["archives"][0]["url"] or not CUDA["version"].endswith(LLAMA.rsplit("/", 1)[-1]):
    sys.exit(f"CUDA в downloads.json ({CUDA['version']}) не из сборки llama.cpp ядра ({LLAMA}): обновите cuda там")
LICENSES = {"icu": "LICENSE", "ffmpeg": "LICENSE.txt"}  # кладутся в core/licenses/<архив>-<файл>
# В архивах cosyvoice.cpp и llama.cpp текстов лицензий нет, а MIT/BSD требуют их прикладывать: берём из репозиториев
# тех же версий. cosyvoice.dll статически содержит PCRE2 (подмодуль vendor/pcre2), cosyvoice-server — cpp-httplib и
# nlohmann/json (vendor/, их список — в THIRD_PARTY_NOTICES.md); onnxruntime.dll — 1.30.0 (версия в свойствах DLL),
# со своими сторонними компонентами в ThirdPartyNotices.txt. Имя в core/licenses: (url, sha256).
RAW = "https://raw.githubusercontent.com"
COSY = f"{RAW}/Lourdle/cosyvoice.cpp/v0.2.0"
ORT = f"{RAW}/microsoft/onnxruntime/v1.30.0"
LICENSE_URLS = {
    "cosyvoice.cpp-LICENSE": (f"{COSY}/LICENSE",
                              "1626472fca8e6ff90fa9d1561cff421b8b67f3383d171b76ae93e7ed404a3ab2"),
    "cosyvoice.cpp-THIRD_PARTY_NOTICES.md": (f"{COSY}/THIRD_PARTY_NOTICES.md",
                                             "6a641347ac09a78c925837eb817925f1aea25a7f7684c09b4545f5eb97d21b4e"),
    "cosyvoice.cpp-FFmpeg-NOTICE.md": (f"{COSY}/FFmpeg-NOTICE.md",
                                       "9f3b2c7d1883b0e9e9b10a51a17c4891f5ffa191268c892b59a1d78cb269bb9c"),
    "cpp-httplib-LICENSE": (f"{COSY}/vendor/cpp-httplib/LICENSE",
                            "4b45cbe16d7b71b89ae6127e26e0d90a029198ca5e958ad8e3d0b8bbed364d8b"),
    "nlohmann-json-LICENSE.MIT": (f"{RAW}/nlohmann/json/v3.12.0/LICENSE.MIT",  # версия — в vendor/nlohmann/json.hpp
                                  "46a65cffd1ea955132d95a8dd921640714a8d6b537d2e4e482d31145ae95b603"),
    "pcre2-LICENCE.md": (f"{RAW}/PCRE2Project/pcre2/7978954dbd2efc6f2196869290553cf1871b4ce6/LICENCE.md",
                         "197d8a73ffee0d6b09adba2f9c677b5f5aede24edf89258a68e48248d010d811"),
    "llama.cpp-LICENSE": (f"{RAW}/ggml-org/llama.cpp/b11500/LICENSE",  # GGML (ggml*.dll)
                          "94f29bbed6a22c35b992c5c6ebf0e7c92f13b836b90f36f461c9cf2f0f1d010d"),
    "onnxruntime-LICENSE": (f"{ORT}/LICENSE", "2f07c72751aed99790b8a4869cf2311df85a860b22ded05fa22803587a48922c"),
    "onnxruntime-ThirdPartyNotices.txt": (f"{ORT}/ThirdPartyNotices.txt",
                                          "143764b952fdb1a7c69ce653bfba74a7744d6a8a573bfb73e235fba356c83de3"),
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def fetch(name, cache):
    url, want = ASSETS[name]
    return download(url, want, os.path.join(cache, url.rsplit("/", 1)[-1]), name)


def download(url, want, dst, name):
    if not (os.path.isfile(dst) and (want is None or sha256(dst) == want)):
        print(f"Качаю {url}", flush=True)
        tmp = dst + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": "stream-tts-build"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        got = sha256(tmp)
        if want and got != want:
            os.remove(tmp)
            sys.exit(f"{name}: sha256 {got}, ожидался {want}")
        os.replace(tmp, dst)
    return dst


def extract(archive, patterns, dest):
    """Файлы из zip по имени (путь внутри архива не важен): только перечисленные, без распаковки чужих путей."""
    os.makedirs(dest, exist_ok=True)
    found = set()
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            base = info.filename.rsplit("/", 1)[-1]
            hit = next((p for p in patterns if base == p or (p.endswith("*") and base.startswith(p[:-1]))), None)
            if not hit or info.is_dir():
                continue
            with z.open(info) as src, open(os.path.join(dest, base), "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            found.add(hit)
    missing = set(patterns) - found
    if missing:
        sys.exit(f"{os.path.basename(archive)}: нет {', '.join(sorted(missing))}")


def zip_member_sha(archive, base):
    with zipfile.ZipFile(archive) as z:
        info = next(i for i in z.infolist() if i.filename.rsplit("/", 1)[-1] == base)
        return hashlib.sha256(z.read(info)).hexdigest()


def file_version(path):
    """(major, minor, build, private) из ресурса версии DLL (Windows) или None."""
    import ctypes
    v = ctypes.windll.version
    size = v.GetFileVersionInfoSizeW(path, None)
    if not size:
        return None
    buf = ctypes.create_string_buffer(size)
    ptr, n = ctypes.c_void_p(), ctypes.c_uint()
    if not (v.GetFileVersionInfoW(path, 0, size, buf) and v.VerQueryValueW(buf, "\\", ctypes.byref(ptr), ctypes.byref(n))):
        return None
    ms, ls = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint32))[2:4]  # VS_FIXEDFILEINFO.dwFileVersionMS/LS
    return ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF


def crt_dirs():
    """Папки, где может лежать CRT: Microsoft.VC14*.CRT всех Visual Studio (vswhere) и System32."""
    found, roots = [], []
    vswhere = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Microsoft Visual Studio",
                           "Installer", "vswhere.exe")
    if os.path.isfile(vswhere):
        out = subprocess.run([vswhere, "-all", "-products", "*", "-prerelease", "-property", "installationPath"],
                             capture_output=True, text=True).stdout
        roots = [r.strip() for r in out.splitlines() if r.strip()]
    for root in roots:
        found += glob.glob(os.path.join(root, "VC", "Redist", "MSVC", "*", "x64", "Microsoft.VC14*.CRT"))
    found.append(os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"))
    return found


def find_crt(forced=None):
    """Папка с полным набором CRT самой новой версии (наименьшая из версий файлов) и эта версия."""
    best = None
    for d in [forced] if forced else crt_dirs():
        if not all(os.path.isfile(os.path.join(d, f)) for f in CRT):
            continue
        ver = min(file_version(os.path.join(d, f)) or (0,) for f in CRT)
        if not best or ver > best[1]:
            best = (d, ver)
    if not best or best[1][:2] < CRT_MIN:
        have = f"{best[0]}: {'.'.join(map(str, best[1]))}" if best else "не найден"
        sys.exit(f"нужен рантайм VC++ не ниже {'.'.join(map(str, CRT_MIN))} ({have}): поставьте Visual Studio Build "
                 "Tools 2026 (MSVC 14.51+) или свежий VC++ Redistributable, или укажите папку в --crt")
    return best


def vcvars():
    """vcvars64.bat самой новой Visual Studio с компилятором C++ (vswhere)."""
    vswhere = os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"), "Microsoft Visual Studio",
                           "Installer", "vswhere.exe")
    if os.path.isfile(vswhere):
        out = subprocess.run([vswhere, "-latest", "-products", "*", "-requires",
                              "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-find",
                              r"VC\Auxiliary\Build\vcvars64.bat"], capture_output=True, text=True).stdout.split("\n")
        if out[0].strip():
            return out[0].strip()
    sys.exit("нет MSVC (vcvars64.bat): поставьте Visual Studio или Build Tools с C++ — он нужен для заглушек espeech")


def build_stub(deffile, dst, vcvars_bat, tmp):
    """DLL-заглушка по .def (pin-espeech.py): функции stub_fail и stub_str, данные — нулевые переменные; без CRT."""
    with open(deffile, encoding="utf-8") as f:
        ndata = sum(1 for line in f if line.rstrip().endswith(" DATA"))
    os.makedirs(tmp, exist_ok=True)
    with open(os.path.join(tmp, "stub.c"), "w", encoding="utf-8") as f:
        f.write('static const char msg[] = "stub";\n__int64 stub_fail(void) { return 1; }\n'
                "const char *stub_str(void) { return msg; }\n" +
                "".join(f"__int64 stub_data{i}[4];\n" for i in range(ndata)))
    bat = os.path.join(tmp, "build.bat")
    with open(bat, "w", encoding="utf-8") as f:
        f.write(f'@call "{vcvars_bat}" >nul\r\ncd /d "{tmp}"\r\ncl /nologo /O1 /GS- /c stub.c || exit /b 1\r\n'
                f'link /nologo /DLL /NOENTRY /NODEFAULTLIB /DEF:"{deffile}" /OUT:"{dst}" stub.obj || exit /b 1\r\n')
    p = subprocess.run(["cmd", "/c", bat], capture_output=True, text=True)
    if p.returncode or not os.path.isfile(dst):
        sys.exit(f"заглушка {os.path.basename(dst)} не собрана:\n{p.stdout[-3000:]}{p.stderr[-2000:]}")
    for ext in (".lib", ".exp"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(dst[:-4] + ext)


def build_espeech(core, cache):
    """core/espeech/: пакет espeech (только *.py) и DLL-заглушки; cuBLAS-заглушки — в stubs/cublas/ (без CUDA)."""
    out = os.path.join(core, "espeech")
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "espeech"), os.path.join(out, "espeech"),
                    ignore=lambda d, names: [n for n in names if not n.endswith(".py")])
    shutil.copy(os.path.join(ROOT, "espeech", "THIRD-PARTY.txt"),
                os.path.join(core, "licenses", "espeech-THIRD-PARTY.txt"))
    bat, tmp = vcvars(), os.path.join(cache, "stubs-build")
    for dll in ESPEECH["stubs"] + ESPEECH["cublas"]:
        d = os.path.join(out, "stubs", *(["cublas"] if dll in ESPEECH["cublas"] else []))
        os.makedirs(d, exist_ok=True)
        build_stub(os.path.join(HERE, "stubs", dll[:-4] + ".def"), os.path.join(d, dll), bat, tmp)
    shutil.rmtree(tmp, ignore_errors=True)
    n = sum(len(fs) for _, _, fs in os.walk(out))
    print(f"espeech: {n} файлов (пакет и заглушки {', '.join(ESPEECH['stubs'] + ESPEECH['cublas'])})")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(BUILD, "runtime"))
    ap.add_argument("--cache", default=os.path.join(BUILD, "cache"))
    ap.add_argument("--crt", help="папка с DLL рантайма VC++ (иначе — самая новая из Visual Studio и System32)")
    ap.add_argument("--cuda", action="store_true", help="разложить и файлы CUDA в <out>/cuda")
    ap.add_argument("--flat", help="ещё и собрать core и CUDA в одну папку (runtime/ для запуска из исходников)")
    a = ap.parse_args()
    cuda = a.cuda or bool(a.flat)
    os.makedirs(a.cache, exist_ok=True)
    files = {name: fetch(name, a.cache) for name in ASSETS if cuda or name not in CUDA_ASSETS}
    crt, crt_ver = find_crt(a.crt)
    print(f"CRT {'.'.join(map(str, crt_ver))}: {crt}")

    # ggml-cuda.dll собран вместе с ggml-base.dll своего архива: убедиться, что ядро из Vulkan-архива то же самое
    if cuda and zip_member_sha(files["vulkan"], "ggml-base.dll") != zip_member_sha(files["cuda"], "ggml-base.dll"):
        sys.exit("ggml-base.dll в Vulkan- и CUDA-архивах llama.cpp различаются: брать ядро из CUDA-архива")

    for comp in ("core", "cuda"):
        shutil.rmtree(os.path.join(a.out, comp), ignore_errors=True)
    for name, patterns, comp in LAYOUT:
        if name in files:
            extract(files[name], patterns, os.path.join(a.out, comp))
    for f in CRT:
        shutil.copy(os.path.join(crt, f), os.path.join(a.out, "core", f))
    lic = os.path.join(a.out, "core", "licenses")
    os.makedirs(lic, exist_ok=True)
    for name, base in LICENSES.items():
        extract(files[name], [base], lic)
        os.replace(os.path.join(lic, base), os.path.join(lic, f"{name}-{base}"))
    shutil.move(os.path.join(a.out, "core", "LICENSE-LLVM-OpenMP"), os.path.join(lic, "LICENSE-LLVM-OpenMP"))
    os.makedirs(os.path.join(a.cache, "licenses"), exist_ok=True)
    for base, (url, want) in LICENSE_URLS.items():
        shutil.copy(download(url, want, os.path.join(a.cache, "licenses", base), base), lic)
    with contextlib.suppress(FileNotFoundError):
        os.remove(os.path.join(a.out, "vc_redist.x64.exe"))  # от прошлых сборок: рантайм VC++ теперь в core
    build_espeech(os.path.join(a.out, "core"), a.cache)

    def size(d):
        return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(d) for f in fs)

    core = os.path.join(a.out, "core")
    print(f"core: {len(os.listdir(core))} файлов, {size(core) / 2**20:.0f} МБ")
    for old in glob.glob(os.path.join(a.out, "StreamTTS-cuda-*.zip")):
        os.remove(old)  # от прошлых сборок: свой архив CUDA больше не делается
    if cuda:
        d = os.path.join(a.out, "cuda")
        print(f"cuda: {len(os.listdir(d))} файлов, {size(d) / 2**20:.0f} МБ")
        if size(d) != CUDA["unpacked"] or sorted(os.listdir(d)) != sorted(CUDA["files"]):
            sys.exit(f"файлы CUDA не совпадают с downloads.json (cuda.files, cuda.unpacked = {size(d)})")

    if a.flat:
        os.makedirs(a.flat, exist_ok=True)
        for comp in ("core", "cuda"):
            top = os.path.join(a.out, comp)
            shutil.copytree(top, a.flat, dirs_exist_ok=True,
                            ignore=lambda d, names: ["espeech"] if os.path.normcase(d) == os.path.normcase(top) else [])
        with open(os.path.join(a.flat, "cuda-version.txt"), "w", encoding="utf-8") as f:
            f.write(CUDA["version"])
        print(f"Собрано в {a.flat}")


if __name__ == "__main__":
    main()
