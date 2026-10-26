"""Голос ESpeech (.espeech) из образца: кусок записи, текст с ударениями, темп и необязательный адаптер LoRA.

Образец для ESpeech — 6–7 с спокойной речи одной-двумя целыми фразами: модель копирует манеру образца (агрессивный
кусок даёт агрессивную речь), поэтому кусок выбирается вручную. Текст — ровно то, что звучит в куске; ударения ставит
RUAccent (полный режим, как на синтезе), если в тексте нет «+». Темп (кадров мела на байт текста) считается по
полному исходному образцу: короткий кусок бывает быстрее или медленнее средней речи голоса.

Один голос:
  make-voice.py <out.espeech> --wav <запись> --text "<что звучит в куске>" [--start С --end С]
                [--full-wav <полная запись> --full-text "<её текст>"] [--lora <адаптер.safetensors>]
                [--title …] [--variant …]
  Без --full-wav темп — по самому куску.
Набор (поле `espeech` в voice-refs/<модель>/<набор>/manifest.json):
  make-voice.py --manifest voice-refs/cosyvoice/<набор>/manifest.json [--out voices/<набор>] [голос …]
  "espeech": {"start": 4.6, "end": 11.53, "text": "…", "clip": "<файл в папке набора, по умолчанию <голос>.wav>",
              "lora": "<путь от корня проекта>", "tempo": <кадров на байт; по умолчанию — по всему <голос>.wav и
              тексту записи>}; голоса без поля пропускаются.
Адаптер — выход обучения LoRA (ключи «<блок>.<q|k|v|o|ff1|ff2>.<A|B>», метаданные rank, alpha, step).
Звук читается через ffmpeg (в PATH) в 24 кГц моно. Нужен espeech/.venv (torch, safetensors, onnxruntime, ruaccent)
и models/espeech (tools/espeech/prepare.py). Выход: .espeech (espeech/voices.py)."""
import argparse, json, os, subprocess, sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
from espeech import voices as V   # noqa: E402

SR = 24000
_acc = None


def read_audio(path, start=None, end=None):
    cmd = ["ffmpeg", "-v", "error", "-i", path]
    if start is not None:
        cmd[3:3] = ["-ss", str(start)]
        if end is not None:
            cmd[5:5] = ["-to", str(end)]
    cmd += ["-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    return np.frombuffer(subprocess.run(cmd, check=True, capture_output=True).stdout, np.float32).copy()


def accented(text):
    global _acc
    if "+" in text:
        return text.strip()
    if _acc is None:
        from espeech.accent import Accentor
        _acc = Accentor(os.path.join(ROOT, "models", "espeech", "ruaccent"))
    return _acc(text.strip())


def read_lora(path):
    from safetensors import safe_open
    with safe_open(path, "pt") as f:
        md = f.metadata() or {}
        t = {"lora." + k: f.get_tensor(k) for k in f.keys()}
    meta = dict(lora_rank=md.get("rank", "16"), lora_alpha=md.get("alpha", md.get("rank", "16")),
                lora_steps=md.get("step"))
    return t, meta


def make(out, wav, text, start=None, end=None, full_wav=None, full_text=None, tempo=None, lora=None, title=None,
         variant=None):
    ref = read_audio(wav, start, end)
    sec = len(ref) / SR
    if not 1.0 <= sec <= V.MAX_REF_SEC:
        raise SystemExit(f"{out}: образец {sec:.1f} с, нужно от 1 до {V.MAX_REF_SEC:g} (лучше 6–7)")
    text = accented(text)
    if tempo is None and full_wav:
        full = read_audio(full_wav)
        tempo = V.tempo_of(len(full), accented(full_text))
    meta = dict(title=title, variant=variant)
    tensors = None
    if lora:
        tensors, lmeta = read_lora(lora)
        meta.update(lmeta)
    V.save(out, ref, text, tempo, meta, tensors)
    own = V.tempo_of(len(ref), text)
    print(f"{out}: {sec:.1f} с, темп {tempo or own:.3f} (кусок {own:.3f})"
          f"{', LoRA' if lora else ''} — {text}")


def from_manifest(path, out_dir, only):
    base = os.path.dirname(os.path.abspath(path))
    man = json.load(open(path, encoding="utf-8"))
    out_dir = out_dir or os.path.join(ROOT, "voices", os.path.basename(base))
    n = 0
    for name, v in man.items():
        e = v.get("espeech")
        if not e or (only and name not in only):
            continue
        rec = os.path.join(base, e.get("clip") or name + ".wav")
        make(os.path.join(out_dir, name + V.EXT), rec, e["text"], e.get("start"), e.get("end"),
             full_wav=os.path.join(base, name + ".wav"), full_text=v.get("text"), tempo=e.get("tempo"),
             lora=os.path.join(ROOT, e["lora"]) if e.get("lora") else None, title=v.get("title"),
             variant=e.get("variant"))
        n += 1
    print(f"голосов: {n}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", nargs="?", help="файл .espeech (один голос)")
    ap.add_argument("voices", nargs="*", help="с --manifest: только эти голоса")
    ap.add_argument("--manifest")
    ap.add_argument("--out", dest="out_dir", help="с --manifest: папка голосов (по умолчанию voices/<набор>)")
    ap.add_argument("--wav")
    ap.add_argument("--text")
    ap.add_argument("--start", type=float)
    ap.add_argument("--end", type=float)
    ap.add_argument("--full-wav")
    ap.add_argument("--full-text")
    ap.add_argument("--tempo", type=float)
    ap.add_argument("--lora")
    ap.add_argument("--title")
    ap.add_argument("--variant")
    a = ap.parse_args()
    if a.manifest:
        only = set(a.voices) | ({a.out} if a.out else set())
        return from_manifest(a.manifest, a.out_dir, only)
    if not (a.out and a.wav and a.text):
        ap.error("нужны out, --wav и --text (или --manifest)")
    if a.full_wav and not a.full_text:
        ap.error("--full-wav без --full-text")
    make(a.out, a.wav, a.text, a.start, a.end, a.full_wav, a.full_text, a.tempo, a.lora, a.title, a.variant)


if __name__ == "__main__":
    main()
