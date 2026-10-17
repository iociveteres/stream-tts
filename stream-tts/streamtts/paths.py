"""Пути по способу запуска: из исходников проекта, в Docker или собранной программой (Nuitka standalone).

- Программа и статика только читаются: рядом с кодом (в сборке — папка exe, Nuitka кладёт туда данные пакета).
- Данные (настройки, SQLite, примеры), модели и голоса: в исходниках проекта — его папки stream-tts-data/,
  models/, voices/, как раньше; иначе %LOCALAPPDATA%\\StreamTTS\\ на Windows, $XDG_DATA_HOME/stream-tts на Linux.
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


_BASE = _REPO if FROM_SOURCE else user_data()
DATA = env("STREAM_TTS_DATA", os.path.join(_BASE, "stream-tts-data") if FROM_SOURCE else _BASE)
MODELS = env("STREAM_TTS_MODELS", os.path.join(_BASE, "models"))
VOICES = env("STREAM_TTS_VOICES", os.path.join(_BASE, "voices"))
# Корень проекта: voice-refs/*/manifest.json и скрипты эффектов (post) ищутся от него
PROJECT = env("STREAM_TTS_PROJECT", _REPO if FROM_SOURCE else _BASE)
RUNTIME = env("STREAM_TTS_RUNTIME", os.path.join(APP, "runtime"))


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
