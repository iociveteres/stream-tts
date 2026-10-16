#!/usr/bin/env bash
# Единая точка входа: ./setup.sh [КВАНТ] [--frontend int8|full|none] [--gpu]
# Работает из Linux, WSL2 и Git Bash на Windows.
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
bash ./download-model.sh "$QUANT" "${dl_args[@]}"
mkdir -p voices

# .env читается compose автоматически: последующие 'docker compose up' возьмут ту же модель
# (а с --gpu и тот же набор compose-файлов)
printf 'MODEL=/models/CosyVoice3-2512_%s.gguf\nPORT=%s\n' "$QUANT" "$PORT" > .env
if [ "$GPU" -eq 1 ]; then
    printf 'COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml\nCOMPOSE_PATH_SEPARATOR=:\n' >> .env
fi

echo "==> Собираю и запускаю контейнер"
docker compose up --build -d

echo "==> Жду сервер"
for _ in $(seq 1 90); do
    if curl -fs -o /dev/null "http://localhost:${PORT}/ping"; then
        echo "Готово. WebUI: http://localhost:${PORT}"
        echo "Логи: docker compose logs -f | Стоп: docker compose down"
        exit 0
    fi
    sleep 2
done
echo "Сервер не ответил за 3 минуты. Логи:" >&2
docker compose logs --tail 50 >&2
exit 1
