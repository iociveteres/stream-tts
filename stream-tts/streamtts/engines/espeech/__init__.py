"""Движок ESpeech: espeech-server из espeech/ проекта (F5-TTS + RUAccent, docs/espeech.md). Описание, запуск в режиме
managed (launch.py), клиент (client.py) и особенности редактора голоса (editor.py). Загрузок пока нет: папку модели собирает tools/espeech/prepare.py, поставка
в установщике — этап 4."""
from .client import Client
from .editor import Maker
from .launch import DEVICES, Launcher, find_server

ID = "espeech"
TITLE = "ESpeech"
VOICE_EXT = ".espeech"   # голос: voices/<имя>.espeech или voices/<набор>/<имя>.espeech (tools/espeech/make-voice.py)
