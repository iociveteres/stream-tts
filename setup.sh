#!/usr/bin/env bash
# Единая точка входа: ./setup.sh [КВАНТ] [--frontend int8|full|none] [--gpu]
# Скачивает модель (её можно скачать и в админке stream-tts), пишет в .env порт и набор compose-файлов, собирает и
# запускает stream-tts со встроенным cosyvoice-server. Работает из Linux, WSL2 и Git Bash на Windows.
set -euo pipefail
cd "$(dirname "$0")"

QUANT="Q8_0"
GPU=0
dl_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --frontend) dl_args+=("$1" "${2:?}"); shift ;;
        --gpu) GPU=1 ;;
        *) QUANT="$1" ;;
    esac
    shift
done
PORT="${PORT:-8080}"

echo "==> Проверяю Docker"
command -v docker >/dev/null 2>&1 || { echo "Docker не найден." >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "Не найден 'docker compose' (нужен Compose v2)." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker daemon не запущен (запустите Docker Desktop)." >&2; exit 1; }
if [ "$GPU" -eq 1 ]; then
    docker run --rm --gpus all ubuntu:26.04 true >/dev/null 2>&1 \
        || { echo "Docker не видит GPU (нужны драйвер NVIDIA и NVIDIA Container Toolkit / Docker Desktop с WSL2)." >&2; exit 1; }
fi

echo "==> Модель"
bash scripts/download-model.sh "$QUANT" "${dl_args[@]}"
mkdir -p voices

# .env читается compose автоматически: последующие 'docker compose up' возьмут тот же порт (а с --gpu и тот же
# набор compose-файлов). Остальные строки (пароль, Client ID, MODEL для профиля server-only) не трогаются.
# Модель в .env не пишется: stream-tts берёт выбранную в админке, а если в models/ она одна — её.
touch .env
sed -i -E '/^(PORT|COMPOSE_FILE|COMPOSE_PATH_SEPARATOR)=/d' .env
printf 'PORT=%s\n' "$PORT" >> .env
if [ "$GPU" -eq 1 ]; then
    printf 'COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml\nCOMPOSE_PATH_SEPARATOR=:\n' >> .env
fi

echo "==> Собираю и запускаю контейнер"
# Отдельный cosyvoice (профиль server-only или старая схема) занял бы порт сервера и видеокарту
docker compose rm -sf cosyvoice >/dev/null 2>&1 || true
docker compose up --build -d

echo "==> Жду сервер"
UI_PORT="$(sed -n 's/^STREAM_TTS_PORT=//p' .env | tail -n 1)"
UI_PORT="${STREAM_TTS_PORT:-${UI_PORT:-8090}}"
for _ in $(seq 1 90); do
    if curl -fs -o /dev/null "http://127.0.0.1:${PORT}/ping"; then
        echo "Готово. Админка: http://localhost:${UI_PORT} | WebUI сервера: http://localhost:${PORT}"
        echo "Логи: docker compose logs -f | Стоп: docker compose down"
        # CUDA в образ не входит: её (~590 МБ) качает админка в ./cuda, пока синтез идёт на процессоре
        if [ "$GPU" -eq 1 ] && [ ! -f cuda/libggml-cuda.so ]; then
            echo "Видеокарта: на вкладке «Сервер» в блоке «Устройство синтеза» скачайте CUDA (~590 МБ)."
        fi
        exit 0
    fi
    sleep 2
done
echo "Сервер не ответил за 3 минуты. Если в models/ несколько моделей, выберите одну в админке" >&2
echo "(http://localhost:${UI_PORT}, вкладка «Сервер»). Логи:" >&2
docker compose logs --tail 50 >&2
exit 1
