# syntax=docker/dockerfile:1
# Образ stream-tts из готовых релизов cosyvoice.cpp и llama.cpp: озвучка наград Twitch со встроенным cosyvoice-server
# (runtime.py и engines/cosyvoice/launch.py). Он же сервер для scripts/ и tools/ — порт 8080 контейнера.
# Собирается без CUDA: её (~590 МБ) качает кнопка на вкладке «Загрузки» в том /cuda (STREAM_TTS_CUDA, install.py), если
# контейнер видит видеокарту NVIDIA, — образ один для всех. GPU=cuda кладёт CUDA-бэкенд в сам образ (по умолчанию не
# нужно; CUDA_VERSION сверяет с downloads.json test_cuda_download.py). ubuntu 26.04: бинарники собраны в CI на
# ubuntu-26.04 (glibc 2.43, ICU 78), а в python:*-slim glibc 2.41.

ARG UBUNTU=ubuntu:26.04

# --- Стадия загрузки: curl нужен только здесь, в итоговые образы не попадает ---
FROM ${UBUNTU} AS fetch

ARG COSYVOICE_TAG=v0.2.0
ARG COSYVOICE_ASSET=cosyvoice-67d45fe-linux-x86_64-ffmpeg.tgz
# Релиз llama.cpp, ближайший к дате COSYVOICE_TAG: GGML в релизах cosyvoice.cpp
# не поставляется, его берут из llama.cpp (см. README cosyvoice.cpp).
ARG LLAMACPP_TAG=b11500
# GPU=cuda: GGML из CUDA-сборки llama.cpp (с libggml-cuda.so) плюс рантайм CUDA
# (cudart, cuBLAS). Нужен драйвер NVIDIA с поддержкой CUDA 13.
ARG GPU=none
ARG CUDA_VERSION=13.4

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    mkdir -p /opt/cosyvoice /tmp/llama; \
    curl -fsSL "https://github.com/Lourdle/cosyvoice.cpp/releases/download/${COSYVOICE_TAG}/${COSYVOICE_ASSET}" \
        | tar xz -C /opt/cosyvoice; \
    llama="https://github.com/ggml-org/llama.cpp/releases/download/${LLAMACPP_TAG}"; \
    case "$GPU" in \
        none) curl -fsSL "$llama/llama-${LLAMACPP_TAG}-bin-ubuntu-x64.tar.gz" | tar xz -C /tmp/llama ;; \
        cuda) curl -fsSL "$llama/llama-${LLAMACPP_TAG}-bin-ubuntu-cuda-${CUDA_VERSION}-x64.tar.gz" | tar xz -C /tmp/llama; \
              curl -fsSL "$llama/cudart-llama-${LLAMACPP_TAG}-bin-ubuntu-cuda-${CUDA_VERSION}-x64.tar.gz" | tar xz -C /tmp/llama; \
              find /tmp/llama -name 'libcu*.so*' -exec cp -a {} /opt/cosyvoice/ \; ; \
              ls /opt/cosyvoice/libcublasLt.so* /tmp/llama/*/libggml-cuda.so >/dev/null ;; \
        *) echo "GPU: none|cuda" >&2; exit 1 ;; \
    esac; \
    # Все libggml*, включая модули libggml-cpu-<isa>.so: сервер сам выбирает
    # подходящий под CPU вариант при старте (GGML_BACKEND_DL).
    find /tmp/llama -name 'libggml*.so*' -exec cp -a {} /opt/cosyvoice/ \; ; \
    ls /opt/cosyvoice/libggml-cpu-*.so >/dev/null; \
    chmod +x /opt/cosyvoice/cosyvoice-server /opt/cosyvoice/cosyvoice-cli /opt/cosyvoice/quantize

# --- Заглушки библиотек CUDA для ESpeech: gcc нужен только здесь ---
# torch ESpeech качается кнопкой в админке без пакетов nvidia-* (~3 ГБ): библиотеки, которые он загружает, а синтез и
# обучение LoRA не вызывают, заменяются заглушками по спискам символов packaging/linux/stubs (pin-espeech.py);
# cudart и cuBLAS — настоящие из скачанной CUDA (том /cuda) или тоже заглушки (docs/espeech.md, «Поставка»)
FROM ${UBUNTU} AS espeech-stubs
RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*
COPY packaging/linux/ /src/
RUN sh /src/build-stubs.sh /src/stubs /out/stubs

# --- Общая основа: рантайм-библиотеки сервера ---
FROM ${UBUNTU} AS base

# GGML_CUDA_DISABLE_GRAPHS: с CUDA Graphs cosyvoice.cpp молча считает неверно
# (тихий шумный звук, мусор вместо речи; ср. Lourdle/cosyvoice.cpp#15). На CPU ни на что не влияет.
# engines/cosyvoice/launch.py задаёт его серверу и сам, здесь — для запуска cosyvoice-cli вручную.
ENV DEBIAN_FRONTEND=noninteractive \
    GGML_CUDA_DISABLE_GRAPHS=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libicu78 libgomp1 \
        libavcodec62 libavformat62 libavutil60 libswresample6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=fetch /opt/cosyvoice /opt/cosyvoice

# --- stream-tts со встроенным сервером ---
FROM base AS stream-tts

# Python из apt (в 26.04 — 3.14) ставит пакеты только в venv (PEP 668).
# ffmpeg: mp3 для примеров голосов и чтение звука в эффектах (post).
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv

WORKDIR /app
COPY stream-tts/requirements.txt .
# --only-binary: без компилятора в образе сборка из исходников всё равно не удалась бы, пусть падает сразу
RUN /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: -r requirements.txt
# ESpeech (вторая модель): в образе только код espeech-server и заглушки (сотни КБ), Python 3.14 с torch и модель
# stream-tts качает по кнопке на вкладке «Загрузки» в тома ESPEECH_RUNTIME и /models (engines/espeech/download.py);
# /opt/cosyvoice — это paths.RUNTIME, код — в его espeech/, как у программы для Windows
COPY --from=espeech-stubs /out/stubs /opt/cosyvoice/espeech/stubs
COPY espeech/*.py espeech/THIRD-PARTY.txt /opt/cosyvoice/espeech/espeech/
# Supertonic 3 (третья модель): supertonic-server запускает Python образа (зависимости — numpy и onnxruntime из
# requirements.txt), нормализатор русского — из кода espeech выше; модель stream-tts качает по кнопке в том /models
# (engines/supertonic/launch.py и download.py)
COPY supertonic/*.py supertonic/THIRD-PARTY.txt /opt/cosyvoice/supertonic/supertonic/
COPY stream-tts/streamtts ./streamtts

# LD_LIBRARY_PATH не задаётся: в /opt/cosyvoice свой libonnxruntime.so (1.30), он перебил бы onnxruntime из venv;
# серверу путь добавляет engines/cosyvoice/launch.py. Сервер запускает stream-tts; COSYVOICE_HOST=0.0.0.0 открывает
# его для порта 8080 контейнера (WebUI и инструменты), stream-tts ходит к нему по 127.0.0.1. ESPEECH_RUNTIME —
# внутри тома (/espeech-runtime): рантайм собирается рядом, в python.tmp, и переименовывается. STREAM_TTS_CUDA — том
# для CUDA: её ggml-cuda сервер подключает через GGML_BACKEND_PATH, cuBLAS из неё берёт и torch ESpeech.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    STREAM_TTS_DATA=/data \
    STREAM_TTS_PROJECT=/project \
    STREAM_TTS_RUNTIME=/opt/cosyvoice \
    STREAM_TTS_MODELS=/models \
    STREAM_TTS_VOICES=/voices \
    STREAM_TTS_LISTEN_HOST=0.0.0.0 \
    STREAM_TTS_LISTEN_PORT=8090 \
    COSYVOICE_HOST=0.0.0.0 \
    COSYVOICE_PORT=8080 \
    ESPEECH_RUNTIME=/espeech-runtime/python \
    STREAM_TTS_CUDA=/cuda
EXPOSE 8090 8080
CMD ["python", "-m", "streamtts"]
