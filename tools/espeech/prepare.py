"""Папка модели ESpeech для espeech-server (models/espeech/) из исходных файлов Hugging Face.

Что делает:
- ESpeech-TTS-1_SFT-256K: из чекпойнта espeech_tts_256k.pt (1,3 ГБ: две копии весов и состояние оптимизатора) —
  только EMA-копия в fp16 без префикса `ema_model.` и без буферов мела → espeech_fp16.safetensors (~675 МБ);
  vocab.txt — как есть;
- Vocos (charactr/vocos-mel-24khz): backbone и head в fp32 без feature_extractor → vocos.safetensors (~54 МБ);
- RUAccent (ruaccent/accentuator, полный режим): словарь словоформ dictionary/accents.json.gz → ruaccent/accents.sqlite
  (таблица a(w PRIMARY KEY, s) WITHOUT ROWID, ~160 МБ, проверка каждой пары); ONNX-модели → ruaccent/onnx/<имя>.onnx
  с весами во внешнем файле, каждый тензор с границы 64 КБ (onnxruntime отображает его в память — веса не входят в
  частную память процесса; байты те же — проверяется); токенизаторы и config.json — в ruaccent/nn/; три малых словаря —
  в ruaccent/dictionary/. RuleEngine (koziev) и accents_nn.json.gz (tiny_mode) не нужны.
Источники — кеш Hugging Face (~/.cache/huggingface/hub), чего нет — качается (huggingface_hub). Готовые файлы
пропускаются (--force — пересобрать).

usage: espeech/.venv/Scripts/python tools/espeech/prepare.py [out=models/espeech] [--ruaccent <папка RUAccent>] [--force]
  --ruaccent — уже скачанная папка RUAccent (dictionary/, nn/), иначе нужные файлы качаются с HF.
Нужны torch, safetensors, onnx, huggingface_hub (espeech/.venv + pip install onnx)."""
import argparse, gzip, json, os, shutil, sqlite3, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
ESPEECH_REPO, VOCOS_REPO = "ESpeech/ESpeech-TTS-1_SFT-256K", "charactr/vocos-mel-24khz"
RUACCENT_REPO, RUACCENT_REV = "ruaccent/accentuator", "b78ae5ea1e62beaf138bed1865cd8c3b0b5ca855"
ONNX = {"turbo3.1": "nn/nn_omograph/turbo3.1", "nn_accent": "nn/nn_accent",
        "nn_stress_usage_predictor": "nn/nn_stress_usage_predictor",
        "nn_yo_homograph_resolver": "nn/nn_yo_homograph_resolver"}
DICTS = ("omographs.json.gz", "yo_words.json.gz", "yo_homographs.json.gz")
ALIGN = 65536   # гранулярность отображения файлов в Windows


def hf_file(repo, name, revision=None):
    from huggingface_hub import hf_hub_download
    return hf_hub_download(repo, name, revision=revision)


def fresh(path, force):
    if os.path.exists(path) and not force:
        print("есть:", os.path.relpath(path, ROOT))
        return False
    return True


def weights(out, force):
    import torch
    from safetensors.torch import save_file
    dst = os.path.join(out, "espeech_fp16.safetensors")
    if fresh(dst, force):
        ck = torch.load(hf_file(ESPEECH_REPO, "espeech_tts_256k.pt"), map_location="cpu", weights_only=True)
        sd = {}
        for k, v in ck["ema_model_state_dict"].items():
            if not torch.is_tensor(v) or k in ("initted", "step"):
                continue
            k = k.removeprefix("ema_model.")
            if k.startswith("mel_spec."):   # окно и фильтры мела строит код
                continue
            sd[k] = (v.half() if v.is_floating_point() else v).contiguous()
        save_file(sd, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
        print("espeech:", len(sd), "тензоров,", round(sum(v.numel() for v in sd.values()) / 1e6), "млн параметров")
    dst = os.path.join(out, "vocab.txt")
    if fresh(dst, force):
        shutil.copyfile(hf_file(ESPEECH_REPO, "vocab.txt"), dst)
    dst = os.path.join(out, "vocos.safetensors")
    if fresh(dst, force):
        v = torch.load(hf_file(VOCOS_REPO, "pytorch_model.bin"), map_location="cpu", weights_only=True)
        v = {k: t.contiguous() for k, t in v.items() if not k.startswith("feature_extractor.")}
        save_file(v, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
        print("vocos:", len(v), "тензоров")


def ruaccent_src(local):
    """→ функция: путь в репозитории RUAccent → локальный файл."""
    if local:
        return lambda rel: os.path.join(local, rel)
    return lambda rel: hf_file(RUACCENT_REPO, rel, RUACCENT_REV)


def accents_db(src, dst):
    t = time.time()
    d = json.load(gzip.open(src))
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in d.items())
    tmp = dst + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
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
    assert n == len(d) and miss == 0, (n, len(d), miss)
    os.replace(tmp, dst)
    print(f"accents.sqlite: {n} форм, {os.path.getsize(dst) / 2**20:.0f} МБ, {time.time() - t:.0f} с, проверено")


def onnx_ext(src, dst, threshold=4096):
    """ONNX → граф + веса во внешнем файле, каждый тензор с границы ALIGN."""
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
    onnx.save_model(m, dst)
    a = {t.name: numpy_helper.to_array(t).tobytes() for t in onnx.load(src).graph.initializer}
    b = {t.name: numpy_helper.to_array(t).tobytes() for t in onnx.load(dst).graph.initializer}
    assert a == b, f"{dst}: веса не совпали"
    print(f"{os.path.basename(dst)}: {pos / 2**20:.0f} МБ во внешнем файле, веса совпадают")


def ruaccent(out, local, force):
    src = ruaccent_src(local)
    base = os.path.join(out, "ruaccent")
    for sub in ("dictionary", "onnx", *ONNX.values()):
        os.makedirs(os.path.join(base, sub), exist_ok=True)
    dst = os.path.join(base, "accents.sqlite")
    if fresh(dst, force):
        accents_db(src("dictionary/accents.json.gz"), dst)
    for name in DICTS:
        dst = os.path.join(base, "dictionary", name)
        if fresh(dst, force):
            shutil.copyfile(src("dictionary/" + name), dst)
    for name, sub in ONNX.items():
        dst = os.path.join(base, "onnx", name + ".onnx")
        if fresh(dst, force):
            onnx_ext(src(sub + "/model.onnx"), dst)
        # токенизаторы и config.json (id2label); у nn_accent ещё ort_config.json — не нужен
        files = ["config.json", "tokenizer_config.json", "special_tokens_map.json"]
        files += ["vocab.txt"] if name == "nn_accent" else ["tokenizer.json"]
        for f in files:
            dst = os.path.join(base, sub, f)
            if fresh(dst, force):
                shutil.copyfile(src(f"{sub}/{f}"), dst)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", nargs="?", default=os.path.join(ROOT, "models", "espeech"))
    ap.add_argument("--ruaccent", help="папка RUAccent (dictionary/, nn/) вместо загрузки с HF")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    weights(a.out, a.force)
    ruaccent(a.out, a.ruaccent, a.force)
    print("готово:", a.out)


if __name__ == "__main__":
    sys.exit(main())
