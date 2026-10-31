"""Запуск встроенного espeech-server (общий наблюдатель процесса — runtime.py).

Сервер — Python-пакет espeech (папка espeech/ проекта), запускается python -m espeech интерпретатором его окружения:
- ESPEECH_SERVER — путь к python(.exe) или к папке с ним (или к папке espeech/ с .venv); пакет ищется вверх от
  интерпретатора (первая папка, где лежит espeech/server.py) и попадает в PYTHONPATH;
- программа для Windows: код (пакет и DLL-заглушки) ставит установщик в CODE (runtime/espeech), а встраиваемый
  Python с torch и пакетами качается по кнопке на вкладке «Сервер» в RUNTIME (download.py; ESPEECH_RUNTIME —
  другая папка). Путь к пакету задаёт python311._pth (write_pth: PYTHONPATH встраиваемый Python не читает), torch
  находит заглушки и cuBLAS по ESPEECH_DLL_DIRS (espeech/__init__.py): cuBLAS — настоящий из CUDA cosyvoice-server
  (paths.RUNTIME), а без неё — заглушки, и тогда синтез только на процессоре;
- из исходников — espeech/.venv проекта.

Аргументы: --models paths.MODELS/espeech (download.py или tools/espeech/prepare.py; без модели сервер не запускается),
--voices paths.VOICES (голоса <имя>.espeech и <набор>/<имя>.espeech; сервер перечитывает папку сам),
--user-accents <данные>/espeech/accents.json (словарь ударений пользователя, правится из админки),
--device auto|cuda|cpu: ESPEECH_DEVICE, иначе выбор в админке (settings.espeech_device); auto — решает torch
(device()), без CUDA в бэкендах — cpu. Порт — ESPEECH_PORT (иначе свободный), адрес сервера — ESPEECH_HOST
(по умолчанию 127.0.0.1)."""
import glob, os, shutil

from ... import paths
from ...i18n import tr
from ...paths import EXE, env
from ...runtime import Unavailable

DEVICES = ("auto", "cuda", "cpu")
WEIGHTS = "espeech_fp16.safetensors"
MODEL_NAME = "ESpeech-TTS-1_SFT-256K"
PY = os.path.join("Scripts", "python.exe") if os.name == "nt" else os.path.join("bin", "python")
# Пути абсолютные: процессы рантайма запускаются в других папках
CODE = os.path.abspath(os.path.join(paths.RUNTIME, "espeech"))                     # от установщика: espeech/, stubs/
RUNTIME = os.path.abspath(env("ESPEECH_RUNTIME", os.path.join(paths.APP, "espeech-runtime")))   # Python с torch
CUBLAS = ("cublas64_13.dll", "cublasLt64_13.dll")
# Готовая модель: основные файлы и последний, который пишет espeech/prepare.py (он собирает по порядку и пропускает
# готовое, так что есть последний — есть и все, outputs()); без импорта пакета espeech — его нет в программе
MODEL_FILES = (WEIGHTS, "vocab.txt", "vocos.safetensors", "ruaccent/accents.sqlite", "ruaccent/onnx/turbo3.1.onnx",
               "ruaccent/nn/nn_yo_homograph_resolver/tokenizer.json")


def _python_in(d):
    for p in (os.path.join(d, "python" + EXE), os.path.join(d, PY), os.path.join(d, ".venv", PY)):
        if os.path.isfile(p):
            return p
    return None


def downloadable():
    """Программа для Windows: код espeech-server от установщика, Python с torch качается по кнопке."""
    return os.name == "nt" and os.path.isfile(os.path.join(CODE, "espeech", "server.py"))


def find_server():
    """Интерпретатор espeech-server или None. У программы для Windows — python.exe рантайма,
    даже если он ещё не скачан: модель видна в выборе, а сервер ждёт загрузки (args)."""
    p = env("ESPEECH_SERVER")
    if p:
        return (_python_in(p) or p) if os.path.isdir(p) else p
    if downloadable():
        return os.path.join(RUNTIME, "python.exe")
    p = _python_in(os.path.join(paths.APP, "espeech"))
    return p if p and package_root(p) else None


def model_ready(models):
    return all(os.path.isfile(os.path.join(models, *f.split("/"))) for f in MODEL_FILES)


def cublas_dir():
    """Папка с настоящим cuBLAS (CUDA cosyvoice-server) или None."""
    d = os.path.abspath(paths.RUNTIME)
    return d if all(os.path.isfile(os.path.join(d, f)) for f in CUBLAS) else None


def write_pth(runtime_dir):
    """sys.path встраиваемого Python: его библиотека, site-packages и папка с пакетом espeech (относительно: папку
    программы можно перенести)."""
    try:
        code = os.path.relpath(CODE, runtime_dir)
    except ValueError:   # другой диск
        code = CODE
    text = "\n".join(("python311.zip", ".", r"Lib\site-packages", code)) + "\n"
    path = os.path.join(runtime_dir, "python311._pth")
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def process_env(runtime_dir=None):
    """Окружение процесса Python ESpeech. Скачанному рантайму — папки DLL: заглушки и cuBLAS (или его заглушки)."""
    e = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    for k in ("PYTHONHOME", "PYTHONPATH", "CUDA_MODULE_LOADING"):   # EAGER — +5 ГБ памяти (docs/espeech.md)
        e.pop(k, None)
    if runtime_dir:
        stubs = os.path.join(CODE, "stubs")
        e["ESPEECH_DLL_DIRS"] = os.pathsep.join((stubs, cublas_dir() or os.path.join(stubs, "cublas")))
    return e


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


def device(prefer, cuda):
    """--device сервера. auto отдаём серверу: nvcuda.dll бывает и без рабочего драйвера (torch тогда падает на
    «Found no NVIDIA driver»), а torch.cuda.is_available() проверяет драйвер по-настоящему."""
    return prefer if cuda else "cpu"


class Launcher:
    ID = "espeech"            # логгер вывода сервера
    NAME = "espeech-server"
    DEVICE_KEY, DEVICE_ENV = "espeech_device", "ESPEECH_DEVICE"

    def __init__(self, server, settings=None):
        self.server, self.dir = server, os.path.dirname(os.path.abspath(server))
        self.s = settings
        # скачиваемый рантайм программы для Windows (download.py): пакет — в CODE
        self.downloaded = downloadable() and os.path.normcase(os.path.abspath(server)) == \
            os.path.normcase(os.path.abspath(os.path.join(RUNTIME, "python.exe")))
        self.port = int(env("ESPEECH_PORT", "0"))
        self.host = env("ESPEECH_HOST", "127.0.0.1")
        self.backend = ""          # CUDA0 | CPU — как у CosyVoice, для админки и проверки скорости
        self.model = ""
        self.voices = 0
        self.override = ""         # устройство на время проверки скорости (bench.py)

    @property
    def backends(self):
        """Видеокарта NVIDIA; скачанному рантайму нужен ещё cuBLAS из CUDA cosyvoice-server (её качают вместе)."""
        return [("CUDA0", False)] if nvidia() and (not self.downloaded or cublas_dir()) else []

    @backends.setter
    def backends(self, value):   # install.py сбрасывает список у CosyVoice после загрузки CUDA; здесь он вычисляется
        pass

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
        if self.downloaded:
            return process_env(RUNTIME)
        e = process_env()
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
        self.model = ""
        if self.downloaded:
            from . import download
            if download.runtime_state() != "ok" or not model_ready(self.models):
                raise Unavailable(tr("ESpeech не скачан: скачайте его на вкладке «Сервер»"))
            write_pth(RUNTIME)   # папку программы могли перенести
        else:
            if not os.path.isfile(self.server):
                raise Unavailable(tr("нет {path}", path=self.server))
            if not package_root(self.server):
                raise Unavailable(tr("рядом с {path} нет пакета espeech", path=self.server))
            if not os.path.isfile(os.path.join(self.models, WEIGHTS)):
                raise Unavailable(tr("нет модели ESpeech: соберите папку {dir} (tools/espeech/prepare.py)",
                                     dir=self.models))
        self.model = MODEL_NAME
        dev = device(self.prefer, self.prefer in ("auto", "cuda") and bool(self.backends))
        self.backend = "CPU" if dev == "cpu" else "CUDA0"   # auto уточнит sync() по /status
        self.voices = len(glob.glob(os.path.join(paths.VOICES, "*.espeech")) +
                          glob.glob(os.path.join(paths.VOICES, "*", "*.espeech")))
        a = os.path.abspath   # сервер работает в папке голосов
        return [self.server, "-m", "espeech", "--models", a(self.models), "--voices", a(paths.VOICES),
                "--user-accents", a(os.path.join(paths.DATA, "espeech", "accents.json")),
                "--host", host, "--port", str(port), "--device", dev]

    def sync(self, info):
        """Устройство, на котором сервер загрузил модель (GET /status): auto решает torch, а не nvcuda.dll."""
        if info.get("device"):
            self.backend = "CUDA0" if info["device"] == "cuda" else "CPU"
