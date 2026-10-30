"""Закрепить загрузки ESpeech (раздел espeech в stream-tts/streamtts/downloads.json) и списки заглушек DLL.

usage: python packaging/windows/pin-espeech.py [--torch-wheel PATH] [--checkpoint PATH] [--cache DIR] [--no-stubs]

Программа качает ESpeech сама по кнопке на вкладке «Сервер» (engines/espeech/download.py), всё из первоисточников и
только закреплённое здесь:
- рантайм: встраиваемый Python (python.org), колёса PACKAGES (PyPI: файлы под cp311 win_amd64 / abi3 / py3) и
  нужные файлы колеса torch (download.pytorch.org) — без cuDNN, cuSPARSE, cuSOLVER, NVRTC, cuFFT и cuBLAS
  (TORCH_EXCLUDE, исследование samples/espeech-lean, docs/espeech.md, «Поставка»): torch_cuda.dll импортирует их
  статически, поэтому вместо них — DLL-заглушки (STUBS), а cuBLAS берётся из CUDA cosyvoice-server (архив llama.cpp);
  из колеса качаются только эти файлы, диапазонами (partzip.py): ~430 МБ из 1,9 ГБ;
- модель: исходные файлы из espeech/prepare.py (SOURCES) на закреплённых ревизиях Hugging Face; из чекпойнта ESpeech
  (2,7 ГБ) — только EMA-копия весов (prepare.ema_members), тоже диапазонами.
Для частичных архивов (колесо torch, чекпойнт) нужен весь файл: --torch-wheel и --checkpoint (иначе качаются в
--cache, по умолчанию packaging/windows/build/cache). Оттуда же — архив cudart llama.cpp (build-runtime.py) для
списка экспорта cuBLAS.

Заглушки: экспорт настоящих DLL (из колеса torch и архива cudart) → packaging/windows/stubs/<dll>.def — все функции
указывают на stub_fail (вернуть 1, у CUDA-библиотек это «не инициализировано»: вызов даст ошибку torch, а не тихий
мусор), *String/*Name — на stub_str, данные — на нулевые переменные. Собирает их build-runtime.py (MSVC). cuBLAS-
заглушки нужны компьютерам без CUDA (синтез на процессоре): torch_cuda.dll без cuBLAS не загружается.
Нужен только Python 3 (стандартная библиотека). Запускать при смене версий в PACKAGES, TORCH, PYTHON или SOURCES."""
import argparse, collections, hashlib, importlib.util, json, os, re, struct, sys, urllib.parse, urllib.request, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DOWNLOADS = os.path.join(ROOT, "stream-tts", "streamtts", "downloads.json")
STUB_DIR = os.path.join(HERE, "stubs")
CACHE = os.path.join(HERE, "build", "cache")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


prepare = _load("espeech_prepare", os.path.join(ROOT, "espeech", "prepare.py"))
partzip = _load("partzip", os.path.join(ROOT, "stream-tts", "streamtts", "partzip.py"))

PYTHON = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"
TORCH = "https://download.pytorch.org/whl/cu132/torch-2.14.1%2Bcu132-cp311-cp311-win_amd64.whl"
# Пакеты espeech-server (espeech/requirements.txt) и сборки модели (prepare.py: onnx с protobuf и ml_dtypes); что
# нужно torch при импорте — typing_extensions. Ставятся распаковкой колеса, без pip и без зависимостей.
PACKAGES = ["numpy==2.4.6", "safetensors==0.8.0", "onnxruntime==1.31.0", "tokenizers==0.23.3", "razdel==0.5.0",
            "psutil==7.2.2", "ruaccent==1.5.8.3", "typing_extensions==4.16.0", "onnx==1.23.2", "protobuf==7.36.2",
            "ml_dtypes==0.6.0"]
L = "torch/lib/"
STUBS = ["cudnn64_9.dll", "cusparse64_12.dll", "cusolver64_12.dll", "nvrtc64_130_0.dll", "cufft64_12.dll"]
CUBLAS = ["cublas64_13.dll", "cublasLt64_13.dll"]   # заглушки только без CUDA; с CUDA — настоящие из рантайма
TORCH_EXCLUDE = [
    # не грузятся статически и синтезу не нужны
    *(L + m for m in ("curand64*", "cusolverMg*", "cufftw*", "nvperf_host*", "nvJitLink*", "*.lib", "*.h")),
    "torch/include/*", "torch/bin/*", "torch/share/*",
    # cuDNN выключен (server.py): от него нужна только заглушка cudnn64_9
    *(L + m for m in ("cudnn*", "cusparse*", "cusolver*", "nvrtc*", "cufft*")),
    # cuBLAS — из CUDA cosyvoice-server; cudart torch связан статически
    *(L + m for m in ("cublas*", "cudart*")),
]


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


def wheel(req):
    """Колесо пакета под Python 3.11 win_amd64: {url, size, sha256}."""
    name, version = req.split("==")
    d = json.loads(get(f"https://pypi.org/pypi/{name}/{version}/json"))
    files = [u for u in d["urls"] if u["packagetype"] == "bdist_wheel"]
    for tag in ("-cp311-cp311-win_amd64.whl", "-abi3-win_amd64.whl", "-py3-none-any.whl", "-py2.py3-none-any.whl"):
        hit = [u for u in files if u["filename"].endswith(tag) and (tag != "-abi3-win_amd64.whl" or   # cp3X-abi3: X <= 11
                                                                   int(re.search(r"-cp3(\d+)-abi3", u["filename"])[1]) <= 11)]
        if hit:
            u = hit[0]
            return dict(url=u["url"], size=u["size"], sha256=u["digests"]["sha256"])
    sys.exit(f"{req}: нет колеса под Python 3.11 win_amd64")


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


def write_def(dll, data):
    exports = pe_exports(data)
    lines = [f"; Заглушка {dll}: экспорт настоящей DLL (packaging/windows/pin-espeech.py), сборка — build-runtime.py",
             f"LIBRARY {dll}", "EXPORTS"]
    n = 0
    for name, is_data in exports:
        if is_data:
            lines.append(f"  {name}=stub_data{n} DATA")
            n += 1
        else:
            lines.append(f"  {name}={'stub_str' if name.endswith(('String', 'Name')) else 'stub_fail'}")
    with open(os.path.join(STUB_DIR, dll[:-4] + ".def"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"{dll}: {len(exports) - n} функций, {n} данных")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--torch-wheel", help="колесо torch целиком (иначе качается в --cache, 1,9 ГБ)")
    ap.add_argument("--checkpoint", help="espeech_tts_256k.pt целиком (иначе качается в --cache, 2,7 ГБ)")
    ap.add_argument("--cache", default=CACHE)
    ap.add_argument("--no-stubs", action="store_true", help="не переписывать stubs/*.def")
    a = ap.parse_args()
    os.makedirs(a.cache, exist_ok=True)

    py = download(PYTHON, a.cache)
    whl = a.torch_wheel or download(TORCH, a.cache)
    files = hf_files()
    ck_src = next(f for f in files if f["path"].endswith("/" + prepare.CHECKPOINT))
    ck = a.checkpoint or download(ck_src["url"], a.cache)
    if sha256(ck) != ck_src["sha256"]:
        sys.exit(f"{ck}: sha256 не совпадает с закреплённой ревизией {prepare.ESPEECH[1]}")

    model = dict(files=[f for f in files if f["path"] != ck_src["path"]],
                 checkpoint=dict(partial(ck, ck_src["url"], dict(members=prepare.ema_members(ck))),
                                 path=ck_src["path"]))
    runtime = dict(python=dict(url=PYTHON, size=os.path.getsize(py), sha256=sha256(py)),
                   wheels=[wheel(r) for r in PACKAGES],
                   torch=partial(whl, TORCH, dict(exclude=TORCH_EXCLUDE)),
                   stubs=STUBS, cublas=CUBLAS)
    with open(DOWNLOADS, encoding="utf-8") as f:
        dl = json.load(f)
    dl["espeech"] = dict(runtime=runtime, model=model)
    with open(DOWNLOADS, "w", encoding="utf-8", newline="\n") as f:
        json.dump(dl, f, ensure_ascii=False, indent=1)
        f.write("\n")
    size = lambda x: x["size"]
    rt = runtime["python"]["size"] + sum(map(size, runtime["wheels"])) + runtime["torch"]["download"]
    md = sum(map(size, model["files"])) + model["checkpoint"]["download"]
    print(f"Закреплено в {DOWNLOADS}: рантайм {rt / 2**20:.0f} МБ, модель {md / 2**20:.0f} МБ")

    if not a.no_stubs:
        os.makedirs(STUB_DIR, exist_ok=True)
        with zipfile.ZipFile(whl) as z:
            for dll in STUBS:
                write_def(dll, z.read(L + dll))
        cudart = os.path.join(a.cache, dl["cuda"]["archives"][1]["url"].rsplit("/", 1)[-1])   # cuBLAS той же CUDA
        if not os.path.isfile(cudart):
            sys.exit(f"нет {cudart}: сначала packaging/windows/build-runtime.py --cuda")
        with zipfile.ZipFile(cudart) as z:
            for dll in CUBLAS:
                write_def(dll, z.read(dll))


if __name__ == "__main__":
    main()
