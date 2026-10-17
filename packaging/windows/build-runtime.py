"""Рантайм stream-tts для Windows: cosyvoice-server с DLL, ffmpeg и VC++ Redistributable.

usage: python packaging/windows/build-runtime.py [--out DIR] [--cache DIR] [--flat DIR]

Скачивает закреплённые версии (ASSETS, с проверкой sha256) и раскладывает по компонентам установщика:
  <out>/core/   cosyvoice-server и cosyvoice-cli, ONNX Runtime, GGML (CPU и Vulkan), ICU, FFmpeg (DLL и ffmpeg.exe)
  <out>/cuda/   ggml-cuda.dll и рантайм CUDA 13 (cudart, cuBLAS) — отдельный компонент, ~660 МБ
  <out>/vc_redist.x64.exe   VC++ 2015–2022: его требуют cosyvoice и ONNX Runtime (MSVCP140)
--flat DIR собирает core и cuda в одну папку: так выглядит runtime/ в корне проекта для запуска stream-tts из
исходников в режиме managed (docs/stream-tts.md). По умолчанию out — packaging/windows/build/runtime, загрузки
кешируются в packaging/windows/build/cache. Нужен только Python 3 (стандартная библиотека).

Версии проверены 17.10.2026 на RTX 3060 Ti (docs/stream-tts.md, «Развёртывание»): GGML в релизах cosyvoice.cpp не
поставляется, его берут из llama.cpp; общие файлы GGML во всех архивах llama.cpp одинаковы, поэтому ядро берётся
из Vulkan-архива (в нём есть и все ggml-cpu-*), а из CUDA-архива — только ggml-cuda.dll. Сборки FFmpeg у BtbN
со временем удаляются: если ссылка пропала, взять свежую autobuild-* n8.1 lgpl-shared и обновить sha256.
"""
import argparse, hashlib, os, shutil, sys, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(HERE, "build")

GH = "https://github.com"
LLAMA = f"{GH}/ggml-org/llama.cpp/releases/download/b11500"
ASSETS = {
    "cosyvoice": (f"{GH}/Lourdle/cosyvoice.cpp/releases/download/v0.2.0/cosyvoice-67d45fe-windows-x64-ffmpeg.zip",
                  "6667b0bc30fcf13d595feb1eff8114a8234186f625486d47cb4efb8e63e0f91a"),
    "vulkan": (f"{LLAMA}/llama-b11500-bin-win-vulkan-x64.zip",
               "aff80157bf74a2f9a450e6946a9ce7bea0f4a47e65da1224ee36f7dcb965763e"),
    "cuda": (f"{LLAMA}/llama-b11500-bin-win-cuda-13.4-x64.zip",
             "acc9413b9a1af8db6698b032e316297a2446056d7e7799e06304145dfa6e5b5e"),
    "cudart": (f"{LLAMA}/cudart-llama-bin-win-cuda-13.4-x64.zip",
               "738f8c251ac22b70c3ae6f83a10cf222725df0395246a2cf58f32bdb85fbe668"),
    "icu": (f"{GH}/unicode-org/icu/releases/download/release-78.3/icu4c-78.3-Win64-MSVC2022.zip",
            "446b671f9437227daa79e221d4521d75793f9ecd65ac44c06e34dd848f201ac2"),
    "ffmpeg": (f"{GH}/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-16-13-04/"
               "ffmpeg-n8.1.3-18-g46d899d7a0-win64-lgpl-shared-8.1.zip",
               "35a7bd207e5dd6c3d1acbe22ed81ae195f081d3527e58f556b15468400252239"),
    # Последняя поддерживаемая версия (aka.ms/vs/17 отдаёт старую 14.44, а cosyvoice собран тулсетом 14.51 и со
    # старым рантаймом не запустится). sha256 не закрепить; файл подписан Microsoft, версию проверяет установщик.
    "vc_redist": ("https://aka.ms/vc14/vc_redist.x64.exe", None),
}

# (архив, имена файлов в нём — без пути; * в конце — по префиксу), компонент
LAYOUT = [
    ("cosyvoice", ["cosyvoice-server.exe", "cosyvoice-cli.exe", "cosyvoice.dll", "onnxruntime.dll",
                   "onnxruntime_providers_shared.dll"], "core"),
    ("vulkan", ["ggml.dll", "ggml-base.dll", "ggml-cpu-*", "libomp.dll", "ggml-vulkan.dll", "LICENSE-LLVM-OpenMP"], "core"),
    ("icu", ["icudt78.dll", "icuuc78.dll", "icuin78.dll"], "core"),
    # avdevice, avfilter, swscale нужны только ffmpeg.exe (stream-tts: mp3 примеров, эффекты)
    ("ffmpeg", ["avcodec-62.dll", "avformat-62.dll", "avutil-60.dll", "swresample-6.dll", "avdevice-62.dll",
                "avfilter-11.dll", "swscale-9.dll", "ffmpeg.exe"], "core"),
    ("cuda", ["ggml-cuda.dll"], "cuda"),
    ("cudart", ["cudart64_13.dll", "cublas64_13.dll", "cublasLt64_13.dll"], "cuda"),
]
LICENSES = {"icu": "LICENSE", "ffmpeg": "LICENSE.txt"}  # кладутся в core/licenses/<архив>-<файл>


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def fetch(name, cache):
    url, want = ASSETS[name]
    dst = os.path.join(cache, url.rsplit("/", 1)[-1])
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


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", default=os.path.join(BUILD, "runtime"))
    ap.add_argument("--cache", default=os.path.join(BUILD, "cache"))
    ap.add_argument("--flat", help="ещё и собрать core и cuda в одну папку (runtime/ для запуска из исходников)")
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)
    files = {name: fetch(name, a.cache) for name in ASSETS}

    # ggml-cuda.dll собран вместе с ggml-base.dll своего архива: убедиться, что ядро из Vulkan-архива то же самое
    if zip_member_sha(files["vulkan"], "ggml-base.dll") != zip_member_sha(files["cuda"], "ggml-base.dll"):
        sys.exit("ggml-base.dll в Vulkan- и CUDA-архивах llama.cpp различаются: брать ядро из CUDA-архива")

    for comp in ("core", "cuda"):
        shutil.rmtree(os.path.join(a.out, comp), ignore_errors=True)
    for name, patterns, comp in LAYOUT:
        extract(files[name], patterns, os.path.join(a.out, comp))
    lic = os.path.join(a.out, "core", "licenses")
    os.makedirs(lic, exist_ok=True)
    for name, base in LICENSES.items():
        extract(files[name], [base], lic)
        os.replace(os.path.join(lic, base), os.path.join(lic, f"{name}-{base}"))
    shutil.move(os.path.join(a.out, "core", "LICENSE-LLVM-OpenMP"), os.path.join(lic, "LICENSE-LLVM-OpenMP"))
    shutil.copy(os.path.join(HERE, "THIRD-PARTY.txt"), lic)
    shutil.copy(files["vc_redist"], os.path.join(a.out, "vc_redist.x64.exe"))

    for comp in ("core", "cuda"):
        d = os.path.join(a.out, comp)
        size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(d) for f in fs)
        print(f"{comp}: {len(os.listdir(d))} файлов, {size / 2**20:.0f} МБ")

    if a.flat:
        os.makedirs(a.flat, exist_ok=True)
        for comp in ("core", "cuda"):
            shutil.copytree(os.path.join(a.out, comp), a.flat, dirs_exist_ok=True)
        print(f"Собрано в {a.flat}")


if __name__ == "__main__":
    main()
