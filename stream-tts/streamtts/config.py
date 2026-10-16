"""Переменные окружения и пути."""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(HERE))  # stream-tts/streamtts -> корень проекта


def env(name, default=""):
    return os.environ.get(name, "").strip() or default


# /data в контейнере, ./stream-tts-data при запуске из исходников
DATA = env("STREAM_TTS_DATA", os.path.join(_REPO, "stream-tts-data"))
# Корень проекта: voice-refs/ и скрипты эффектов (post) берутся относительно него
PROJECT = env("STREAM_TTS_PROJECT", _REPO)

PORT = int(env("STREAM_TTS_PORT", "8090"))
BIND = env("STREAM_TTS_BIND", "127.0.0.1")
# Внутри контейнера сервис слушает 0.0.0.0:8090, а BIND/PORT задают проброс порта на хосте
LISTEN_HOST = env("STREAM_TTS_LISTEN_HOST", BIND)
LISTEN_PORT = int(env("STREAM_TTS_LISTEN_PORT", str(PORT)))
PASSWORD = env("STREAM_TTS_PASSWORD")

COSYVOICE_URL = env("COSYVOICE_URL", "http://cosyvoice:8080").rstrip("/")
COSYVOICE_API_KEY = env("COSYVOICE_API_KEY")
TWITCH_CLIENT_ID = env("TWITCH_CLIENT_ID")

LOCAL_BINDS = {"127.0.0.1", "localhost", "::1"}

SR = 24000                 # частота синтеза cosyvoice
TARGET_LUFS = -16.0
BEEP_BELOW_DB = 9.0        # писк вместо мата тише речи
CHARS_PER_SEC = 12.0       # расчётный темп речи для жёсткой обрезки
MAX_LEN_FACTOR = 1.8
MAX_SECONDS = 60.0
MIN_SECONDS = 2.0          # нижний предел обрезки: короткая фраза с ником «Ку» всё равно 1–2 с
HISTORY_DAYS = 30
