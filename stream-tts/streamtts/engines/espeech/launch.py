"""Запуск встроенного espeech-server (общий наблюдатель процесса — runtime.py).

Сервер — Python-пакет espeech (папка espeech/ проекта), запускается python -m espeech интерпретатором его окружения:
- ESPEECH_SERVER — путь к python(.exe) или к папке с ним (или к папке espeech/ с .venv); пакет ищется вверх от
  интерпретатора (первая папка, где лежит espeech/server.py) и попадает в PYTHONPATH;
- программа для Windows и образ Docker (downloadable()): код (пакет и заглушки библиотек CUDA) лежит в CODE
  (<paths.RUNTIME>/espeech: у программы его ставит установщик, в образе — Dockerfile), а Python 3.14 с torch и
  пакетами качается по кнопке на вкладке «Сервер» в RUNTIME (download.py; ESPEECH_RUNTIME — другая папка, в Docker —
  том). Путь к пакету — в sys.path рантайма (write_pth: python3XX._pth встраиваемого Python на Windows, он не читает
  PYTHONPATH; espeech.pth в site-packages на Linux). torch находит заглушки и cuBLAS по ESPEECH_DLL_DIRS (dll_dirs(),
  espeech/__init__.py): cuBLAS (на Linux и cudart) — настоящий из CUDA cosyvoice-server (скачанной в paths.CUDA или
  лежащей в paths.RUNTIME), а без неё — заглушки, и тогда синтез только на процессоре;
- из исходников — espeech/.venv проекта.

Аргументы: --models paths.MODELS/espeech (download.py или tools/espeech/prepare.py; без модели сервер не запускается),
--voices paths.VOICES (голоса <имя>.espeech и <набор>/<имя>.espeech; сервер перечитывает папку сам),
--user-accents <данные>/espeech/accents.json (словарь ударений пользователя, правится из админки),
--device auto|cuda|cpu: ESPEECH_DEVICE, иначе выбор в админке (settings.espeech_device); auto — решает torch
(device()), без CUDA в бэкендах — cpu. Порт — ESPEECH_PORT (иначе свободный), адрес сервера — ESPEECH_HOST
(по умолчанию 127.0.0.1)."""
import functools, glob, hashlib, json, os, shutil

from ... import paths
from ...i18n import tr
from ...paths import EXE, env
from ...runtime import Unavailable

DEVICES = ("auto", "cuda", "cpu")
WEIGHTS = "espeech_fp16.safetensors"
MODEL_NAME = "ESpeech-TTS-1_SFT-256K"
PY = os.path.join("Scripts", "python.exe") if os.name == "nt" else os.path.join("bin", "python")
# Пути абсолютные: процессы рантайма запускаются в других папках
CODE = os.path.abspath(os.path.join(paths.RUNTIME, "espeech"))      # от установщика или из образа: espeech/, stubs/
RUNTIME = os.path.abspath(env("ESPEECH_RUNTIME", os.path.join(paths.APP, "espeech-runtime")))   # Python с torch
# Интерпретатор скачанного рантайма: встраиваемый Python на Windows, python-build-standalone на Linux
RUNTIME_PYTHON = "python.exe" if os.name == "nt" else os.path.join("bin", "python3")
# Настоящие библиотеки CUDA cosyvoice-server, которые нужны torch (downloads.json: espeech.runtime.<система>.cublas);
# на Linux torch связан с cudart динамически, и порядок — порядок загрузки: cuBLAS требует cuBLASLt
CUBLAS = (("cublas64_13.dll", "cublasLt64_13.dll") if os.name == "nt" else
          ("libcudart.so.13", "libcublasLt.so.13", "libcublas.so.13"))
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
    """Программа для Windows и образ Docker: код espeech-server в CODE, Python с torch качается по кнопке."""
    return os.path.isfile(os.path.join(CODE, "espeech", "server.py"))


def runtime_python(runtime_dir=None):
    return os.path.join(runtime_dir or RUNTIME, RUNTIME_PYTHON)


def site_packages(runtime_dir):
    """site-packages рантайма: Lib/site-packages встраиваемого Python, lib/python3.X/site-packages на Linux."""
    if os.name == "nt":
        return os.path.join(runtime_dir, "Lib", "site-packages")
    lib = glob.glob(os.path.join(runtime_dir, "lib", "python3.*", ""))
    if len(lib) != 1:
        raise Unavailable(tr("рантайм ESpeech повреждён: нет библиотеки Python в {dir}", dir=runtime_dir))
    return os.path.join(lib[0], "site-packages")


def find_server():
    """Интерпретатор espeech-server или None. У скачиваемого рантайма — его Python, даже если он ещё не скачан:
    модель видна в выборе, а сервер ждёт загрузки (args)."""
    p = env("ESPEECH_SERVER")
    if p:
        return (_python_in(p) or p) if os.path.isdir(p) else p
    if downloadable():
        return runtime_python()
    p = _python_in(os.path.join(paths.APP, "espeech"))
    return p if p and package_root(p) else None


def model_ready(models):
    return all(os.path.isfile(os.path.join(models, *f.split("/"))) for f in MODEL_FILES)


# Закреплённый рантайм — раздел espeech downloads.json (packaging/pin-espeech.py). Он и проверка «рантайм скачан и
# свежий» (её зовёт args()) — здесь, а не в download.py: тот импортирует launch, а downloads.py — движки
# (engines.cosyvoice), так что оттуда — цикл импорта (tests/e2e/test_layers.py)
SYSTEM = "windows" if os.name == "nt" else "linux"   # рантайм в downloads.json


@functools.cache
def spec():
    with open(os.path.join(paths.HERE, "downloads.json"), encoding="utf-8") as f:
        return json.load(f)["espeech"]


def runtime_spec():
    return spec()["runtime"][SYSTEM]


def runtime_version():
    """Хэш закреплённого рантайма своей системы: другой — качать заново."""
    return hashlib.sha256(json.dumps(runtime_spec(), sort_keys=True).encode()).hexdigest()[:16]


def runtime_state():
    """ok | missing | outdated."""
    try:
        with open(os.path.join(RUNTIME, "version.txt"), encoding="utf-8") as f:
            have = f.read().strip()
    except OSError:
        return "missing"
    if not os.path.isfile(runtime_python()):
        return "missing"
    return "ok" if have == runtime_version() else "outdated"


def cublas_dir():
    """Папка с настоящим cuBLAS (CUDA cosyvoice-server: скачанная в paths.CUDA или в рантайме) или None."""
    for d in filter(None, (paths.CUDA, paths.RUNTIME)):
        d = os.path.abspath(d)
        if all(os.path.isfile(os.path.join(d, f)) for f in CUBLAS):
            return d
    return None


def write_pth(runtime_dir):
    """Папка с пакетом espeech — в sys.path рантайма (относительно: папку программы можно перенести; путь
    переписывается при каждом запуске, так что и том Docker можно смонтировать в другое место):
    - встраиваемый Python (Windows): python3XX._pth — его библиотека, site-packages и эта папка. Имя — по библиотеке
      python3XX.zip рядом: версию Python закрепляет downloads.json, здесь она не повторяется;
    - Linux: espeech.pth в site-packages (пути из него добавляет site)."""
    if os.name == "nt":
        try:
            code = os.path.relpath(CODE, runtime_dir)
        except ValueError:   # другой диск
            code = CODE
        lib = glob.glob(os.path.join(runtime_dir, "python3*.zip"))
        if len(lib) != 1:
            raise Unavailable(tr("рантайм ESpeech повреждён: нет библиотеки Python в {dir}", dir=runtime_dir))
        stem = os.path.basename(lib[0])[:-4]
        text = "\n".join((stem + ".zip", ".", r"Lib\site-packages", code)) + "\n"
        path = os.path.join(runtime_dir, stem + "._pth")
    else:
        site = site_packages(runtime_dir)
        text = os.path.relpath(CODE, site) + "\n"
        path = os.path.join(site, "espeech.pth")
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return
    except OSError:
        pass
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def dll_dirs():
    """ESPEECH_DLL_DIRS скачанного рантайма: заглушки и cuBLAS (или его заглушки, если CUDA нет).
    Windows — папки поиска DLL; Linux — библиотеки, которые espeech загружает до torch (папка — все lib*.so* в ней):
    ld.so находит зависимость по SONAME среди уже загруженных, а LD_LIBRARY_PATH=paths.RUNTIME нельзя — там и свой
    libonnxruntime.so cosyvoice-server."""
    stubs, real = os.path.join(CODE, "stubs"), cublas_dir()
    if os.name == "nt":
        return [stubs, real or os.path.join(stubs, "cublas")]
    return ([os.path.join(real, f) for f in CUBLAS] if real else [os.path.join(stubs, "cublas")]) + [stubs]


def process_env(runtime_dir=None):
    """Окружение процесса Python ESpeech. Скачанному рантайму — библиотеки CUDA (dll_dirs)."""
    e = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    for k in ("PYTHONHOME", "PYTHONPATH", "CUDA_MODULE_LOADING"):   # EAGER — +5 ГБ памяти (docs/espeech.md)
        e.pop(k, None)
    if runtime_dir:
        e["ESPEECH_DLL_DIRS"] = os.pathsep.join(dll_dirs())
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
    return paths.nvidia() or os.name != "nt" and bool(shutil.which("nvidia-smi"))


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
        # скачиваемый рантайм (программа для Windows, образ Docker; download.py): пакет — в CODE
        self.downloaded = downloadable() and os.path.normcase(os.path.abspath(server)) == \
            os.path.normcase(os.path.abspath(runtime_python()))
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
            if runtime_state() != "ok" or not model_ready(self.models):
                raise Unavailable(tr("ESpeech не скачан. Скачать — на вкладке «Загрузки»"))
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

    def has_model(self):
        """Модель скачана: устаревший рантайм — тоже (сервер ждёт обновления, админка его предлагает)."""
        if self.downloaded:
            return runtime_state() != "missing" and model_ready(self.models)
        return os.path.isfile(os.path.join(self.models, WEIGHTS))

    def sync(self, info):
        """Устройство, на котором сервер загрузил модель (GET /status): auto решает torch, а не nvcuda.dll."""
        if info.get("device"):
            self.backend = "CUDA0" if info["device"] == "cuda" else "CPU"
