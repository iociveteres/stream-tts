"""supertonic-server — синтез Supertonic 3 (Supertone, ONNX, только CPU, 10 готовых голосов) для stream-tts: HTTP API
как у cosyvoice-server. Запуск: python -m supertonic --models models/supertonic --voices voices (server.py) из корня
проекта: нормализатор русского текста — из espeech/ (text.py). Устройство — docs/supertonic.md.

Модули: server.py — HTTP API и запуск; model.py — синтез (перенос helper.py официального репозитория, MIT —
THIRD-PARTY.txt); voices.py — папка голосов <имя>.supertonic; text.py — подготовка текста."""
