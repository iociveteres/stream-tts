"""Переменные окружения и пути (пути по способу запуска — в paths.py)."""
from .paths import DATA, HERE, PROJECT, env

PORT = int(env("STREAM_TTS_PORT", "8090"))
BIND = env("STREAM_TTS_BIND", "127.0.0.1")
# Внутри контейнера сервис слушает 0.0.0.0:8090, а BIND/PORT задают проброс порта на хосте
LISTEN_HOST = env("STREAM_TTS_LISTEN_HOST", BIND)
LISTEN_PORT = int(env("STREAM_TTS_LISTEN_PORT", str(PORT)))
PASSWORD = env("STREAM_TTS_PASSWORD")

# Задан COSYVOICE_URL или TTS_SERVERS — внешние серверы (Docker, другая машина). Нет — stream-tts сам запускает
# сервер выбранной модели (runtime.py, engines/), а если ни одного нет — по-старому http://cosyvoice:8080.
COSYVOICE_URL = env("COSYVOICE_URL").rstrip("/")
COSYVOICE_API_KEY = env("COSYVOICE_API_KEY")


def _servers():
    """TTS_SERVERS=cosyvoice=http://…,espeech=http://… — внешние серверы движков (выбор модели — в админке);
    COSYVOICE_URL — то же, что cosyvoice=… (как до второй модели)."""
    out = {}
    for part in env("TTS_SERVERS").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip().rstrip("/")
    if COSYVOICE_URL:
        out.setdefault("cosyvoice", COSYVOICE_URL)
    return out


TTS_SERVERS = _servers()
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
