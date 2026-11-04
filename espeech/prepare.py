"""Папка модели ESpeech для espeech-server (models/espeech/) из исходных файлов Hugging Face.

Что делает:
- ESpeech-TTS-1_SFT-256K: из чекпойнта espeech_tts_256k.pt (2,7 ГБ: две копии весов в fp32) — только EMA-копия в
  fp16 без префикса `ema_model.` и без буферов мела → espeech_fp16.safetensors (~675 МБ); vocab.txt — как есть.
  Чекпойнт читается без torch.load (load_ema): берутся только тензоры EMA-копии, поэтому годится и урезанный
  чекпойнт, где есть лишь они, — его собирает программа (engines/espeech/download.py), скачивая из 2,7 ГБ 1,35 ГБ;
- Vocos (charactr/vocos-mel-24khz): backbone и head в fp32 без feature_extractor → vocos.safetensors (~54 МБ);
- RUAccent (ruaccent/accentuator, полный режим): словарь словоформ dictionary/accents.json.gz → ruaccent/accents.sqlite
  (таблица a(w PRIMARY KEY, s) WITHOUT ROWID, ~160 МБ, проверка каждой пары); ONNX-модели → ruaccent/onnx/<имя>.onnx
  с весами во внешнем файле, каждый тензор с границы 64 КБ (onnxruntime отображает его в память — веса не входят в
  частную память процесса; байты те же — проверяется); токенизаторы и config.json — в ruaccent/nn/; три малых словаря —
  в ruaccent/dictionary/. RuleEngine (koziev) и accents_nn.json.gz (tiny_mode) не нужны.
Исходные файлы — SOURCES (репозиторий, закреплённая ревизия, пути); их же качает программа (адреса, размеры и sha256 —
в stream-tts/streamtts/downloads.json, packaging/pin-espeech.py). Готовые файлы пропускаются (--force —
пересобрать); каждый пишется во временный и переименовывается, так что прерванная сборка продолжается.

usage: python -m espeech.prepare --src <папка> [out=models/espeech] [--force]
  --src — папка с исходными файлами в виде <репозиторий>/<путь> (так их раскладывает программа). Из кеша Hugging Face
  с загрузкой недостающего собирает tools/espeech/prepare.py.
Нужны torch, safetensors, onnx (с protobuf)."""
import argparse, collections, gzip, json, os, pickle, shutil, sqlite3, sys, tempfile, time, zipfile

ESPEECH = ("ESpeech/ESpeech-TTS-1_SFT-256K", "2bd8087e9b7e3a89e7b546e2fb1893f2f0ba137c")
VOCOS = ("charactr/vocos-mel-24khz", "0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21")
RUACCENT = ("ruaccent/accentuator", "b78ae5ea1e62beaf138bed1865cd8c3b0b5ca855")
CHECKPOINT = "espeech_tts_256k.pt"
ONNX = {"turbo3.1": "nn/nn_omograph/turbo3.1", "nn_accent": "nn/nn_accent",
        "nn_stress_usage_predictor": "nn/nn_stress_usage_predictor",
        "nn_yo_homograph_resolver": "nn/nn_yo_homograph_resolver"}
DICTS = ("omographs.json.gz", "yo_words.json.gz", "yo_homographs.json.gz")
ALIGN = 65536   # гранулярность отображения файлов в Windows


def tokenizer_files(name):
    """Токенизаторы и config.json (id2label); у nn_accent ещё ort_config.json — не нужен."""
    return ["config.json", "tokenizer_config.json", "special_tokens_map.json"] + \
        (["vocab.txt"] if name == "nn_accent" else ["tokenizer.json"])


def sources():
    """[(репозиторий, ревизия, путь)] — всё, что нужно для сборки папки модели."""
    out = [(*ESPEECH, CHECKPOINT), (*ESPEECH, "vocab.txt"), (*VOCOS, "pytorch_model.bin"),
           (*RUACCENT, "dictionary/accents.json.gz")]
    out += [(*RUACCENT, "dictionary/" + n) for n in DICTS]
    for name, sub in ONNX.items():
        out += [(*RUACCENT, f"{sub}/{f}") for f in ["model.onnx"] + tokenizer_files(name)]
    return out


def outputs():
    """Файлы готовой папки модели (пути от неё): по ним программа узнаёт, что сборка закончена."""
    out = ["espeech_fp16.safetensors", "vocab.txt", "vocos.safetensors", "ruaccent/accents.sqlite"]
    out += ["ruaccent/dictionary/" + n for n in DICTS]
    for name, sub in ONNX.items():
        out += [f"ruaccent/onnx/{name}.onnx", f"ruaccent/onnx/{name}.onnx.data"]
        out += [f"ruaccent/{sub}/{f}" for f in tokenizer_files(name)]
    return out


def fresh(path, force):
    if os.path.exists(path) and not force:
        print("есть:", os.path.basename(path), flush=True)
        return False
    return True


def copy(src, dst):
    shutil.copyfile(src, dst + ".tmp")
    os.replace(dst + ".tmp", dst)


STORAGES = ("FloatStorage", "HalfStorage", "BFloat16Storage", "DoubleStorage", "LongStorage", "IntStorage",
            "BoolStorage", "ByteStorage")
_Storage = collections.namedtuple("Storage", "key type")
_Tensor = collections.namedtuple("Tensor", "storage offset size stride")


class _Unpickler(pickle.Unpickler):
    """Только то, из чего состоит чекпойнт: OrderedDict, тензоры и хранилища (данные хранилищ не читаются)."""

    def find_class(self, module, name):
        if (module, name) == ("collections", "OrderedDict"):
            return collections.OrderedDict
        if (module, name) == ("torch._utils", "_rebuild_tensor_v2"):
            return lambda storage, offset, size, stride, *rest: _Tensor(storage, offset, tuple(size), tuple(stride))
        if module == "torch" and name in STORAGES:
            return name
        raise pickle.UnpicklingError(f"чекпойнт: неожиданный {module}.{name}")

    def persistent_load(self, pid):   # ('storage', тип, ключ, устройство, число элементов)
        return _Storage(pid[2], pid[1])


def _ema(z):
    """(префикс, {имя: _Tensor}) EMA-копии весов в открытом чекпойнте."""
    pkl = next(n for n in z.namelist() if n.endswith("/data.pkl"))
    with z.open(pkl) as f:
        root = _Unpickler(f).load()
    return pkl[:-len("data.pkl")], root["ema_model_state_dict"]


def ema_members(path):
    """Файлы чекпойнта (zip), которые читает load_ema: из них программа собирает урезанный чекпойнт."""
    with zipfile.ZipFile(path) as z:
        prefix, ema = _ema(z)
    return sorted({prefix + "data.pkl"} | {f"{prefix}data/{t.storage.key}" for t in ema.values()})


def load_ema(path):
    """{имя: тензор} EMA-копии весов из чекпойнта torch.save без torch.load: данные читаются только у её тензоров.
    Тензоры — fp32 как в файле (bool и int64 у служебных initted и step)."""
    import torch
    dtypes = dict(zip(STORAGES, (torch.float32, torch.float16, torch.bfloat16, torch.float64, torch.int64, torch.int32,
                                 torch.bool, torch.uint8)))
    with zipfile.ZipFile(path) as z:
        prefix, ema = _ema(z)
        out = {}
        for k, v in ema.items():
            raw = bytearray(z.read(f"{prefix}data/{v.storage.key}"))
            out[k] = torch.frombuffer(raw, dtype=dtypes[v.storage.type]).as_strided(v.size, v.stride, v.offset)
    return out


def weights(src, out, force):
    import torch
    from safetensors.torch import save_file
    dst = os.path.join(out, "espeech_fp16.safetensors")
    if fresh(dst, force):
        sd = {}
        for k, v in load_ema(src(ESPEECH, CHECKPOINT)).items():
            if k in ("initted", "step"):
                continue
            k = k.removeprefix("ema_model.")
            if k.startswith("mel_spec."):   # окно и фильтры мела строит код
                continue
            sd[k] = (v.half() if v.is_floating_point() else v).contiguous()
        save_file(sd, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
        print("espeech:", len(sd), "тензоров,", round(sum(v.numel() for v in sd.values()) / 1e6), "млн параметров",
              flush=True)
    dst = os.path.join(out, "vocab.txt")
    if fresh(dst, force):
        copy(src(ESPEECH, "vocab.txt"), dst)
    dst = os.path.join(out, "vocos.safetensors")
    if fresh(dst, force):
        v = torch.load(src(VOCOS, "pytorch_model.bin"), map_location="cpu", weights_only=True)
        v = {k: t.contiguous() for k, t in v.items() if not k.startswith("feature_extractor.")}
        save_file(v, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
        print("vocos:", len(v), "тензоров", flush=True)


def accents_db(src, dst):
    t = time.time()
    d = json.load(gzip.open(src))
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in d.items())
    # база собирается и проверяется во временной папке, а на место — копией: в Docker папка модели — том с Windows
    # (Docker Desktop), и там VACUUM и 3 млн выборок проверки шли больше 20 мин вместо минуты
    work = tempfile.mkdtemp(prefix="espeech-accents-")
    tmp = os.path.join(work, "accents.sqlite")
    db = sqlite3.connect(tmp)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("CREATE TABLE a (w TEXT PRIMARY KEY, s TEXT) WITHOUT ROWID")
    db.executemany("INSERT INTO a VALUES (?, ?)", sorted(d.items()))   # по порядку: файл меньше, запись быстрее
    db.commit()
    db.execute("VACUUM")
    n = db.execute("SELECT count(*) FROM a").fetchone()[0]
    miss = sum(db.execute("SELECT s FROM a WHERE w=?", (k,)).fetchone() != (v,) for k, v in d.items())
    db.close()
    try:
        assert n == len(d) and miss == 0, (n, len(d), miss)
        shutil.copyfile(tmp, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"accents.sqlite: {n} форм, {os.path.getsize(dst) / 2**20:.0f} МБ, {time.time() - t:.0f} с, проверено",
          flush=True)


def onnx_ext(src, dst, threshold=4096):
    """ONNX → граф + веса во внешнем файле, каждый тензор с границы ALIGN. Граф пишется последним (через временный
    файл): есть граф — есть и веса."""
    import onnx
    from onnx import TensorProto, numpy_helper
    m = onnx.load(src)
    data = os.path.basename(dst) + ".data"
    pos = 0
    with open(os.path.join(os.path.dirname(dst), data), "wb") as f:
        for t in m.graph.initializer:
            if not t.HasField("raw_data") or len(t.raw_data) < threshold:
                continue
            pos = (pos + ALIGN - 1) // ALIGN * ALIGN
            f.seek(pos)
            f.write(t.raw_data)
            ln = len(t.raw_data)
            t.ClearField("raw_data")
            t.data_location = TensorProto.EXTERNAL
            del t.external_data[:]
            for k, v in (("location", data), ("offset", str(pos)), ("length", str(ln))):
                e = t.external_data.add()
                e.key, e.value = k, v
            pos += ln
    tmp = dst + ".tmp"
    onnx.save_model(m, tmp)
    a = {t.name: numpy_helper.to_array(t).tobytes() for t in onnx.load(src).graph.initializer}
    b = {t.name: numpy_helper.to_array(t).tobytes() for t in onnx.load(tmp).graph.initializer}   # веса — из data
    assert a == b, f"{dst}: веса не совпали"
    os.replace(tmp, dst)
    print(f"{os.path.basename(dst)}: {pos / 2**20:.0f} МБ во внешнем файле, веса совпадают", flush=True)


def ruaccent(src, out, force):
    base = os.path.join(out, "ruaccent")
    for sub in ("dictionary", "onnx", *ONNX.values()):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    dst = os.path.join(base, "accents.sqlite")
    if fresh(dst, force):
        accents_db(src(RUACCENT, "dictionary/accents.json.gz"), dst)
    for name in DICTS:
        dst = os.path.join(base, "dictionary", name)
        if fresh(dst, force):
            copy(src(RUACCENT, "dictionary/" + name), dst)
    for name, sub in ONNX.items():
        dst = os.path.join(base, "onnx", name + ".onnx")
        if fresh(dst, force):
            onnx_ext(src(RUACCENT, sub + "/model.onnx"), dst)
        for f in tokenizer_files(name):
            dst = os.path.join(base, sub, f)
            if fresh(dst, force):
                copy(src(RUACCENT, f"{sub}/{f}"), dst)


def build(src, out, force=False):
    """src(источник, путь) → локальный файл; источник — ESPEECH, VOCOS или RUACCENT."""
    os.makedirs(out, exist_ok=True)
    weights(src, out, force)
    ruaccent(src, out, force)
    missing = [f for f in outputs() if not os.path.isfile(os.path.join(out, *f.split("/")))]
    if missing:
        raise RuntimeError(f"не собраны: {', '.join(missing)}")
    print("готово:", out, flush=True)


def main():
    ap = argparse.ArgumentParser(prog="python -m espeech.prepare", description=__doc__.split("\n")[0])
    ap.add_argument("out", nargs="?", default=os.path.join("models", "espeech"))
    ap.add_argument("--src", required=True, help="папка с исходными файлами: <репозиторий>/<путь>")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    build(lambda source, rel: os.path.join(a.src, *source[0].split("/"), *rel.split("/")), a.out, a.force)


if __name__ == "__main__":
    sys.exit(main())
