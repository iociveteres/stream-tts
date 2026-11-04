"""Закрепить загрузки ESpeech (раздел espeech в stream-tts/streamtts/downloads.json) и списки заглушек библиотек CUDA.

usage: python packaging/pin-espeech.py [--what windows,linux,model] [--torch-wheel PATH] [--linux-torch-wheel PATH]
                                       [--checkpoint PATH] [--cache DIR] [--no-stubs]

stream-tts качает ESpeech сам по кнопке на вкладке «Сервер» (engines/espeech/download.py) — программа для Windows и
образ Docker одним кодом, всё из первоисточников и только закреплённое здесь (docs/espeech.md, «Поставка»):
- рантайм, свой у каждой системы (runtime.windows, runtime.linux): Python 3.14.7 (встраиваемый с python.org;
  python-build-standalone для Linux), колёса PACKAGES (PyPI: cp314 / abi3 / py3 под win_amd64 или manylinux x86_64)
  и нужные файлы колеса torch (download.pytorch.org) — диапазонами (partzip.py). Библиотеки CUDA, которые torch
  загружает, а синтезу и обучению LoRA они не нужны, заменяются заглушками (STUBS), а cuBLAS (на Linux и cudart)
  torch берёт настоящий из CUDA cosyvoice-server; заглушки CUBLAS — только для компьютеров без CUDA (синтез на
  процессоре). На Windows эти DLL лежат в колесе и исключаются из загрузки (TORCH_EXCLUDE; ~430 МБ из 1,9 ГБ;
  samples/espeech-lean), на Linux их в колесе нет (они в пакетах nvidia-*, ~3 ГБ, не качаются;
  samples/espeech-lean-linux);
- модель: исходные файлы из espeech/prepare.py (SOURCES) на закреплённых ревизиях Hugging Face; из чекпойнта ESpeech
  (2,7 ГБ) — только EMA-копия весов (prepare.ema_members), тоже диапазонами.
--what — что закреплять (по умолчанию всё); остальное в downloads.json остаётся как есть. Для частичных архивов
(колёса torch, чекпойнт) нужен весь файл: --torch-wheel, --linux-torch-wheel, --checkpoint (иначе качаются в
--cache, по умолчанию packaging/windows/build/cache).

Заглушки (--no-stubs — не переписывать):
- Windows: экспорт настоящих DLL (из колеса torch и архива cudart llama.cpp, его кладёт в кеш
  packaging/windows/build-runtime.py --cuda) → packaging/windows/stubs/<dll>.def; все функции — stub_fail (вернуть 1:
  у CUDA-библиотек это «не инициализировано», вызов даст ошибку torch, а не тихий мусор), *String/*Name — stub_str,
  данные — нулевые переменные. Собирает build-runtime.py (MSVC).
- Linux: символы, которые библиотеки torch берут из каждой заглушённой (неопределённые символы с версией из
  .gnu.version_r, без версии — по префиксу UNVERSIONED), → packaging/linux/stubs[/cublas]/<soname>.map — скрипт
  версий ld (версии символов те же, что у настоящих библиотек, иначе ld.so на каждой загрузке пишет «no version
  information available»); в заголовке — код ошибки stub_fail. Собирает packaging/linux/build-stubs.sh (стадия
  stubs Dockerfile). Заодно проверяется, что всё, что оставшиеся библиотеки torch требуют (DT_NEEDED), есть в torch,
  в системе (SYSTEM) или среди заглушек: новая зависимость в новой версии torch остановит закрепление.
Нужен только Python 3 (стандартная библиотека). Запускать при смене версий в PACKAGES, TORCH, PYTHON или SOURCES."""
import argparse, collections, fnmatch, hashlib, importlib.util, json, os, re, struct, sys, urllib.parse, \
    urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOWNLOADS = os.path.join(ROOT, "stream-tts", "streamtts", "downloads.json")
CACHE = os.path.join(HERE, "windows", "build", "cache")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prepare = _load("espeech_prepare", os.path.join(ROOT, "espeech", "prepare.py"))
partzip = _load("partzip", os.path.join(ROOT, "stream-tts", "streamtts", "partzip.py"))

# Python 3.14 — как у stream-tts и в образе Docker (Ubuntu 26.04): одна версия на всех системах; под cp314 у всех
# PACKAGES и у torch есть колёса и win_amd64, и manylinux x86_64
PY = (3, 14)
# Пакеты espeech-server (espeech/requirements.txt) и сборки модели (prepare.py: onnx с protobuf и ml_dtypes); что
# нужно torch при импорте — typing_extensions; обучению LoRA — sympy с mpmath: torch.optim (add_param_group, step и
# др. под torch._disable_dynamo) при первом вызове импортирует torch._dynamo, а тот — sympy (+81 МБ uss, ~1 с, только
# на обучении). Остальные зависимости torch (networkx, jinja2, filelock, fsspec, setuptools) на этом пути не
# импортируются. Ставятся распаковкой колеса, без pip и без зависимостей.
PACKAGES = ["numpy==2.4.6", "safetensors==0.8.0", "onnxruntime==1.31.0", "tokenizers==0.23.3", "razdel==0.5.0",
            "psutil==7.2.2", "ruaccent==1.5.8.3", "typing_extensions==4.16.0", "onnx==1.23.2", "protobuf==7.36.2",
            "ml_dtypes==0.6.0", "sympy==1.14.0", "mpmath==1.3.0"]
L = "torch/lib/"

# --- Windows ---
WIN_PYTHON = "https://www.python.org/ftp/python/3.14.7/python-3.14.7-embed-amd64.zip"
WIN_TORCH = "https://download.pytorch.org/whl/cu132/torch-2.14.1%2Bcu132-cp314-cp314-win_amd64.whl"
WIN_STUBS = ["cudnn64_9.dll", "cusparse64_12.dll", "cusolver64_12.dll", "nvrtc64_130_0.dll", "cufft64_12.dll"]
WIN_CUBLAS = ["cublas64_13.dll", "cublasLt64_13.dll"]   # заглушки только без CUDA; с CUDA — настоящие из рантайма
WIN_EXCLUDE = [
    # не грузятся статически и синтезу не нужны
    *(L + m for m in ("curand64*", "cusolverMg*", "cufftw*", "nvperf_host*", "nvJitLink*", "*.lib", "*.h")),
    "torch/include/*", "torch/bin/*", "torch/share/*",
    # cuDNN выключен (server.py): от него нужна только заглушка cudnn64_9
    *(L + m for m in ("cudnn*", "cusparse*", "cusolver*", "nvrtc*", "cufft*")),
    # cuBLAS — из CUDA cosyvoice-server; cudart torch связан статически
    *(L + m for m in ("cublas*", "cudart*")),
]

# --- Linux (образ Docker на Ubuntu 26.04, glibc 2.43) ---
# python-build-standalone: тот же CPython 3.14.7, что встраиваемый на Windows, переносимый (папку можно перенести) и
# свой, а не из образа: обновление образа не ломает скачанный рантайм; stripped — без отладочных символов
LINUX_PYTHON = ("https://github.com/astral-sh/python-build-standalone/releases/download/20260929/"
                "cpython-3.14.7%2B20260929-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz")
LINUX_TORCH = "https://download.pytorch.org/whl/cu132/torch-2.14.1%2Bcu132-cp314-cp314-manylinux_2_28_x86_64.whl"
GLIBC = (2, 43)   # manylinux_2_X: X <= glibc образа
# Из архива Python не нужно: tcl/tk, terminfo, заголовки, pip (рантайм ставится без него), IDLE; python3.14 собран
# статически, libpython3.14.so ему не нужна (−90 МБ из 128)
LINUX_PYTHON_EXCLUDE = [
    *("python/" + m for m in ("share/*", "include/*", "bin/idle*", "bin/pip*", "bin/pydoc*", "bin/*-config",
                              "lib/pkgconfig/*", "lib/libpython3*", "lib/libtcl*", "lib/libtk*", "lib/tcl*", "lib/tk*",
                              "lib/itcl*", "lib/thread*")),
    *(f"python/lib/python{PY[0]}.{PY[1]}/" + m for m in ("site-packages/*", "ensurepip/*", "idlelib/*", "tkinter/*",
                                                       "turtledemo/*", "lib-dynload/_tkinter*")),
]
# Библиотеки, которые torch/lib требует (DT_NEEDED), а синтезу и обучению LoRA они не нужны: cuDNN выключен, STFT на
# процессоре (cuFFT, NVRTC), остальное — линейная алгебра, распределённое обучение, профилировщик, GPUDirect Storage,
# NVSHMEM; cuRAND torch не вызывает вовсе (заглушка — пустая библиотека с тем же именем)
LINUX_STUBS = ["libcudnn.so.9", "libcusparse.so.12", "libcufft.so.12", "libnvrtc.so.13", "libcupti.so.13",
               "libnccl.so.2", "libcusparseLt.so.0", "libcufile.so.0", "libcurand.so.10", "libnvshmem_host.so.3"]
# Из CUDA cosyvoice-server (/opt/cosyvoice образа stream-tts:cuda, релиз llama.cpp); заглушки — в образе без CUDA.
# Порядок — порядок загрузки: cuBLAS требует cuBLASLt
LINUX_CUBLAS = ["libcudart.so.13", "libcublasLt.so.13", "libcublas.so.13"]
# Без версий символов: чьи они — по префиксу. Символы драйвера (cu[A-Z]…, libcuda.so.1) берёт только
# libcaffe2_nvrtc.so, а её torch грузит лениво, только для NVRTC (jiterator)
UNVERSIONED = {"libnccl.so.2": "nccl", "libcusparseLt.so.0": "cusparseLt", "libcufile.so.0": "cuFile",
               "libnvshmem_host.so.3": "nvshmem"}
# Код ошибки stub_fail: 1 — «не инициализировано» у cuBLAS, cuSPARSE, cuFFT и др.; у cudart — cudaErrorNoDevice:
# torch.cuda.is_available() без CUDA тогда просто False, без предупреждения
FAIL = {"libcudart.so.13": 100}
SYSTEM = ("libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0", "librt.so.1", "libstdc++.so.6", "libgcc_s.so.1",
          "ld-linux-x86-64.so.2", "libcuda.so.1")   # libcuda — драйвер NVIDIA (только libcaffe2_nvrtc.so)
LINUX_EXCLUDE = [
    # torch/bin/torch_shm_manager остаётся: import torch на Linux проверяет, что он есть
    "torch/include/*", "torch/bin/protoc*", "torch/share/*", L + "libshm/*", L + "libshm_windows/*",
    # cuSOLVER и MAGMA: torch грузит её лениво, при линейной алгебре на GPU (BUILD_LAZY_CUDA_LINALG)
    L + "libtorch_cuda_linalg.so",
]

PLATFORMS = dict(
    windows=dict(python=WIN_PYTHON, torch=WIN_TORCH, exclude=WIN_EXCLUDE, stubs=WIN_STUBS, cublas=WIN_CUBLAS),
    linux=dict(python=LINUX_PYTHON, python_exclude=LINUX_PYTHON_EXCLUDE, torch=LINUX_TORCH, exclude=LINUX_EXCLUDE,
               stubs=LINUX_STUBS, cublas=LINUX_CUBLAS),
)


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "stream-tts-build"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def download(url, cache):
    dst = os.path.join(cache, urllib.parse.unquote(url.rsplit("/", 1)[-1]))
    if not os.path.isfile(dst):
        print("Качаю", url, flush=True)
        req = urllib.request.Request(url, headers={"User-Agent": "stream-tts-build"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dst + ".part", "wb") as f:
            while b := r.read(1 << 20):
                f.write(b)
        os.replace(dst + ".part", dst)
    return dst


def _fits(filename, system):
    """Колесо годится для Python PY на system: (приоритет, …) или None. Порядок: cp314-cp314, abi3, py3."""
    m = re.match(r".+?-(?P<py>[^-]+)-(?P<abi>[^-]+)-(?P<plat>[^-]+)\.whl$", filename)
    if not m:
        return None
    py, abi, plats = m["py"], m["abi"], m["plat"].split(".")
    cp = f"cp{PY[0]}{PY[1]}"
    if system == "windows":
        ok = "win_amd64" in plats
    else:
        def linux(p):
            g = re.fullmatch(r"manylinux_(\d+)_(\d+)_x86_64", p)
            return (bool(g) and (int(g[1]), int(g[2])) <= GLIBC) or p in ("manylinux2014_x86_64",
                                                                           "manylinux2010_x86_64",
                                                                           "manylinux1_x86_64")
        ok = any(map(linux, plats))
    if plats == ["any"]:
        ok = True
    if not ok:
        return None
    if py == cp and abi == cp:
        return 0
    if abi == "abi3" and (g := re.fullmatch(r"cp3(\d+)", py)) and int(g[1]) <= PY[1]:
        return 1
    if abi == "none" and plats == ["any"] and py in ("py3", "py2.py3"):
        return 2
    return None


def wheel(req, system):
    """Колесо пакета под Python PY и system: {url, size, sha256}."""
    name, version = req.split("==")
    d = json.loads(get(f"https://pypi.org/pypi/{name}/{version}/json"))
    files = sorted((rank, u["filename"], u) for u in d["urls"] if u["packagetype"] == "bdist_wheel"
                   and (rank := _fits(u["filename"], system)) is not None)
    if not files:
        sys.exit(f"{req}: нет колеса под Python {PY[0]}.{PY[1]} {system}")
    u = files[0][2]
    return dict(url=u["url"], size=u["size"], sha256=u["digests"]["sha256"])


def partial(path, url, spec):
    """Описание части zip: хвост (центральный каталог) и digest выбранных файлов полного архива."""
    size = os.path.getsize(path)
    with zipfile.ZipFile(path) as z:
        tail, infos = z.start_dir, z.infolist()   # start_dir — начало центрального каталога
    with open(path, "rb") as f:
        f.seek(tail)
        tail_sha = hashlib.sha256(f.read()).hexdigest()
    out = dict(url=url, size=size, tail=tail, tail_sha256=tail_sha, **spec)
    out["digest"] = partzip.digest_of(path, out)
    sel = partzip.selected(out, infos)
    ranges = partzip.ranges_of(infos, sel, tail)   # то же разбиение на диапазоны, что у программы
    out["unpacked"] = sum(i.file_size for i in sel)
    out["download"] = partzip.download_size(out, ranges)
    print(f"{os.path.basename(path)}: {len(sel)} файлов, качается {out['download'] / 2**20:.0f} из "
          f"{size / 2**20:.0f} МБ ({len(ranges)} диапазонов), на диске {out['unpacked'] / 2**20:.0f} МБ")
    return out


def hf_files():
    """Исходные файлы модели: {url, path (<репозиторий>/<путь>), size, sha256}. LFS — по API Hugging Face (oid —
    sha256), мелкие обычные файлы — скачиванием."""
    out, by_repo = [], collections.defaultdict(list)
    for repo, rev, path in prepare.sources():
        by_repo[(repo, rev)].append(path)
    for (repo, rev), paths in by_repo.items():
        body = urllib.parse.urlencode([("paths", p) for p in paths]).encode()
        req = urllib.request.Request(f"https://huggingface.co/api/models/{repo}/paths-info/{rev}", data=body,
                                     headers={"User-Agent": "stream-tts-build"})
        with urllib.request.urlopen(req, timeout=60) as r:
            info = {i["path"]: i for i in json.load(r)}
        for p in paths:
            i = info[p]
            url = f"https://huggingface.co/{repo}/resolve/{rev}/{p}"
            sha = (i.get("lfs") or {}).get("oid") or hashlib.sha256(get(url)).hexdigest()
            out.append(dict(url=url, path=f"{repo}/{p}", size=i["size"], sha256=sha))
    return out


# --- экспорт DLL (PE32+) ---
def pe_exports(data):
    """[(имя, данные ли)] экспорта DLL: данные — экспорт не в исполняемой секции (переменные)."""
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    assert data[pe:pe + 4] == b"PE\0\0"
    nsec, opt_size = struct.unpack_from("<H", data, pe + 6)[0], struct.unpack_from("<H", data, pe + 20)[0]
    opt = pe + 24
    assert struct.unpack_from("<H", data, opt)[0] == 0x20B, "не PE32+"
    exp_rva, exp_size = struct.unpack_from("<II", data, opt + 112)
    secs = []
    for i in range(nsec):
        s = opt + opt_size + 40 * i
        vsize, va, rawsize, raw = struct.unpack_from("<IIII", data, s + 8)
        secs.append((va, max(vsize, rawsize), raw, struct.unpack_from("<I", data, s + 36)[0]))

    def sec(rva):
        return next(s for s in secs if s[0] <= rva < s[0] + s[1])

    def off(rva):
        va, _, raw, _ = sec(rva)
        return rva - va + raw

    def cstr(rva):
        o = off(rva)
        return data[o:data.index(b"\0", o)].decode()

    e = off(exp_rva)
    nnames, funcs, names, ords = struct.unpack_from("<IIII", data, e + 24)
    out = []
    for i in range(nnames):
        name = cstr(struct.unpack_from("<I", data, off(names) + 4 * i)[0])
        ordinal = struct.unpack_from("<H", data, off(ords) + 2 * i)[0]
        rva = struct.unpack_from("<I", data, off(funcs) + 4 * ordinal)[0]
        forwarder = exp_rva <= rva < exp_rva + exp_size
        out.append((name, not forwarder and not sec(rva)[3] & 0x20000000))   # IMAGE_SCN_MEM_EXECUTE
    return out


def write_def(stub_dir, dll, data):
    exports = pe_exports(data)
    lines = [f"; Заглушка {dll}: экспорт настоящей DLL (packaging/pin-espeech.py), сборка — build-runtime.py",
             f"LIBRARY {dll}", "EXPORTS"]
    n = 0
    for name, is_data in exports:
        if is_data:
            lines.append(f"  {name}=stub_data{n} DATA")
            n += 1
        else:
            lines.append(f"  {name}={'stub_str' if name.endswith(('String', 'Name')) else 'stub_fail'}")
    with open(os.path.join(stub_dir, dll[:-4] + ".def"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"{dll}: {len(exports) - n} функций, {n} данных")


# --- ELF (x86-64): зависимости и неопределённые символы ---
def elf_info(data):
    """(DT_NEEDED, [(имя, тип символа, файл версии | None, версия | None)] неопределённых символов)."""
    assert data[:4] == b"\x7fELF" and data[4] == 2, "не ELF64"
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
    # (name, type, flags, addr, offset, size, link, info, align, entsize)
    secs = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]
    first = {}
    for s in secs:
        first.setdefault(s[1], s)
    dynsym, dynamic = first[11], first[6]                                # SHT_DYNSYM, SHT_DYNAMIC
    versym, verneed = first.get(0x6fffffff), first.get(0x6ffffffe)      # SHT_GNU_versym, SHT_GNU_verneed

    def cstr(tab, off):
        o = tab[4] + off
        return data[o:data.index(b"\0", o)].decode()

    dstr = secs[dynamic[6]]
    needed = []
    for i in range(dynamic[5] // 16):
        tag, val = struct.unpack_from("<qQ", data, dynamic[4] + 16 * i)
        if tag == 0:
            break
        if tag == 1:   # DT_NEEDED
            needed.append(cstr(dstr, val))
    versions = {}
    if verneed:
        vstr, off = secs[verneed[6]], verneed[4]
        for _ in range(verneed[7]):
            _, cnt, file, aux, nxt = struct.unpack_from("<HHIII", data, off)
            a = off + aux
            for _ in range(cnt):
                _, _, other, name, anext = struct.unpack_from("<IHHII", data, a)
                versions[other] = (cstr(vstr, file), cstr(vstr, name))
                a += anext
            off += nxt
    strtab, syms = secs[dynsym[6]], []
    for i in range(1, dynsym[5] // 24):
        st_name, info, _, shndx, _, _ = struct.unpack_from("<IBBHQQ", data, dynsym[4] + 24 * i)
        if shndx:
            continue
        ver = struct.unpack_from("<H", data, versym[4] + 2 * i)[0] & 0x7fff if versym else 0
        file, vname = versions.get(ver, (None, None))
        syms.append((cstr(strtab, st_name), {1: "OBJECT", 2: "FUNC"}.get(info & 0xf, "NOTYPE"), file, vname))
    return needed, syms


def linux_stub_maps(whl, stub_dir):
    """packaging/linux/stubs[/cublas]/<soname>.map по библиотекам torch, которые остаются в рантайме."""
    p = PLATFORMS["linux"]
    stubbed = p["stubs"] + p["cublas"]
    want = collections.defaultdict(dict)   # soname -> {символ: версия | None}
    with zipfile.ZipFile(whl) as z:
        libs = [i.filename for i in z.infolist() if i.filename.startswith(L) and ".so" in i.filename
                and not any(fnmatch.fnmatch(i.filename.lower(), m.lower()) for m in p["exclude"])]
        own = {n[len(L):] for n in libs}
        for name in libs:
            needed, syms = elf_info(z.read(name))
            extra = [n for n in needed if n not in own and n not in SYSTEM and n not in stubbed]
            if extra:
                sys.exit(f"{name} требует {', '.join(extra)}: добавьте в LINUX_STUBS, LINUX_CUBLAS или SYSTEM")
            for sym, typ, file, ver in syms:
                lib = file if file in stubbed else next((s for s, pre in UNVERSIONED.items()
                                                         if file is None and sym.startswith(pre)), None)
                if lib is None:
                    continue
                if typ == "OBJECT":
                    sys.exit(f"{name}: {sym} из {lib} — данные, заглушка их не умеет")
                want[lib][sym] = ver
    for lib in stubbed:
        syms = want.get(lib, {})
        # одна версия на библиотеку; ссылка без версии (слабая nvshmem_init) находит и символ с версией
        versions = set(syms.values()) - {None}
        assert len(versions) <= 1, (lib, versions)
        ver = versions.pop() if versions else None
        d = os.path.join(stub_dir, *(["cublas"] if lib in p["cublas"] else []))
        os.makedirs(d, exist_ok=True)
        lines = [f"/* Заглушка {lib} для torch (packaging/pin-espeech.py): символы, которые берут из неё библиотеки "
                 f"torch; сборка — packaging/linux/build-stubs.sh. fail={FAIL.get(lib, 1)} */",
                 f"{ver or ''} {{".lstrip()]
        if syms:
            lines += ["  global:", *(f"    {s};" for s in sorted(syms))]
        lines += ["  local: *;", "};"]
        with open(os.path.join(d, lib + ".map"), "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines) + "\n")
        print(f"{lib}: {len(syms)} функций" + (f", версия {ver}" if ver else ""))


def pin_runtime(system, py, whl):
    p = PLATFORMS[system]
    python = dict(url=p["python"], size=os.path.getsize(py), sha256=sha256(py))
    if p.get("python_exclude"):   # архив tar.gz: распаковывается без них (download.py); у Windows — zip целиком
        python["exclude"] = p["python_exclude"]
    return dict(python=python,
                wheels=[wheel(r, system) for r in PACKAGES],
                torch=partial(whl, p["torch"], dict(exclude=p["exclude"])),
                stubs=p["stubs"], cublas=p["cublas"])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--what", default="windows,linux,model", help="что закреплять: windows, linux, model")
    ap.add_argument("--torch-wheel", help="колесо torch для Windows целиком (иначе качается в --cache, 1,9 ГБ)")
    ap.add_argument("--linux-torch-wheel", help="колесо torch для Linux целиком (иначе качается в --cache, 533 МБ)")
    ap.add_argument("--checkpoint", help="espeech_tts_256k.pt целиком (иначе качается в --cache, 2,7 ГБ)")
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--no-stubs", action="store_true", help="не переписывать списки заглушек")
    a = ap.parse_args()
    what = set(a.what.split(","))
    if what - {"windows", "linux", "model"}:
        sys.exit(f"--what: {a.what}")
    os.makedirs(a.cache, exist_ok=True)
    with open(DOWNLOADS, encoding="utf-8") as f:
        dl = json.load(f)
    esp = dl.setdefault("espeech", {})
    runtime = esp.setdefault("runtime", {})

    if "windows" in what:
        py = download(WIN_PYTHON, a.cache)
        whl = a.torch_wheel or download(WIN_TORCH, a.cache)
        runtime["windows"] = pin_runtime("windows", py, whl)
        if not a.no_stubs:
            stub_dir = os.path.join(HERE, "windows", "stubs")
            os.makedirs(stub_dir, exist_ok=True)
            with zipfile.ZipFile(whl) as z:
                for dll in WIN_STUBS:
                    write_def(stub_dir, dll, z.read(L + dll))
            cudart = os.path.join(a.cache, dl["cuda"]["archives"][1]["url"].rsplit("/", 1)[-1])   # cuBLAS той же CUDA
            if not os.path.isfile(cudart):
                sys.exit(f"нет {cudart}: сначала packaging/windows/build-runtime.py --cuda")
            with zipfile.ZipFile(cudart) as z:
                for dll in WIN_CUBLAS:
                    write_def(stub_dir, dll, z.read(dll))
    if "linux" in what:
        py = download(LINUX_PYTHON, a.cache)
        whl = a.linux_torch_wheel or download(LINUX_TORCH, a.cache)
        runtime["linux"] = pin_runtime("linux", py, whl)
        if not a.no_stubs:
            linux_stub_maps(whl, os.path.join(HERE, "linux", "stubs"))
    if "model" in what:
        files = hf_files()
        ck_src = next(f for f in files if f["path"].endswith("/" + prepare.CHECKPOINT))
        ck = a.checkpoint or download(ck_src["url"], a.cache)
        if sha256(ck) != ck_src["sha256"]:
            sys.exit(f"{ck}: sha256 не совпадает с закреплённой ревизией {prepare.ESPEECH[1]}")
        esp["model"] = dict(files=[f for f in files if f["path"] != ck_src["path"]],
                            checkpoint=dict(partial(ck, ck_src["url"], dict(members=prepare.ema_members(ck))),
                                            path=ck_src["path"]))

    dl["espeech"] = dict(runtime={k: runtime[k] for k in ("windows", "linux") if k in runtime}, model=esp["model"])
    with open(DOWNLOADS, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dl, f, ensure_ascii=False, indent=1)
        f.write("\n")
    size = lambda x: x["size"]
    for system, rt in dl["espeech"]["runtime"].items():
        n = rt["python"]["size"] + sum(map(size, rt["wheels"])) + rt["torch"]["download"]
        print(f"Рантайм {system}: {n / 2**20:.0f} МБ")
    m = dl["espeech"]["model"]
    print(f"Модель: {(sum(map(size, m['files'])) + m['checkpoint']['download']) / 2**20:.0f} МБ; "
          f"закреплено в {DOWNLOADS}")


if __name__ == "__main__":
    main()
