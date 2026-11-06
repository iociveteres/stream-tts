"""Движок ESpeech: espeech-server из espeech/ проекта (F5-TTS + RUAccent, docs/espeech.md). Описание, запуск
сервера (launch.py), клиент (client.py), особенности редактора голоса (editor.py), обучение LoRA голоса (train.py,
его импортирует __main__.py) и загрузка рантайма и модели программой для Windows и в Docker (download.py; из
исходников папку модели собирает tools/espeech/prepare.py)."""
from .client import Client
from .editor import Maker
from .launch import DEVICES, Launcher, find_server

ID = "espeech"
TITLE = "ESpeech"
VOICE_EXT = ".espeech"   # голос: voices/<имя>.espeech или voices/<набор>/<имя>.espeech (tools/espeech/make-voice.py)
