"""Пути по способу запуска: из исходников проекта, в Docker или собранной программой (Nuitka standalone).

- Программа и статика только читаются: рядом с кодом (в сборке — папка exe, Nuitka кладёт туда данные пакета).
- Данные (настройки, SQLite, примеры), модели и голоса: в исходниках проекта — его папки stream-tts-data/,
  models/, voices/, как раньше; у программы, поставленной установщиком (рядом с exe лежит файл-признак portable), —
  data/, models/, voices/ в её папке; иначе %LOCALAPPDATA%\\StreamTTS\\ на Windows, $XDG_DATA_HOME/stream-tts на
  Linux.
- Рантайм (cosyvoice-server, DLL, ffmpeg): runtime/ рядом с программой (в исходниках — в корне проекта).
Переменные STREAM_TTS_DATA, STREAM_TTS_MODELS, STREAM_TTS_VOICES, STREAM_TTS_PROJECT, STREAM_TTS_RUNTIME
переопределяют всё (Docker задаёт STREAM_TTS_DATA и STREAM_TTS_PROJECT в Dockerfile)."""
import functools, os, shutil, sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Nuitka не задаёт sys.frozen: у скомпилированных модулей есть глобальная __compiled__
FROZEN = "__compiled__" in globals() or bool(getattr(sys, "frozen", False))
EXE = ".exe" if os.name == "nt" else ""

# Корень проекта: stream-tts/streamtts -> корень; признак — docker-compose.yml рядом
_REPO = os.path.dirname(os.path.dirname(HERE))
FROM_SOURCE = not FROZEN and os.path.isfile(os.path.join(_REPO, "docker-compose.yml"))
# Папка программы: у собранной — папка exe, из исходников — корень проекта
APP = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else _REPO


def _program():
    """Исполняемый файл, которым запущен stream-tts. У сборки Nuitka sys.executable — python.exe рядом с программой
    (его нет: так Nuitka изображает обычный Python), настоящий файл знает ОС."""
    if not FROZEN:
        return sys.executable
    if os.name == "nt":
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf)):
            return buf.value
    return os.path.abspath(sys.argv[0])


# Программа для Windows запускает ею же supertonic-server (engines/supertonic/launch.py)
PROGRAM = _program()
# Файлы, которые идут вместе с кодом (effects/): в сборке лежат рядом с exe
BUNDLE = APP
STATIC = os.path.join(HERE, "static")


def env(name, default=""):
    return os.environ.get(name, "").strip() or default


def user_data():
    if os.name == "nt":
        base = env("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "StreamTTS")
    return os.path.join(env("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share"), "stream-tts")


# Установщик (StreamTTS.iss) ставит программу для одного пользователя в папку, куда можно писать, и кладёт туда этот
# признак: всё хранится в папке программы
PORTABLE = not FROM_SOURCE and os.path.isfile(os.path.join(APP, "portable"))
_BASE = _REPO if FROM_SOURCE else APP if PORTABLE else user_data()
DATA = env("STREAM_TTS_DATA", os.path.join(_BASE, "stream-tts-data") if FROM_SOURCE
           else os.path.join(_BASE, "data") if PORTABLE else _BASE)
MODELS = env("STREAM_TTS_MODELS", os.path.join(_BASE, "models"))
VOICES = env("STREAM_TTS_VOICES", os.path.join(_BASE, "voices"))
# Корень проекта: voice-refs/<модель>/<набор>/manifest.json и скрипты эффектов (post) ищутся от него
PROJECT = env("STREAM_TTS_PROJECT", _REPO if FROM_SOURCE else DATA)
RUNTIME = env("STREAM_TTS_RUNTIME", os.path.join(APP, "runtime"))
# Куда кнопка на вкладке «Загрузки» кладёт CUDA (ggml-cuda и рантайм CUDA 13, install.py): пусто — к cosyvoice-server
# (программа для Windows), в Docker — том /cuda: образ один, CUDA качают только те, у кого видеокарта NVIDIA
CUDA = env("STREAM_TTS_CUDA")
# Драйвер NVIDIA на Linux: libcuda.so.1 подкладывает в контейнер --gpus (в Docker Desktop с WSL2 /dev/nvidia* нет —
# там /dev/dxg и библиотеки в /usr/lib/wsl/lib)
_LIBCUDA = ("/usr/lib/x86_64-linux-gnu/libcuda.so.1", "/usr/lib/wsl/lib/libcuda.so.1", "/usr/lib64/libcuda.so.1")


def nvidia():
    """Есть ли драйвер NVIDIA (видеокарта, которой пригодится CUDA). Рабочий ли он, решает сам сервер синтеза."""
    if os.name == "nt":
        return os.path.isfile(os.path.join(env("SystemRoot", r"C:\Windows"), "System32", "nvcuda.dll"))
    return os.path.exists("/dev/nvidiactl") or any(os.path.exists(p) for p in _LIBCUDA)


def resource(rel):
    """Файл из проекта (PROJECT), а если его там нет — из поставки программы (BUNDLE)."""
    for base in (PROJECT, BUNDLE):
        p = os.path.join(base, rel)
        if os.path.exists(p):
            return p
    return os.path.join(PROJECT, rel)


def tool(name):
    """Программа из рантайма или рядом с программой, потом из PATH. Без находки — само имя (ошибка при запуске)."""
    for d in (RUNTIME, APP):
        p = os.path.join(d, name + EXE)
        if os.path.isfile(p):
            return p
    return shutil.which(name) or name


@functools.cache
def ffmpeg():
    return env("FFMPEG", tool("ffmpeg"))
