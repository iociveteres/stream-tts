"""Переменные окружения и пути (пути по способу запуска — в paths.py)."""
from .paths import DATA, HERE, PROJECT, env

PORT = int(env("STREAM_TTS_PORT", "8090"))
BIND = env("STREAM_TTS_BIND", "127.0.0.1")
# Внутри контейнера сервис слушает 0.0.0.0:8090, а BIND/PORT задают проброс порта на хосте
LISTEN_HOST = env("STREAM_TTS_LISTEN_HOST", BIND)
LISTEN_PORT = int(env("STREAM_TTS_LISTEN_PORT", str(PORT)))
PASSWORD = env("STREAM_TTS_PASSWORD")


def _test_servers():
    """Только для тестов (tests/e2e, stream-tts/ui-edit.py): готовые (поддельные) серверы моделей вместо встроенного,
    STREAM_TTS_TEST_SERVERS=cosyvoice=http://…,espeech=http://…. Иначе stream-tts сам запускает сервер выбранной
    модели (runtime.py, engines/<id>/launch.py)."""
    out = {}
    for part in env("STREAM_TTS_TEST_SERVERS").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip().rstrip("/")
    return out


TEST_SERVERS = _test_servers()
# Только для тестов (tests/e2e/fake_twitch.py): поддельный Twitch вместо настоящего, STREAM_TTS_TEST_TWITCH=http://…
# (вход — <адрес>/oauth2, Helix — <адрес>/helix, EventSub — ws://…/ws), см. twitch.py
TEST_TWITCH = env("STREAM_TTS_TEST_TWITCH").rstrip("/")
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
