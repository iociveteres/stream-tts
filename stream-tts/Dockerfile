# syntax=docker/dockerfile:1
# stream-tts: озвучка наград Twitch голосами cosyvoice-server.
# Python 3.15 (вышел 14.10.2026): колёса numpy, aiohttp, yarl, multidict, propcache уже есть под cp315,
# frozenlist ставится чистым py3-колесом. Если сборка упадёт на компиляции, верните PYTHON=3.14.
ARG PYTHON=3.15
FROM python:${PYTHON}-slim

# ffmpeg: mp3 для примеров голосов и чтение звука в эффектах (post)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# --only-binary: без компилятора в образе сборка из исходников всё равно не удалась бы, пусть падает сразу
RUN pip install --no-cache-dir --only-binary=:all: -r requirements.txt
COPY streamtts ./streamtts

ENV PYTHONUNBUFFERED=1 \
    STREAM_TTS_DATA=/data \
    STREAM_TTS_PROJECT=/project \
    STREAM_TTS_LISTEN_HOST=0.0.0.0 \
    STREAM_TTS_LISTEN_PORT=8090
EXPOSE 8090
CMD ["python", "-m", "streamtts"]
