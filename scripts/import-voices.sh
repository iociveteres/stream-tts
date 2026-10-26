#!/usr/bin/env bash
# Регистрирует голоса из папки с образцами и сохраняет их в ./voices/<имя>.gguf.
# Использование: scripts/import-voices.sh <папка> [--force] [имя ...]
#   В папке лежат пары <имя>.wav + <имя>.txt (точная расшифровка образца).
#   Уже сохранённые голоса пропускаются, если не указан --force.
#   Имена после папки — только эти голоса (их печатает tools/voices/pick.py --install).
set -euo pipefail
cd "$(dirname "$0")/.."  # пути ниже — от корня проекта

DIR="${1:?Укажите папку с образцами, например voice-refs/cosyvoice/wc3}"
shift
FORCE=0
if [ "${1:-}" = "--force" ]; then FORCE=1; shift; fi
ONLY=" $* "
URL="http://localhost:${PORT:-8080}"

curl -fs -o /dev/null "$URL/ping" || { echo "Сервер $URL не отвечает (docker compose up -d)" >&2; exit 1; }

# ONNX-фронтенд сервер держит выгруженным (stream-tts грузит его, когда создаёт голос): грузим по путям из его флагов
load_frontend() {
    curl -fs "$URL/status" | grep -q '"frontend_available":true' && return
    curl -fsS -X POST "$URL/frontend/model/load" -H 'Content-Type: application/json' \
        -d "$(curl -fsS "$URL/frontend/model")" >/dev/null \
        || { echo "ONNX-фронтенд не загружен: нет models/frontend-onnx (scripts/download-model.sh --frontend int8)" >&2; exit 1; }
}

# python3 на Linux, python на Windows (там python3 бывает заглушкой Microsoft Store, поэтому проверяем запуск)
PY=""
for p in python3 python; do
    if "$p" -c '' >/dev/null 2>&1; then PY="$p"; break; fi
done
[ -n "$PY" ] || { echo "Не найден Python (python3 или python)" >&2; exit 1; }

json_escape() { "$PY" -c 'import json,sys; print(json.dumps(sys.stdin.read().strip(), ensure_ascii=False))'; }

ok=0; skip=0; fail=0
for wav in "$DIR"/*.wav; do
    name="$(basename "$wav" .wav)"
    [ "$ONLY" = "  " ] || [[ "$ONLY" == *" $name "* ]] || continue
    txt="$DIR/$name.txt"
    [ -f "$txt" ] || { echo "Нет расшифровки: $txt" >&2; fail=$((fail+1)); continue; }
    if [ -f "voices/$name.gguf" ] && [ "$FORCE" -ne 1 ]; then
        skip=$((skip+1)); continue
    fi
    echo "==> $name"
    load_frontend   # stream-tts выгружает его после простоя
    # сервер не регистрирует имя повторно (409): сначала выгружаем голос из памяти
    [ "$FORCE" -eq 1 ] && curl -fs -o /dev/null -X DELETE "$URL/speaker/$name" || true
    if ! curl -fsS -X POST "$URL/speaker?type=extract" \
            -F "name=$name" -F "text=$(cat "$txt")" -F "audio=@$wav" >/dev/null; then
        fail=$((fail+1)); continue
    fi
    body="{\"name\":$(printf '%s' "$name" | json_escape),\"path\":$(printf '/voices/%s.gguf' "$name" | json_escape)}"
    if curl -fsS -X POST "$URL/speaker/save" -H 'Content-Type: application/json' -d "$body" >/dev/null; then
        ok=$((ok+1))
    else
        fail=$((fail+1))
    fi
done
echo "Готово: добавлено $ok, пропущено $skip, ошибок $fail"
[ "$fail" -eq 0 ]
