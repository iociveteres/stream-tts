"""Запуск встроенного supertonic-server (общий наблюдатель процесса — runtime.py).

Сервер — Python-пакет supertonic (папка supertonic/ проекта), запускается python -m supertonic. Зависимости у него
только numpy и onnxruntime — те же, что у stream-tts, поэтому из исходников его запускает интерпретатор stream-tts
(sys.executable) с корнем проекта в PYTHONPATH:
- SUPERTONIC_SERVER — путь к python(.exe) или к папке с ним; пакет ищется вверх от интерпретатора (первая папка, где
  лежит supertonic/server.py), а если его там нет — в корне проекта (package_root());
- из исходников — sys.executable, если в корне проекта есть supertonic/server.py;
- программа для Windows и образ Docker — пока нет (find_server() → None): как поставлять сервер с ними, решает
  следующий этап (docs/supertonic.md).

Аргументы: --models paths.MODELS/supertonic (раскладка Hugging Face Supertone/supertonic-3: onnx/*.onnx, onnx/tts.json,
onnx/unicode_indexer.json, voice_styles/; без модели сервер не запускается — причина видна в админке),
--voices paths.VOICES (голоса <имя>.supertonic и <набор>/<имя>.supertonic; сервер перечитывает папку сам),
--threads SUPERTONIC_THREADS (если задана). Устройство одно — процессор. Порт — SUPERTONIC_PORT (иначе свободный),
адрес сервера — SUPERTONIC_HOST (по умолчанию 127.0.0.1)."""
import glob, os, sys

from ... import paths
from ...i18n import tr
from ...paths import EXE, env
from ...runtime import Unavailable

DEVICES = ("cpu",)
MODEL_NAME = "Supertonic 3"
# Без этих файлов сервер не запустится (раскладка Supertone/supertonic-3; voice_styles/ — готовые голоса, их ставит
# набор supertonic)
MODEL_FILES = ("onnx/duration_predictor.onnx", "onnx/text_encoder.onnx", "onnx/vector_estimator.onnx",
               "onnx/vocoder.onnx", "onnx/tts.json", "onnx/unicode_indexer.json")
PY = os.path.join("Scripts", "python.exe") if os.name == "nt" else os.path.join("bin", "python")


def _python_in(d):
    for p in (os.path.join(d, "python" + EXE), os.path.join(d, PY), os.path.join(d, ".venv", PY)):
        if os.path.isfile(p):
            return p
    return None


def _has_package(d):
    return os.path.isfile(os.path.join(d, "supertonic", "server.py"))


def package_root(python):
    """Папка с пакетом supertonic (для PYTHONPATH) или None. У интерпретатора stream-tts — корень проекта, из которого
    он запущен: окружение бывает общим с другой копией проекта (у git worktree — основной папки), и поиск вверх от
    него нашёл бы чужой пакет. У другого (SUPERTONIC_SERVER) — вверх от него, потом корень проекта."""
    own = paths.APP if paths.FROM_SOURCE and _has_package(paths.APP) else None
    if os.path.normcase(os.path.abspath(python)) == os.path.normcase(os.path.abspath(sys.executable)) and own:
        return own
    d = os.path.dirname(os.path.abspath(python))
    while True:
        if _has_package(d):
            return d
        up = os.path.dirname(d)
        if up == d:
            return own
        d = up


def find_server():
    """Интерпретатор supertonic-server или None."""
    p = env("SUPERTONIC_SERVER")
    if p:
        return (_python_in(p) or p) if os.path.isdir(p) else p
    # Программа для Windows и Docker: поставка сервера — следующий этап, пока модели в выборе нет
    if paths.FROM_SOURCE and _has_package(paths.APP):
        return sys.executable
    return None


def model_ready(models):
    return all(os.path.isfile(os.path.join(models, *f.split("/"))) for f in MODEL_FILES)


class Launcher:
    ID = "supertonic"         # логгер вывода сервера
    NAME = "supertonic-server"
    # Устройство не выбирается (только процессор): админка показывает единственный вариант, POST /api/backend отказывает
    DEVICE_KEY, DEVICE_ENV = "", ""

    def __init__(self, server, settings=None):
        self.server, self.dir = server, os.path.dirname(os.path.abspath(server))
        self.s = settings
        self.port = int(env("SUPERTONIC_PORT", "0"))
        self.host = env("SUPERTONIC_HOST", "127.0.0.1")
        self.backend = "CPU"
        self.backends = []         # видеокарт нет: «Сравнить все устройства» не показывается (bench.py)
        self.model = ""
        self.voices = 0
        self.override = ""         # bench.py ставит устройство на время проверки; здесь оно одно

    @property
    def models(self):
        return os.path.join(paths.MODELS, "supertonic")

    def public(self):
        return dict(backend=self.backend, prefer="cpu", model=self.model, voices=self.voices, backends=[],
                    devices=list(DEVICES), models_dir=self.models, device_env=self.DEVICE_ENV,
                    locked=dict(backend=False, model=True))

    def env(self):
        e = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        e.pop("PYTHONHOME", None)
        e["PYTHONPATH"] = package_root(self.server) or ""
        return e

    # --- для лога ---
    def shown(self, args):
        return args

    def started(self):
        return f" (голосов: {self.voices})"

    def ready(self):
        return f"процессор, голосов {self.voices}"

    @staticmethod
    def noise(line):
        return False

    # --- аргументы ---
    async def args(self, host, port):
        self.model = ""
        if not os.path.isfile(self.server):
            raise Unavailable(tr("нет {path}", path=self.server))
        if not package_root(self.server):
            raise Unavailable(tr("рядом с {path} нет пакета supertonic", path=self.server))
        if not model_ready(self.models):
            raise Unavailable(tr("нет модели Supertonic 3: положите файлы Supertone/supertonic-3 в {dir}",
                                 dir=self.models))
        self.model = MODEL_NAME
        self.voices = len(glob.glob(os.path.join(paths.VOICES, "*.supertonic")) +
                          glob.glob(os.path.join(paths.VOICES, "*", "*.supertonic")))
        a = os.path.abspath   # сервер работает в папке голосов
        out = [self.server, "-m", "supertonic", "--models", a(self.models), "--voices", a(paths.VOICES),
               "--host", host, "--port", str(port)]
        if env("SUPERTONIC_THREADS"):
            out += ["--threads", env("SUPERTONIC_THREADS")]
        return out
