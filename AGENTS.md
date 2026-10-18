# AGENTS.md

Заметки для ИИ-агентов, которые работают в этом репозитории. Пользовательская документация — в
[README.md](README.md) и [docs/](docs/); здесь только то, что нужно, чтобы менять код и данные и ничего не сломать.

## Что это за проект

Локальный синтез речи с клонированием голоса: [cosyvoice.cpp](https://github.com/Lourdle/cosyvoice.cpp)
(CosyVoice3 0.5B, GGUF) в Docker, плюс всё, что вокруг него собрано:

1. **cosyvoice-server** (`cosyvoice/`, `docker-compose*.yml`, `setup.sh`) — сервер синтеза на :8080. Код сервера не
   наш: образ собирается из готовых релизов cosyvoice.cpp и llama.cpp (GGML).
2. **Голоса** (`voices/*.gguf`) — собираются из образцов `voice-refs/<набор>/<голос>.wav + .txt` скриптом
   `scripts/import-voices.sh`. Три набора: `wc3` (Warcraft III, `wc3-*`), `games` (другие игры), `streamers`.
   Каталог: [docs/voices.md](docs/voices.md).
3. **Подбор образцов** (`tools/voices/`, `tools/wc3/`, `tools/sources/`) — один процесс для всех наборов: реплики
   из источника → варианты образца → синтез тестовых фраз и оценки (CAM++, Whisper, модель эмоций) → выбор на слух
   на локальной странице → установка. Описание процесса: [docs/voice-pipeline.md](docs/voice-pipeline.md),
   особенности WC3: [docs/voices-wc3.md](docs/voices-wc3.md).
4. **stream-tts** (`stream-tts/`) — второй сервис на :8090: озвучка наград Twitch голосами cosyvoice, админка и
   оверлей для OBS. Спецификация: [docs/stream-tts.md](docs/stream-tts.md).

Хост: Windows 10, Docker Desktop (WSL2), Ryzen 5 5600G, RTX 3060 Ti 8 ГБ. Сервер обычно запущен на GPU.

## Где что лежит

| Путь | Что | В git |
|---|---|---|
| `setup.sh`, `docker-compose.yml`, `docker-compose.gpu.yml` | запуск всего стека | да |
| `cosyvoice/` | `Dockerfile` и `entrypoint.sh` образа cosyvoice-server | да |
| `stream-tts/streamtts/` | пакет stream-tts (aiohttp); `paths.py` — пути по способу запуска, `runtime.py` — встроенный cosyvoice-server (режим managed), `install.py` — загрузка модели, дополнительные пакеты и наборы голосов из админки, `editor.py` — новый голос из записи (черновик, тестовые фразы, сохранение), `separate.py` — очистка образца от музыки (htdemucs в ONNX через onnxruntime, пакет «music»), `bench.py` — проверка скорости устройств, `desktop.py` — точка входа собранной программы, `i18n.py` — язык интерфейса, `static/` — админка, оверлей, страница голосов, `static/en.json` — английский словарь, `static/i18n.js` — перевод страниц, `static/quants/` — примеры звука квантов (их же берёт установщик); `stream-tts/check-i18n.py` — строки без перевода | да |
| `packaging/windows/` | программа и установщик для Windows: `build-runtime.py` (рантайм), `build-exe.py` (Nuitka, `StreamTTS.py`), `StreamTTS.iss` (Inno Setup), `THIRD-PARTY.txt` и `THIRD-PARTY.en.txt` (сторонние лицензии, страница «Информация» установщика); сборочный venv `.venv`, портативный Inno Setup `.inno`, выход `build/`, `dist/`; CI — `.github/workflows/windows.yml` | да, кроме `.venv`, `.inno`, `build/`, `dist/` |
| `scripts/` | `download-model.sh`, `import-voices.sh` | да |
| `tools/voices/` | общий подбор: `pick.py` + `pick.html` (страница, `--add`, `--add-batch`, `--install`), `synth.py` (синтез и оценки), `emotions.py` (модель эмоций), `emotion-eval.py` + `.html` (сверка моделей эмоций с разметкой на слух), `campplus.py` (эмбеддинги) | да |
| `tools/wc3/` | реплики WC3: `cut-clips.py` (кандидаты партиями, `pools.json`), `review.py` (тексты и метки фраз), `extract-subtitles.py` | да |
| `tools/sources/` | прочие источники: `fetch-wiki-lines.py`, `extract-stalker.py`, `transcribe-lines.py` → `score-speaker.py` → `build-ref.py`; `villager-ref.py` | да |
| `effects/` | эффекты поверх синтеза (`post`), сейчас `villager-fx.py` | да |
| `voice-clips/<набор>/` | `set.json`, `alts.json` (варианты), `picks.json` (выбор), `ab/` (синтез, `scores.json`); у WC3 ещё партии `b0N/` | нет (тексты реплик из игр) |
| `voice-refs/` | итоговые образцы и `manifest.json` наборов | нет |
| `voices/`, `models/`, `voice-origs/`, `samples/`, `stream-tts-data/`, `.env` | данные и секреты | нет |
| `runtime/` | нативный cosyvoice-server для Windows: exe, DLL (GGML CPU/CUDA/Vulkan, ICU, FFmpeg), `ffmpeg.exe` | нет |

Связи между файлами, которые легко порвать при переносе:
- Скрипты в `tools/` считают корень проекта как `ROOT = dirname(dirname(HERE))` и берут данные от него, а соседние
  модули грузят через `importlib` по пути: `review.py` → `cut-clips.py`, `tools/voices/pick.py` →
  `tools/wc3/review.py` (http-обработчик), `synth.py` → `pick.py`, `campplus.py`, `emotions.py`;
  `emotion-eval.py` → `pick.py`, `emotions.py`;
  `cut-clips.py` → `emotions.py`, `tools/sources/build-ref.py` → `pick.py`, `score-speaker.py` → `campplus.py`.
- Имя набора — это имя папки и в `voice-clips/`, и в `voice-refs/`; stream-tts группирует по нему страницу голосов
  для зрителей (`GROUPS` в `stream-tts/streamtts/voices.py`).
- `post` в `voice-clips/*/alts.json` и `voice-refs/*/manifest.json` — это `[скрипт, аргументы…]`, путь к скрипту
  от корня проекта (`effects/villager-fx.py`). stream-tts ищет его в `STREAM_TTS_PROJECT` (в контейнере `/project`,
  туда смонтированы `voice-refs/` и `effects/`).
- `post` выполняется в процессе stream-tts: скрипт эффекта грузится через importlib и должен иметь
  `apply(звук, аргументы)`; интерфейс командной строки остаётся для `tools/voices/synth.py`.
- Порты: cosyvoice 8080, stream-tts 8090 (`stream-tts-managed` из исходников — 8092, его cosyvoice-server — на
  свободном порту, `stream-tts-en` — английский интерфейс без модели — 8093), `review.py` 8765, `tools/voices/pick.py` 8766, `tools/voices/emotion-eval.py` 8767
  (конфигурации в `.claude/launch.json`).

## Как запускать

- Стек: `docker compose up -d --build` (в `.env` уже прописаны модель и GPU-вариант compose). После правок
  `stream-tts/` — `docker compose up -d --build stream-tts`, после правок `cosyvoice/` — `--build cosyvoice`.
  Оба сервиса могут быть в работе (стрим), поэтому перезапуск контейнеров согласовывать с пользователем.
- Инструменты запускаются **из корня проекта**: `python tools/wc3/review.py b02`.
  - `review.py` и `tools/voices/pick.py` — только стандартная библиотека, подойдёт любой Python 3.
  - Остальному (numpy, onnxruntime, faster-whisper, demucs, yt-dlp, torch cu124, transformers) нужен venv проекта:
    `.venv/Scripts/python.exe` (Python 3.11). cuBLAS/cuDNN для faster-whisper берутся из `torch/lib`.
  - Везде нужны `ffmpeg`/`ffprobe` в PATH; звук внутри пайплайна — 24 кГц моно.
- stream-tts без Docker: `stream-tts/.venv` (Python 3.14) и конфигурация `stream-tts` в `.claude/launch.json`
  (ставит `COSYVOICE_URL=http://localhost:8080`, данные берутся из `stream-tts-data/`).
- Shell-скрипты — bash (Git Bash на Windows), из PowerShell не запускаются.
- Программа для Windows: `packaging/windows/.venv/Scripts/python.exe packaging/windows/build-exe.py --runtime core`
  (Nuitka + MSVC, первая сборка ~6 мин), установщик — `--setup` (нужен Inno Setup 6). Рантайм —
  `build-runtime.py`. Проверять собранный exe на другом порту и с отдельными данными (`STREAM_TTS_PORT`,
  `STREAM_TTS_DATA`), чтобы не задеть Docker-версию на 8090.

## Подводные камни

- Сборка Nuitka: собранную программу `paths.py` узнаёт по `__compiled__` (Nuitka не задаёт `sys.frozen`), данные
  пакета и `effects/` лежат рядом с exe. Модули, которые грузятся по пути через importlib (эффекты), и их импорты
  Nuitka не видит: новые зависимости эффектов добавлять в `--include-module` в `build-exe.py`.
- stream-tts без `COSYVOICE_URL` при наличии `runtime/cosyvoice-server.exe` запускает свой сервер (режим managed):
  это второй сервер на той же видеокарте (1,5–1,9 ГБ видеопамяти на Q8_0). Конфигурация `stream-tts` в `.claude/launch.json`
  задаёт `COSYVOICE_URL` и работает с Docker как раньше.
- **К cosyvoice-server — строго один запрос за раз.** Параллельная регистрация голосов роняет сервер
  (`double free`). Не запускать `scripts/import-voices.sh` одновременно с `tools/voices/synth.py` (его запускает и
  кнопка на странице `pick.py`) или stream-tts, который синтезирует в этот момент. stream-tts раз в минуту
  перечитывает список голосов и сам синтезирует примеры для новых, то есть может вклиниться посреди
  `import-voices.sh` на несколько голосов (так было при переименовании `jeetbot-*`, сервер выдержал).
- Сервер помнит `chunk_tokens` из прошлого запроса, поэтому stream-tts передаёт его в каждом запросе.
- Повторная регистрация того же имени голоса даёт 409: сначала `DELETE /speaker/<имя>` (так делает
  `import-voices.sh --force`). `POST /model/load` ждёт поле `model`, а после выгрузки модели голоса сбрасываются.
- Не убирать `GGML_CUDA_DISABLE_GRAPHS=1` (образ) и `--llm-flash-attn 0` (`docker-compose.gpu.yml`): без них на GPU
  тихий шумный звук или мусор вместо речи, причём без ошибок в логах.
- Замеры скорости — по `elapsed_ms` из `docker compose logs cosyvoice` или через `127.0.0.1`: `localhost` из Python
  на Windows добавляет ~2 с (сначала пробует IPv6).
- В `voice-clips/` лежит ручная работа пользователя: `ratings.txt`, `text-fixes.json`, `phrase-tags.json`,
  `picks.json`, `alts.json`. В git их нет (там тексты реплик из игр), откатить нечем: не перезаписывать без нужды,
  перед массовой правкой — резервная копия в scratchpad.
- Имена в русской локализации WC3 2002 года звучат по-старому (Артес, Тиренда, Малганус, Кел-Тузед, Мэв, Фарион,
  Маннорох) — это учитывается и в расшифровках, и в тексте для синтеза.
- Переименование голоса: новое имя регистрируется (`import-voices.sh`), старое выгружается
  (`DELETE /speaker/<имя>`) и его `voices/<имя>.gguf` убирается, иначе после перезапуска вернётся. В stream-tts
  у старого имени остаются настройки и выбор зрителей (`users.voice`), такие зрители получат голос по хешу ника.
- Модель эмоций (`emotions.py`) — CC BY-NC-SA 4.0: только для разметки образцов, не для синтеза.
- Голоса реальных людей (***, ***, ***) — только для шуток на стриме. Предпочтительны оригинальные
  записи; примеры JeetBot — запасной вариант, это уже синтез.
- `voice-origs/` — ассеты игр и записи с YouTube: в git и в образы не попадают.

## Соглашения

- Язык проекта — русский: документация, комментарии, тексты интерфейса, сообщения коммитов, ответы пользователю.
- У stream-tts есть английский интерфейс (`i18n.py`): новые строки для пользователя пишутся по-русски через
  `tr("…")` в Python и `T("…")` в `admin.js`, разметка переводится сама (абзац со ссылкой или `<code>` — с `data-t`),
  перевод добавляется в `static/en.json`. Проверка: `python stream-tts/check-i18n.py` и `[...I18N_MISSING]` в
  консоли админки на английском (`stream-tts-en`). Логи (`log.*`) не переводятся.
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
