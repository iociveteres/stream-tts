"""Папка модели ESpeech для espeech-server (models/espeech/) из кеша Hugging Face — для разработки.

Сборку делает espeech/prepare.py (что из чего получается — в его docstring), этот скрипт только подставляет ему
исходные файлы: из кеша Hugging Face (~/.cache/huggingface/hub) на закреплённых там ревизиях, чего нет — качается
(huggingface_hub). Установленная программа качает те же файлы сама (вкладка «Загрузки», engines/espeech/download.py).
Готовые файлы пропускаются (--force — пересобрать).

usage: espeech/.venv/Scripts/python tools/espeech/prepare.py [out=models/espeech] [--ruaccent <папка RUAccent>] [--force]
  --ruaccent — уже скачанная папка RUAccent (dictionary/, nn/), иначе нужные файлы качаются с HF.
Нужны torch, safetensors, onnx, huggingface_hub (espeech/.venv + pip install onnx huggingface_hub)."""
import argparse, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
from espeech import prepare  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("out", nargs="?", default=os.path.join(ROOT, "models", "espeech"))
    ap.add_argument("--ruaccent", help="папка RUAccent (dictionary/, nn/) вместо загрузки с HF")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()

    def src(source, rel):
        if source == prepare.RUACCENT and a.ruaccent:
            return os.path.join(a.ruaccent, *rel.split("/"))
        from huggingface_hub import hf_hub_download
        return hf_hub_download(source[0], rel, revision=source[1])

    prepare.build(src, a.out, a.force)


if __name__ == "__main__":
    sys.exit(main())
