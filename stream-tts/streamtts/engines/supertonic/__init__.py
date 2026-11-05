"""Движок Supertonic 3 (Supertone, ONNX): supertonic-server из supertonic/ проекта. Только процессор и только готовые
голоса M1–M5, F1–F5: своего клонирования у модели нет, редактор голоса для неё не работает. Описание, запуск сервера
(launch.py), клиент (client.py), отказ редактора голоса (editor.py) и загрузка модели с готовыми голосами по кнопке
(download.py, его импортирует install.py)."""
from .client import Client
from .editor import Maker
from .launch import DEVICES, Launcher, find_server

ID = "supertonic"
TITLE = "Supertonic 3"
# Голос — JSON стиля (style_ttl, style_dp): voices/<имя>.supertonic или voices/<набор>/<имя>.supertonic; готовые
# голоса модели — набор supertonic (имена — download.VOICES)
VOICE_EXT = ".supertonic"
