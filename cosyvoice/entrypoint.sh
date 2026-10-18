#!/usr/bin/env bash
# Собирает аргументы cosyvoice-server из содержимого /models и /voices.
set -euo pipefail

CV_DIR=/opt/cosyvoice

if ! grep -qw avx2 /proc/cpuinfo; then
    echo "[entrypoint] CPU без AVX2: рантайм cosyvoice.cpp на нём не работает" >&2
    exit 1
fi

shopt -s nullglob
args=()

# Модель: $MODEL, иначе единственный *.gguf в /models
model="${MODEL:-}"
if [ -z "$model" ]; then
    ggufs=(/models/*.gguf)
    if [ ${#ggufs[@]} -eq 1 ]; then
        model="${ggufs[0]}"
    elif [ ${#ggufs[@]} -gt 1 ]; then
        echo "[entrypoint] В /models несколько моделей, задайте MODEL; пока без модели (загрузка через WebUI)" >&2
    fi
fi
if [ -n "$model" ]; then
    [ -f "$model" ] || { echo "[entrypoint] Модель не найдена: $model" >&2; exit 1; }
    args+=(--model "$model")
fi

# ONNX-фронтенд (нужен для клонирования голоса из аудио): полные версии приоритетнее int8
first_existing() { for f in "$@"; do if [ -f "$f" ]; then echo "$f"; return; fi; done; }
fe=/models/frontend-onnx
tokenizer="$(first_existing "$fe/speech_tokenizer_v3.onnx" "$fe/speech_tokenizer_v3.int8.onnx")"
campplus="$(first_existing "$fe/campplus.onnx" "$fe/campplus.int8.onnx")"
# В режиме WebUI флаги --speech-tokenizer/--campplus только задают пути (GET /frontend/model), а сам фронтенд
# (~300 МБ) не загружается: его грузят по этим путям те, кому он нужен, — stream-tts при создании голоса,
# scripts/import-voices.sh и tools/voices/synth.py (POST /frontend/model/load)
if [ -n "$tokenizer" ] && [ -n "$campplus" ]; then
    args+=(--speech-tokenizer "$tokenizer" --campplus "$campplus")
else
    echo "[entrypoint] ONNX-фронтенд не найден в $fe: извлечение голоса из аудио недоступно" >&2
fi

# Сохранённые голоса: /voices/<имя>.gguf -> голос <имя>, в подпапках слэши заменяются
# на "_": /voices/wc3/orc/thrall.gguf -> голос wc3_orc_thrall
shopt -s globstar
declare -A voice_src=()
for f in /voices/**/*.gguf; do
    name="${f#/voices/}"
    name="${name%.gguf}"
    name="${name//\//_}"
    if [ -n "${voice_src[$name]:-}" ]; then
        echo "[entrypoint] Голос $name уже взят из ${voice_src[$name]}, пропускаю $f" >&2
        continue
    fi
    voice_src[$name]="$f"
    args+=(--voice-prompt "$name=$f")
done

echo "[entrypoint] cosyvoice-server ${args[*]} $*" >&2
exec "$CV_DIR/cosyvoice-server" "${args[@]}" "$@"
