# AGENTS.md

Заметки для ИИ-агентов: только то, что нужно, чтобы менять код и данные и ничего не сломать. Дерево файлов с
назначением модулей, образ, обновление версий — [docs/development.md](docs/development.md); README — только для
пользователя.

## Проект

Локальный синтез речи с клонированием голоса: [cosyvoice.cpp](https://github.com/Lourdle/cosyvoice.cpp)
(CosyVoice3 0.5B, GGUF) и всё, что вокруг него собрано. Хост: Windows 10, Docker Desktop (WSL2), Ryzen 5 5600G,
RTX 3060 Ti 8 ГБ; сервер обычно запущен на GPU.

| Путь | Что | В git |
|---|---|---|
| `setup.sh`, `docker-compose*.yml`, `cosyvoice/` | cosyvoice-server на :8080; образ из готовых релизов cosyvoice.cpp и llama.cpp (код сервера не наш) | да |
| `stream-tts/streamtts/` | stream-tts на :8090: озвучка наград Twitch, админка, оверлей OBS (спецификация [docs/stream-tts.md](docs/stream-tts.md), развёртывание `stream-tts-deploy.md`, Windows `stream-tts-windows.md`) | да |
| `packaging/windows/` | программа (Nuitka) и установщик (Inno Setup 6.5+) для Windows; CI — `.github/workflows/windows.yml` | да, кроме `.venv`, `.inno`, `build/`, `dist/` |
| `scripts/` | `download-model.sh`; `import-voices.sh` — голоса из `voice-refs/<набор>/<голос>.wav + .txt` в `voices/*.gguf` | да |
| `tools/voices/`, `tools/wc3/`, `tools/sources/` | подбор образцов: общий (`pick.py`, `synth.py`, оценки), реплики WC3, прочие источники ([docs/voice-pipeline.md](docs/voice-pipeline.md), [docs/voices-wc3.md](docs/voices-wc3.md)) | да |
| `effects/` | эффекты поверх синтеза (`post`) | да |
| `voice-clips/<набор>/` | варианты образцов, их синтез, оценки и выбор | нет (тексты реплик из игр) |
| `voice-refs/<набор>/` | итоговые образцы и `manifest.json` | нет |
| `voices/`, `models/`, `voice-origs/`, `samples/`, `stream-tts-data/`, `runtime/`, `.env` | данные, секреты, нативный cosyvoice-server для Windows | нет |

Наборы голосов: `wc3` (`wc3-*`), `games`, `streamers`; каталог — [docs/voices.md](docs/voices.md).

## Связи, которые легко порвать

- Скрипты `tools/` считают корень как `ROOT = dirname(dirname(HERE))` и грузят соседние модули по пути
  (`spec_from_file_location`): перед переносом или переименованием — `grep spec_from_file_location`.
- Имя набора — имя папки и в `voice-clips/`, и в `voice-refs/`; по нему stream-tts группирует страницу голосов
  (`GROUPS` в `streamtts/voices.py`).
- `post` в `alts.json` и `manifest.json` — `[скрипт, аргументы…]`, путь от корня проекта. stream-tts ищет скрипт в
  `STREAM_TTS_PROJECT` (в контейнере `/project`), грузит через importlib и вызывает `apply(звук, аргументы)`;
  командная строка эффекта нужна `synth.py`.
- Порты: cosyvoice 8080, stream-tts 8090, `review.py` 8765, `pick.py` 8766, `emotion-eval.py` 8767; из исходников
  `stream-tts-managed` — 8092 (его cosyvoice-server — на свободном порту), `stream-tts-en` (английский интерфейс без
  модели) — 8093. Конфигурации — `.claude/launch.json`.

## Запуск

- Стек: `docker compose up -d --build` (`.env` уже настроен на модель и GPU); после правок — `--build stream-tts`
  или `--build cosyvoice`. Сервисы могут быть в работе (стрим): перезапуск контейнеров согласовывать с пользователем.
- Инструменты — из корня проекта: `python tools/wc3/review.py b02`. `review.py` и `pick.py` — только стандартная
  библиотека; остальным (numpy, onnxruntime, faster-whisper, torch cu124 и др.) нужен `.venv/Scripts/python.exe`
  (3.11), cuBLAS/cuDNN для faster-whisper — из `torch/lib`. Везде нужны `ffmpeg`/`ffprobe` в PATH; звук в
  пайплайне — 24 кГц моно.
- stream-tts без Docker: `stream-tts/.venv` (3.14), конфигурация `stream-tts` (`COSYVOICE_URL=http://localhost:8080`,
  данные из `stream-tts-data/`).
- Shell-скрипты — bash (Git Bash), из PowerShell не запускаются.
- Программа для Windows: `packaging/windows/.venv/Scripts/python.exe packaging/windows/build-exe.py --runtime core`
  (Nuitka + MSVC, первая сборка ~6 мин), установщик — `--setup`, рантайм — `build-runtime.py` (`--cuda` — для
  `--runtime all`). Установленная программа держит `data\`, `models\`, `voices\` в своей папке (признак — файл
  `portable`). Собранный exe проверять на другом порту и с отдельными данными (`STREAM_TTS_PORT`,
  `STREAM_TTS_DATA`), чтобы не задеть Docker-версию на 8090.

## Подводные камни

- **К cosyvoice-server — строго один запрос за раз**: параллельная регистрация голосов роняет его (`double free`).
  Не запускать `import-voices.sh` вместе с `synth.py` (его запускает и кнопка в `pick.py`) или синтезом stream-tts.
  stream-tts раз в минуту перечитывает голоса и синтезирует примеры новых, то есть может вклиниться в
  `import-voices.sh`.
- Сервер помнит `chunk_tokens` из прошлого запроса, поэтому stream-tts передаёт его в каждом.
- Повторная регистрация имени даёт 409: сначала `DELETE /speaker/<имя>` (так делает `import-voices.sh --force`).
  `POST /model/load` ждёт поле `model`; после выгрузки модели голоса сбрасываются.
- Не убирать `GGML_CUDA_DISABLE_GRAPHS=1` (образ) и `--llm-flash-attn 0` (`docker-compose.gpu.yml`): без них на GPU
  тихий шум или мусор вместо речи, причём без ошибок в логах.
- Замеры скорости — по `elapsed_ms` из `docker compose logs cosyvoice` или через `127.0.0.1`: `localhost` из Python
  на Windows добавляет ~2 с (сначала пробует IPv6).
- stream-tts без `COSYVOICE_URL` при наличии `runtime/cosyvoice-server.exe` запускает свой сервер (режим managed) —
  второй на той же видеокарте (1,5–1,9 ГБ видеопамяти на Q8_0).
- Nuitka: собранную программу `paths.py` узнаёт по `__compiled__` (`sys.frozen` не задаётся); данные пакета и
  `effects/` лежат рядом с exe. Импорты эффектов Nuitka не видит: новые зависимости — в `--include-module` в
  `build-exe.py`.
- CUDA в установщик не входит: установщик и вкладка «Сервер» качают два архива из релиза llama.cpp той же сборки,
  что ядро GGML (`cuda.archives` в `downloads.json`; `ggml-cuda.dll` должен совпадать с `ggml-base.dll`). Новая
  сборка llama.cpp — `LLAMA`/`ASSETS` в `build-runtime.py` и `cuda` в `downloads.json` вместе. Строка в `[Code]`
  `StreamTTS.iss` не должна начинаться с `[` (Inno примет её за заголовок секции).
- Версия программы — только `__version__` в `streamtts/__init__.py`. Релиз — аннотированный тег `v<версия>`: CI
  собирает установщик и публикует релиз с `latest.json`, по которому программа ищет обновления (`REPO` в
  `updates.py` должен совпадать с репозиторием, иначе CI падает на шаге «Версия»).
- В `voice-clips/` ручная работа пользователя (`ratings.txt`, `text-fixes.json`, `phrase-tags.json`, `picks.json`,
  `alts.json`), в git её нет: без нужды не перезаписывать, перед массовой правкой — копия в scratchpad.
- Переименование голоса: зарегистрировать новое имя, старое выгрузить (`DELETE /speaker/<имя>`) и удалить
  `voices/<имя>.gguf`, иначе вернётся после перезапуска. Настройки и выбор зрителей (`users.voice`) в stream-tts
  остаются на старом имени — такие зрители получат голос по хешу ника.
- Имена в русской локализации WC3 2002 года звучат по-старому (Артес, Тиренда, Малганус, Кел-Тузед, Мэв, Фарион,
  Маннорох) — и в расшифровках, и в тексте для синтеза.
- Модель эмоций (`emotions.py`) — CC BY-NC-SA 4.0: только для разметки образцов, не для синтеза.
- Голоса реальных людей (***, ***, ***) — только для шуток на стриме. Предпочтительны оригинальные
  записи; примеры JeetBot — запасной вариант, это уже синтез.
- `voice-origs/` (ассеты игр, записи с YouTube) не попадает ни в git, ни в образы.

## Соглашения

- Язык проекта — русский: документация, комментарии, интерфейс, коммиты, ответы пользователю.
- Английский интерфейс stream-tts (`i18n.py`): строки для пользователя пишутся по-русски через `tr("…")` в Python и
  `T("…")` в `admin.js`, разметка переводится сама (абзац со ссылкой или `<code>` — с `data-t`), перевод — в
  `static/en.json`. Проверка: `python stream-tts/check-i18n.py` и `[...I18N_MISSING]` в консоли админки на
  `stream-tts-en`. Логи (`log.*`) не переводятся.
- Коммит: «область: что сделано» (`stream-tts: короткие фразы целиком…`), в теле — зачем и что проверено.
- Инструменты — самостоятельные скрипты с docstring в начале (назначение, `usage:`, входы и выходы). При изменении
  поведения обновлять docstring и документ в `docs/`.
- Комментарии короткие и объясняют «почему»: замеры, найденные баги, причины выбора значений.
- Shell-скрипты, `Dockerfile` и `*.yml` — с LF (`.gitattributes`); скрипты сами делают `cd` в корень проекта.
- Новые и перенесённые файлы — в дерево [docs/development.md](docs/development.md); новая папка — ещё и в таблицу выше.

## Проверка

Автотестов нет. Минимум после правок:
- Python: `python -m py_compile <файлы>`; страницы инструментов и stream-tts — через preview (`.claude/launch.json`)
  с проверкой консоли и запросов.
- Compose: `docker compose config -q`; образ — `docker compose build <сервис>`.
- Голоса и эффекты — тестовая фраза через `POST /tts`; оценку на слух делает пользователь.
