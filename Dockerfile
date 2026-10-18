# syntax=docker/dockerfile:1
# Два образа из готовых релизов cosyvoice.cpp и llama.cpp (цель выбирает docker-compose.yml):
# - stream-tts (по умолчанию): озвучка наград Twitch со встроенным cosyvoice-server (режим managed, runtime.py);
# - cosyvoice: только cosyvoice-server (профиль server-only), аргументы собирает entrypoint.sh.
# GPU=cuda добавляет CUDA-бэкенд. Обе цели на ubuntu 26.04: бинарники собраны в CI на ubuntu-26.04
# (glibc 2.43, ICU 78), а в python:*-slim glibc 2.41.

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

# --- Общая основа: рантайм-библиотеки сервера ---
FROM ${UBUNTU} AS base

# GGML_CUDA_DISABLE_GRAPHS: с CUDA Graphs cosyvoice.cpp молча считает неверно
# (тихий шумный звук, мусор вместо речи; ср. Lourdle/cosyvoice.cpp#15). На CPU ни на что не влияет.
# runtime.py задаёт его серверу и сам, здесь — для запуска сервера и cosyvoice-cli вручную.
ENV DEBIAN_FRONTEND=noninteractive \
    GGML_CUDA_DISABLE_GRAPHS=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libicu78 libgomp1 \
        libavcodec62 libavformat62 libavutil60 libswresample6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=fetch /opt/cosyvoice /opt/cosyvoice

# --- Только сервер (профиль server-only) ---
FROM base AS cosyvoice

ENV LD_LIBRARY_PATH=/opt/cosyvoice
COPY cosyvoice/entrypoint.sh /usr/local/bin/entrypoint.sh
# sed: страховка от CRLF, если файл сохранили на Windows
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /opt/cosyvoice
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--webui"]

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
COPY stream-tts/streamtts ./streamtts

# LD_LIBRARY_PATH не задаётся: в /opt/cosyvoice свой libonnxruntime.so (1.30), он перебил бы onnxruntime из venv;
# серверу путь добавляет runtime.py. COSYVOICE_URL не задан — режим managed; COSYVOICE_HOST=0.0.0.0 открывает
# сервер для порта 8080 контейнера (WebUI и инструменты), stream-tts ходит к нему по 127.0.0.1.
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
    COSYVOICE_PORT=8080
EXPOSE 8090 8080
CMD ["python", "-m", "streamtts"]
