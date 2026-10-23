"""Движки синтеза: у каждого свой пакет engines/<id>/ — описание (ID, TITLE, VOICE_EXT, DEVICES, загрузки),
запуск в режиме managed (Launcher, find_server) и клиент (Client, общий протокол — client.py). Модули лежат внутри
пакета streamtts: собранная программа (Nuitka) импортов извне не видит."""
from . import cosyvoice

ENGINES = {cosyvoice.ID: cosyvoice}
