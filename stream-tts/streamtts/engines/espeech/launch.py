"""Запуск espeech-server в режиме managed (общий наблюдатель процесса — runtime.py).

Сервер — Python-пакет espeech (папка espeech/ проекта), запускается python -m espeech интерпретатором его окружения:
ESPEECH_SERVER — путь к python(.exe) или к папке с ним (или к папке espeech/ с .venv); иначе
paths.RUNTIME/espeech/python(.exe) (поставка программы, этап 4), иначе espeech/.venv проекта (из исходников).
Пакет ищется вверх от интерпретатора (первая папка, где лежит espeech/server.py) и попадает в PYTHONPATH.

Аргументы: --models paths.MODELS/espeech (tools/espeech/prepare.py; без весов сервер не запускается),
--voices paths.VOICES (голоса <имя>.espeech и <набор>/<имя>.espeech; сервер перечитывает папку сам),
--device cuda|cpu: ESPEECH_DEVICE, иначе выбор в админке (settings.espeech_device), auto — CUDA, если есть драйвер
NVIDIA. Порт — ESPEECH_PORT (иначе свободный), адрес сервера — ESPEECH_HOST (по умолчанию 127.0.0.1)."""
import glob, os, shutil

from ... import paths
from ...i18n import tr
from ...paths import EXE, env
from ...runtime import Unavailable

DEVICES = ("auto", "cuda", "cpu")
WEIGHTS = "espeech_fp16.safetensors"
MODEL_NAME = "ESpeech-TTS-1_SFT-256K"
PY = os.path.join("Scripts", "python.exe") if os.name == "nt" else os.path.join("bin", "python")


def _python_in(d):
    for p in (os.path.join(d, "python" + EXE), os.path.join(d, PY), os.path.join(d, ".venv", PY)):
        if os.path.isfile(p):
            return p
    return None


def find_server():
    """Интерпретатор espeech-server для режима managed или None."""
    p = env("ESPEECH_SERVER")
    if p:
        return (_python_in(p) or p) if os.path.isdir(p) else p
    for d in (os.path.join(paths.RUNTIME, "espeech"), os.path.join(paths.APP, "espeech")):
        p = _python_in(d)
        if p and package_root(p):
            return p
    return None


def package_root(python):
    """Папка, в которой лежит пакет espeech (для PYTHONPATH), или None."""
    d = os.path.dirname(os.path.abspath(python))
    while True:
        if os.path.isfile(os.path.join(d, "espeech", "server.py")):
            return d
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def nvidia():
    """Есть ли драйвер NVIDIA: torch в рантайме — с CUDA, без драйвера синтез только на процессоре."""
    if os.name == "nt":
        return os.path.isfile(os.path.join(env("SystemRoot", r"C:\Windows"), "System32", "nvcuda.dll"))
    return os.path.exists("/dev/nvidiactl") or bool(shutil.which("nvidia-smi"))


class Launcher:
    ID = "espeech"            # логгер вывода сервера
    NAME = "espeech-server"
    DEVICE_KEY, DEVICE_ENV = "espeech_device", "ESPEECH_DEVICE"

    def __init__(self, server, settings=None):
        self.server, self.dir = server, os.path.dirname(os.path.abspath(server))
        self.s = settings
        self.port = int(env("ESPEECH_PORT", "0"))
        self.host = env("ESPEECH_HOST", "127.0.0.1")
        self.backend = ""          # CUDA0 | CPU — как у CosyVoice, для админки и проверки скорости
        self.backends = [("CUDA0", False)] if nvidia() else []
        self.model = ""
        self.voices = 0
        self.override = ""         # устройство на время проверки скорости (bench.py)

    @property
    def models(self):
        return os.path.join(paths.MODELS, "espeech")

    @property
    def prefer(self):
        v = (env("ESPEECH_DEVICE") or self.override or (self.s.get("espeech_device", "") if self.s else "")
             or "auto").lower()
        return v if v in DEVICES else "auto"

    def public(self):
        return dict(backend=self.backend, prefer=self.prefer, model=self.model, voices=self.voices,
                    backends=[n for n, _ in self.backends], devices=list(DEVICES), models_dir=self.models,
                    device_env=self.DEVICE_ENV,
                    locked=dict(backend=bool(env("ESPEECH_DEVICE")), model=True))

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
        return f"устройство {self.backend}, голосов {self.voices}"

    @staticmethod
    def noise(line):
        return False

    # --- аргументы ---
    async def args(self, host, port):
        if not os.path.isfile(self.server):
            raise Unavailable(tr("нет {path}", path=self.server))
        if not package_root(self.server):
            raise Unavailable(tr("рядом с {path} нет пакета espeech", path=self.server))
        self.model = ""
        if not os.path.isfile(os.path.join(self.models, WEIGHTS)):
            raise Unavailable(tr("нет модели ESpeech: соберите папку {dir} (tools/espeech/prepare.py)",
                                 dir=self.models))
        self.model = MODEL_NAME
        cuda = self.prefer in ("auto", "cuda") and bool(self.backends)
        self.backend = "CUDA0" if cuda else "CPU"
        self.voices = len(glob.glob(os.path.join(paths.VOICES, "*.espeech")) +
                          glob.glob(os.path.join(paths.VOICES, "*", "*.espeech")))
        return [self.server, "-m", "espeech", "--models", self.models, "--voices", paths.VOICES,
                "--host", host, "--port", str(port), "--device", "cuda" if cuda else "cpu"]
