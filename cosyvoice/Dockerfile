# syntax=docker/dockerfile:1
# cosyvoice-server (FFmpeg, WebUI) из готовых релизов; GPU=cuda добавляет CUDA-бэкенд.
# Базовый образ 26.04: бинарники собраны в CI на ubuntu-26.04 (glibc 2.43, ICU 78).

ARG UBUNTU=ubuntu:26.04

# --- Стадия загрузки: curl нужен только здесь, в итоговый образ не попадает ---
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

# --- Итоговый образ: только рантайм-библиотеки ---
FROM ${UBUNTU}

# GGML_CUDA_DISABLE_GRAPHS: с CUDA Graphs cosyvoice.cpp молча считает неверно
# (тихий шумный звук, мусор вместо речи; ср. Lourdle/cosyvoice.cpp#15). На CPU ни на что не влияет.
ENV DEBIAN_FRONTEND=noninteractive \
    LD_LIBRARY_PATH=/opt/cosyvoice \
    GGML_CUDA_DISABLE_GRAPHS=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        libicu78 libgomp1 \
        libavcodec62 libavformat62 libavutil60 libswresample6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=fetch /opt/cosyvoice /opt/cosyvoice
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
# sed: страховка от CRLF, если файл сохранили на Windows
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

WORKDIR /opt/cosyvoice
EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--webui"]
