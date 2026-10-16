# AGENTS.md

Заметки для ИИ-агентов, которые работают в этом репозитории. Пользовательская документация — в
[README.md](README.md) и [docs/](docs/); здесь только то, что нужно, чтобы менять код и данные и ничего не сломать.

## Что это за проект

Локальный синтез речи с клонированием голоса: [cosyvoice.cpp](https://github.com/Lourdle/cosyvoice.cpp)
(CosyVoice3 0.5B, GGUF) в Docker, плюс всё, что вокруг него собрано:

1. **cosyvoice-server** (`cosyvoice/`, `docker-compose*.yml`, `setup.sh`) — сервер синтеза на :8080. Код сервера не
   наш: образ собирается из готовых релизов cosyvoice.cpp и llama.cpp (GGML).
2. **Голоса** (`voices/*.gguf`) — собираются из образцов `voice-refs/<набор>/<голос>.wav + .txt` скриптом
   `scripts/import-voices.sh`. Два набора: Warcraft III (`wc3-*`) и мемные голоса для стрима.
3. **Инструменты подбора образцов** (`tools/wc3/`, `tools/meme/`) — нарезка кандидатов, расшифровка, A/B синтеза,
   локальные страницы для отслушивания. Подробно: [docs/voices-wc3.md](docs/voices-wc3.md),
   [docs/voices-meme.md](docs/voices-meme.md).
4. **stream-tts** (`stream-tts/`) — второй сервис на :8090: озвучка наград Twitch голосами cosyvoice, админка и
   оверлей для OBS. Спецификация: [docs/stream-tts.md](docs/stream-tts.md).

Хост: Windows 10, Docker Desktop (WSL2), Ryzen 5 5600G, RTX 3060 Ti 8 ГБ. Сервер обычно запущен на GPU.

## Где что лежит

| Путь | Что | В git |
|---|---|---|
| `setup.sh`, `docker-compose.yml`, `docker-compose.gpu.yml` | запуск всего стека | да |
| `cosyvoice/` | `Dockerfile` и `entrypoint.sh` образа cosyvoice-server | да |
| `stream-tts/streamtts/` | пакет stream-tts (aiohttp); `static/` — админка, оверлей, страница голосов | да |
| `scripts/` | `download-model.sh`, `import-voices.sh` | да |
| `tools/wc3/` | `cut-clips.py` → `review.py` → `synth-ab.py` → `pick.py`, плюс `extract-subtitles.py` | да |
| `tools/meme/` | `fetch-wiki-lines.py`, `extract-stalker.py`, `transcribe-lines.py` → `score-speaker.py` → `build-ref.py`; `meme-pick.py`; `villager-ref.py` | да |
| `effects/` | эффекты поверх синтеза (`post`), сейчас `villager-fx.py` | да |
| `voice-clips/` | кандидаты, оценки, выбор, тексты реплик | только `.txt`/`.json` |
| `voice-refs/` | итоговые образцы и `manifest.json` наборов | нет |
| `voices/`, `models/`, `voice-origs/`, `samples/`, `stream-tts-data/`, `.env` | данные и секреты | нет |

Связи между файлами, которые легко порвать при переносе:
- Скрипты в `tools/` считают корень проекта как `ROOT = dirname(dirname(HERE))` и берут данные от него, а соседние
  модули с дефисом в имени грузят через `importlib` по пути: `review.py` и `synth-ab.py` → `cut-clips.py`,
  `pick.py` → `review.py`, `tools/meme/meme-pick.py` → `tools/wc3/review.py`,
  `tools/meme/score-speaker.py` → `tools/wc3/synth-ab.py`.
- `post` в `voice-clips/meme/alts.json` и `voice-refs/*/manifest.json` — это `[скрипт, аргументы…]`, путь к скрипту
  от корня проекта (`effects/villager-fx.py`). stream-tts ищет его в `STREAM_TTS_PROJECT` (в контейнере `/project`,
  туда смонтированы `voice-refs/` и `effects/`).
- Порты: cosyvoice 8080, stream-tts 8090, `review.py` 8765, `pick.py` 8766, `meme-pick.py` 8767
  (конфигурации в `.claude/launch.json`).

## Как запускать

- Стек: `docker compose up -d --build` (в `.env` уже прописаны модель и GPU-вариант compose). После правок
  `stream-tts/` — `docker compose up -d --build stream-tts`, после правок `cosyvoice/` — `--build cosyvoice`.
  Оба сервиса могут быть в работе (стрим), поэтому перезапуск контейнеров согласовывать с пользователем.
- Инструменты запускаются **из корня проекта**: `python tools/wc3/review.py b02`.
  - `review.py`, `pick.py`, `meme-pick.py` — только стандартная библиотека, подойдёт любой Python 3.
  - Остальному (numpy, onnxruntime, faster-whisper, demucs, yt-dlp, torch cu124) нужен venv проекта:
    `.venv/Scripts/python.exe` (Python 3.11). cuBLAS/cuDNN для faster-whisper берутся из `torch/lib`.
  - Везде нужны `ffmpeg`/`ffprobe` в PATH; звук внутри пайплайна — 24 кГц моно.
- stream-tts без Docker: `stream-tts/.venv` (Python 3.14) и конфигурация `stream-tts` в `.claude/launch.json`
  (ставит `COSYVOICE_URL=http://localhost:8080`, данные берутся из `stream-tts-data/`).
- Shell-скрипты — bash (Git Bash на Windows), из PowerShell не запускаются.

## Подводные камни

- **К cosyvoice-server — строго один запрос за раз.** Параллельная регистрация голосов роняет сервер
  (`double free`). Не запускать `scripts/import-voices.sh` одновременно с `synth-ab.py`, `meme-pick.py --synth` или
  stream-tts, который синтезирует в этот момент.
- Сервер помнит `chunk_tokens` из прошлого запроса, поэтому stream-tts передаёт его в каждом запросе.
- Повторная регистрация того же имени голоса даёт 409: сначала `DELETE /speaker/<имя>` (так делает
  `import-voices.sh --force`). `POST /model/load` ждёт поле `model`, а после выгрузки модели голоса сбрасываются.
- Не убирать `GGML_CUDA_DISABLE_GRAPHS=1` (образ) и `--llm-flash-attn 0` (`docker-compose.gpu.yml`): без них на GPU
  тихий шумный звук или мусор вместо речи, причём без ошибок в логах.
- Замеры скорости — по `elapsed_ms` из `docker compose logs cosyvoice` или через `127.0.0.1`: `localhost` из Python
  на Windows добавляет ~2 с (сначала пробует IPv6).
- В `voice-clips/` лежит ручная работа пользователя: `ratings.txt`, `text-fixes.json`, `phrase-tags.json`,
  `picks.json`, `alts.json`. Не перезаписывать без нужды, перед массовой правкой — резервная копия в scratchpad.
- Имена в русской локализации WC3 2002 года звучат по-старому (Артес, Тиренда, Малганус, Кел-Тузед, Мэв, Фарион,
  Маннорох) — это учитывается и в расшифровках, и в тексте для синтеза.
- Голоса реальных людей (***, ***, ***) — только для шуток на стриме. Предпочтительны оригинальные
  записи; примеры JeetBot — запасной вариант, это уже синтез.
- `voice-origs/` — ассеты игр и записи с YouTube: в git и в образы не попадают.

## Соглашения

- Язык проекта — русский: документация, комментарии, тексты интерфейса, сообщения коммитов, ответы пользователю.
- Коммит: заголовок «область: что сделано» (`stream-tts: короткие фразы целиком…`), в теле — зачем и что
  проверено.
- Инструменты — самостоятельные скрипты с docstring в начале (назначение, `usage:`, где лежат входы и выходы).
  При изменении поведения обновлять docstring и соответствующий документ в `docs/`.
- Комментарии короткие и объясняют «почему»: замеры, найденные баги, причины выбора значений.
- Shell-скрипты, `Dockerfile` и `*.yml` — с LF (`.gitattributes`); скрипты делают `cd` в корень проекта сами.
- При переносе или добавлении файлов обновлять дерево в README («Устройство») и таблицу выше.

## Проверка

Автотестов нет. Минимум после правок:
- Python: `python -m py_compile <файлы>`; страницы инструментов и stream-tts — через `.claude/launch.json`
  (preview), с проверкой консоли и запросов.
- Compose: `docker compose config -q`; образ — `docker compose build <сервис>`.
- Голоса и эффекты — синтез тестовой фразы через `POST /tts` и прослушивание (оценку на слух делает пользователь).
