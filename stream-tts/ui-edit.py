"""Режим правки строк интерфейса stream-tts (в разработке): админка с кнопкой «✎ Текст» на поддельном сервере
синтеза из e2e-тестов (tests/e2e/fake_cosyvoice.py: CosyVoice и ESpeech, модель — на вкладке «Сервер»; голоса
alpha, bravo, charlie, вместо речи синус). Видеокарта, модель и Docker-версия на 8090 не нужны и не задеваются,
Twitch не подключён (свои данные).

Щелчок по надписи в режиме правки — окно с её строками; правки копятся в stream-tts-data/ui-edits.json
({русская строка: {"text", "note", "where"}}), сразу видны на странице и переживают перезапуск. Перенос в код
(строки, en.json) делается отдельно, файл после этого удаляется. Подробнее — docs/stream-tts.md, «Правка текста
интерфейса».

usage: stream-tts/.venv/Scripts/python stream-tts/ui-edit.py      (конфигурация stream-tts-ui-edit, порт 8099)
Переменные, если заданы, не меняются: STREAM_TTS_PORT, STREAM_TTS_DATA, STREAM_TTS_UI_EDITS и др."""
import asyncio, os, runpy, sys, threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "stream-tts-data", "ui-edit")

sys.path.insert(0, os.path.join(ROOT, "tests", "e2e"))
from fake_cosyvoice import FakeCosy  # noqa: E402

# Оба сервера: модель переключается на вкладке «Сервер», и видны строки и CosyVoice, и ESpeech
loop = asyncio.new_event_loop()
cosy, espeech = FakeCosy(frontend=True), FakeCosy(engine="espeech")
loop.run_until_complete(cosy.start())
loop.run_until_complete(espeech.start())
threading.Thread(target=loop.run_forever, daemon=True).start()

for d in ("models", "voices"):
    os.makedirs(os.path.join(DATA, d), exist_ok=True)
for k, v in dict(STREAM_TTS_PORT="8099", STREAM_TTS_DATA=DATA, STREAM_TTS_MODELS=os.path.join(DATA, "models"),
                 STREAM_TTS_VOICES=os.path.join(DATA, "voices"), STREAM_TTS_LANG="ru",
                 STREAM_TTS_UI_EDITS=os.path.join(ROOT, "stream-tts-data", "ui-edits.json")).items():
    os.environ.setdefault(k, v)
os.environ["STREAM_TTS_TEST_SERVERS"] = f"cosyvoice={cosy.url},espeech={espeech.url}"
for k in ("TWITCH_CLIENT_ID", "STREAM_TTS_PASSWORD"):
    os.environ.pop(k, None)

sys.path.insert(0, HERE)
runpy.run_module("streamtts", run_name="__main__")
