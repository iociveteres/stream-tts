#!/usr/bin/env bash
# Использование: ./download-model.sh [КВАНТ] [--frontend int8|full|none] [--list] [--force]
#   КВАНТ       по умолчанию Q8_0 (рекомендация автора)
#   --frontend  ONNX-модели для клонирования голоса: int8 (~250 МБ, по умолчанию),
#               full (~1 ГБ), none
set -euo pipefail

REPO="Lourdle/Fun-CosyVoice3-0.5B-2512-GGUF"
BASE="https://huggingface.co/${REPO}/resolve/main"
DEST_DIR="${MODELS_DIR:-./models}"
FORCE=0
QUANT="Q8_0"
FRONTEND="int8"

QUANTS="F32:3.4G F16:1.7G Q8_0:944M Q6_K:840M Q5_1:684M Q5_K_M:683M Q5_K_S:659M
Q5_K_XXS:657M Q5_0:632M Q4_K_M:629M Q4_K_S:602M Q4_1:580M Q4_K_XXS:578M Q4_0:527M
Q3_K_M:523M Q3_K_S:495M Q3_K_XXS:470M Q2_K_L:463M Q2_K:447M"

while [ $# -gt 0 ]; do
    case "$1" in
        --list)
            for q in $QUANTS; do printf "%-10s %s\n" "${q%%:*}" "${q#*:}"; done
            exit 0 ;;
        --force) FORCE=1 ;;
        --frontend) FRONTEND="${2:?--frontend: int8|full|none}"; shift ;;
        -*) echo "Неизвестный флаг: $1" >&2; exit 1 ;;
        *) QUANT="$1" ;;
    esac
    shift
done

case " $(echo $QUANTS) " in
    *" ${QUANT}:"*) ;;
    *) echo "Неизвестный квант: $QUANT (см. ./download-model.sh --list)" >&2; exit 1 ;;
esac

# fetch <путь в репо> <локальный файл>: качает в .part с докачкой, переименовывает по успеху
fetch() {
    local dst="$2"
    if [ -f "$dst" ] && [ "$FORCE" -ne 1 ]; then
        echo "Уже есть: $dst"
        return
    fi
    mkdir -p "$(dirname "$dst")"
    echo "Качаю $1 ..."
    curl -fL -C - --retry 5 --retry-delay 3 --progress-bar -o "${dst}.part" "${BASE}/$1"
    mv -f "${dst}.part" "$dst"
}

fetch "CosyVoice3-2512_${QUANT}.gguf" "${DEST_DIR}/CosyVoice3-2512_${QUANT}.gguf"

case "$FRONTEND" in
    int8) sfx=".int8" ;;
    full) sfx="" ;;
    none) exit 0 ;;
    *) echo "--frontend: int8|full|none" >&2; exit 1 ;;
esac
for f in campplus speech_tokenizer_v3; do
    fetch "frontend-onnx/${f}${sfx}.onnx" "${DEST_DIR}/frontend-onnx/${f}${sfx}.onnx"
done
