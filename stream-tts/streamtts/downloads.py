"""Что качается из админки: закреплённые загрузки (downloads.json — CUDA, кванты модели, рантайм и модель ESpeech),
модель CosyVoice и её кванты, необязательные пакеты (PACKAGES), фраза примеров звучания (static/samples/). Сами загрузки — install.py,
ESpeech — engines/espeech/download.py."""
import json, os

from . import paths
from .engines import cosyvoice

# Закреплённые загрузки (версию и файлы CUDA, размеры для сводки build-exe.py передаёт ещё и в StreamTTS.iss)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads.json"), encoding="utf-8") as _f:
    DOWNLOADS = json.load(_f)
CUDA = DOWNLOADS["cuda"]
# Другая папка с теми же архивами (проверка без GitHub: python -m http.server в packaging/windows/build/cache)
CUDA_BASE = paths.env("STREAM_TTS_CUDA_URL").rstrip("/")


def cuda_archives():
    """[(url, имя файла, размер, sha256, нужные файлы)] архивов CUDA."""
    out = []
    for a in CUDA["archives"]:
        name = a["url"].rsplit("/", 1)[-1]
        out.append((f"{CUDA_BASE}/{name}" if CUDA_BASE else a["url"], name, a["size"], a["sha256"], a["files"]))
    return out


CUDA_VERSION_FILE = "cuda-version.txt"

# Модель CosyVoice: репозиторий, кванты и видеопамять — в описании движка (engines/cosyvoice), размеры и адрес с
# закреплённой ревизией — из downloads.json (sha256 сверяется по x-linked-etag и совпадает с закреплённым)
REPO = cosyvoice.REPO
BASE = DOWNLOADS["model"]["url"].rstrip("/")
QUANTS = {q: dict(d, size=DOWNLOADS["model"]["quants"][q]["size"]) for q, d in cosyvoice.QUANTS.items()}
DEFAULT_QUANT = cosyvoice.DEFAULT_QUANT
# Примеры вариантов на «Загрузках» — static/samples/<вариант>.mp3: эта фраза, seed 42 (docs/stream-tts-deploy.md)
SAMPLE_PHRASE = "Ну всё, я пошёл в бой! Если не вернусь — считайте, что я был прав, а вы нет. Вперёд!"
FRONTEND = cosyvoice.FRONTEND
# Необязательные пакеты: качаются из админки («Загрузки» и редактор голоса). files — пути в репозитории source на
# Hugging Face, локально — paths.MODELS/<into>/<путь>. size — байт. alt — те же файлы в полной точности
# (scripts/download-model.sh --frontend full): тоже считаются установленными. restart — файлы читает
# cosyvoice-server: после загрузки он перезапускается, а удаляются они при остановленном сервере.
PACKAGES = {
    "frontend": dict(
        # ONNX-фронтенд CosyVoice3: речевой токенизатор и CAM++ (отпечаток голоса)
        title="Голос из записи", about="Нужен, чтобы сделать голос из записи.",
        files=FRONTEND, size=252891972, source=REPO, restart=True,
        alt=("frontend-onnx/campplus.onnx", "frontend-onnx/speech_tokenizer_v3.onnx")),
    # Веса Demucs обучены на MusDB: автор называет их исследовательскими (facebookresearch/demucs#267, #327, #508),
    # код Demucs и экспорт в ONNX — MIT. Поэтому пакет не входит в установщик, а качается по кнопке; лицензии всех
    # загрузок перечислены на странице «Информация» установщика (packaging/windows/THIRD-PARTY*.txt).
    "music": dict(
        # Demucs v4 отделяет голос от музыки: галочка «убрать музыку» в редакторе голоса
        title="Очистка от музыки", about="Убирает музыку из образца в редакторе голоса.",
        files=("htdemucs_int8.onnx",), into="demucs", size=84108789, source="HRSadeghi/demucs-onnx"),
}


def package_files(pid):
    """[(url, путь от paths.MODELS)] файлов пакета."""
    p = PACKAGES[pid]
    return [(f"https://huggingface.co/{p['source']}/resolve/main/{f}", f"{p['into']}/{f}" if p.get("into") else f)
            for f in p["files"]]
