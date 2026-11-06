# Устройство и разработка

Для тех, кто меняет проект: дерево файлов, как собраны образы, обновление версий, запуск stream-tts
из исходников. Как пользоваться — [README](../README.md); заметки для ИИ-агентов — [AGENTS.md](../AGENTS.md).

## Файлы

```
.
├── setup.sh               # единая точка входа: модель, .env, сборка и запуск
├── Dockerfile             # образы stream-tts (со встроенным сервером) и cosyvoice (только сервер)
├── .dockerignore          # контекст сборки — весь проект, в него пускаются только нужные образам файлы
├── docker-compose.yml     # stream-tts; cosyvoice — в профиле server-only
├── docker-compose.gpu.yml # надстройка для видеокарты NVIDIA
├── README.md              # для пользователя: установка и работа
├── AGENTS.md              # заметки для ИИ-агентов (CLAUDE.md ссылается на него)
├── .gitattributes         # LF для shell-скриптов, Dockerfile и *.yml; .gitignore — данные не в git
├── .claude/launch.json    # конфигурации preview (раздел «Запуск»)
├── LICENSE                # MIT — код проекта (что не входит — в README, «Лицензии»)
├── cosyvoice/
│   └── entrypoint.sh      # профиль server-only: собирает аргументы сервера из /models и /voices
├── espeech/               # espeech-server — синтез ESpeech для stream-tts (docs/espeech.md): __init__.py (папки
│                          # библиотек CUDA до torch), __main__.py (python -m espeech), server.py (HTTP API
│                          # как у cosyvoice-server: обработчик и роуты), engine.py (модель в рабочем потоке),
│                          # wire.py (multipart, WAV), model.py (DiT, сэмплер, Vocos), accent.py (облегчённый
│                          # RUAccent и словарь ударений пользователя), normalize.py (текст чата: конвейер; числа —
│                          # numbers.py и numerals.py, латиница — latin.py), pipeline.py (нарезка на куски),
│                          # lora.py (адаптер голоса), train.py (обучение LoRA на загруженной модели), pairs.py
│                          # (реплики для обучения: звук, текст, проверка), trainjob.py (обучение задачей сервера,
│                          # POST /train), voices.py (формат .espeech), prepare.py (папка модели из файлов Hugging
│                          # Face; её запускает и программа для Windows); requirements.txt — окружение
│                          # espeech/.venv, THIRD-PARTY.txt — лицензии перенесённого кода
├── supertonic/            # supertonic-server — синтез Supertonic 3 на onnxruntime, только CPU (docs/supertonic.md):
│                          # __main__.py (python -m supertonic), server.py (HTTP API как у cosyvoice-server),
│                          # model.py (перенос helper.py авторов: текст, нарезка, четыре ONNX-модели), text.py
│                          # (ударения «+» прочь, русский — нормализатором espeech/normalize.py), voices.py
│                          # (формат .supertonic); THIRD-PARTY.txt — лицензия перенесённого кода; окружение —
│                          # stream-tts/.venv (numpy и onnxruntime)
├── stream-tts/            # озвучка наград Twitch (docs/stream-tts.md), основной сервис docker-compose.yml
│   ├── check-i18n.py      # английский словарь: ключи без перевода и лишние; --missing — строки для en.json
│   ├── ui-edit.py         # режим правки строк интерфейса (в разработке): админка на поддельном сервере, :8099
│   ├── requirements.txt   # окружение stream-tts/.venv и образа
│   └── streamtts/         # пакет; слои — docs/index.md, «Связи» (держит tests/e2e/test_layers.py)
│       ├── __main__.py    # точка входа python -m streamtts: сборка частей, веб-сервер, остановка
│       ├── __init__.py    # __version__ — единственное место версии
│       ├── paths.py       # пути по способу запуска (исходники, Docker, exe); config.py — переменные окружения
│       ├── engine.py      # очередь: приём, итог, синтез, воспроизведение
│       ├── rules.py       # доступ, кулдауны, ответ зрителю об отказе (R_*, REPLIES, ACCESS_WHO)
│       ├── commands.py    # команды чата !tts*, SERVICE_COMMANDS
│       ├── overlay.py     # оверлеи без aiohttp: отправку и проверку «закрыт» передаёт web/
│       ├── filters.py     # фильтры текста: ссылки, мат (profanity.txt — словарь)
│       ├── audio.py       # громкость (LUFS), ограничитель пиков, перекодирование ffmpeg
│       ├── voices.py      # голоса: список с сервера, группы наборов, эффекты post, примеры, страница голосов
│       ├── store.py       # settings.json и SQLite (зрители, баны, история); read_json/write_json
│       ├── stats.py       # статистика активаций и загрузка машины
│       ├── twitch/        # Twitch-приложение: __init__.py — класс Twitch из примесей, режим, запуск и остановка;
│       │                  # auth.py — вход Device Code Flow и токены, helix.py — Helix (награда, статусы
│       │                  # активаций, ответ в чат, HELIX_HINTS), eventsub.py — EventSub WebSocket; common.py —
│       │                  # адреса (STREAM_TTS_TEST_TWITCH), права, ошибки
│       ├── chat_irc.py    # режим «Только чтение чата»: анонимный IRC
│       ├── i18n.py        # язык интерфейса, tr()
│       ├── runtime.py     # встроенный сервер: процесс, порт, перезапуски, paused, выгрузка
│       ├── web/           # HTTP: __init__.py — make_app, пароль, Origin, страницы и сокеты, сводный POSTS;
│       │                  # common.py — ответы, обёртка POST, приём файла сырым телом (upload), _restart_guard;
│       │                  # роуты по модулям, у каждого своя таблица POSTS: state.py (состояние админки),
│       │                  # admin.py (настройки, голоса, очередь, баны, Twitch), model.py (модель, «как
│       │                  # прочитает», ударения), server.py (встроенный сервер, наборы, пакеты, CUDA, ESpeech,
│       │                  # Supertonic 3),
│       │                  # editor.py (/api/draft*), train.py (/api/train*)
│       ├── engines/       # модели синтеза: __init__.py — выбор и смена модели, client.py — общий клиент и
│       │                  # очередь к серверу; cosyvoice/, espeech/ и supertonic/ — описание (__init__.py),
│       │                  # запуск (launch.py), клиент (client.py), особенности редактора голоса (editor.py;
│       │                  # у supertonic/ — отказ: голоса из записи у Supertonic 3 нет);
│       │                  # espeech/train.py — обучение LoRA голоса: набор реплик и запросы /train;
│       │                  # espeech/download.py — загрузка рантайма и модели ESpeech по кнопке;
│       │                  # supertonic/download.py — загрузка Supertonic 3 и его 10 готовых голосов
│       ├── install.py     # Installer: задачи загрузок из админки и их состояние
│       ├── downloads.py   # downloads.json: CUDA, кванты, пакеты (PACKAGES), примеры (SAMPLES)
│       ├── downloads.json # закреплённые загрузки: CUDA из llama.cpp, кванты модели, рантайм и модель ESpeech,
│       │                  # модель Supertonic 3
│       ├── fetch.py       # файл по HTTP с докачкой, повторами и sha256
│       ├── partzip.py     # часть zip по HTTP диапазонами
│       ├── voicesets.py   # наборы голосов из zip: проверка, установка, удаление по модели
│       ├── editor.py      # редактор: новый голос из записи
│       ├── separate.py    # очистка образца от музыки: htdemucs в ONNX
│       ├── bench.py       # скорость устройств
│       ├── desktop.py     # точка входа программы для Windows; updates.py — проверка новой версии
│       └── static/        # admin.html, admin.css, common.css; admin.js — общее админки, вкладки —
│                          # admin-queue.js, admin-twitch.js, admin-voices.js, admin-server.js,
│                          # admin-settings.js, admin-stats.js, admin-downloads.js (модели, CUDA, пакеты и
│                          # очередь загрузок); admin-start.js — старт после всех вкладок;
│                          # editor.js, train.js — редактор и обучение; overlay.html, voices.html, login.html;
│                          # en.json — английский словарь, i18n.js — перевод страниц; ui-edit.js и ui-edit.css —
│                          # режим правки строк; samples/ — примеры звучания моделей для вкладки «Загрузки»
├── packaging/
│   ├── pin-espeech.py     # загрузки ESpeech в downloads.json для обеих систем и списки заглушек библиотек CUDA:
│                          # windows/stubs/*.def, linux/stubs/**/*.map
│   ├── linux/             # заглушки .so для torch ESpeech в Docker: stubs/<soname>.map (скрипты версий ld; в cublas/ —
│                          # cudart и cuBLAS для образа без CUDA), build-stubs.sh (сборка, стадия espeech-stubs Dockerfile)
├── packaging/windows/     # программа и установщик для Windows: build-runtime.py (рантайм с DLL VC++, код
│                          # espeech-server и DLL-заглушки из stubs/*.def), build-exe.py (Nuitka), StreamTTS.iss (Inno Setup,
│                          # ничего не качает: модель и CUDA — в админке),
│                          # StreamTTS.py (главный модуль Nuitka: зовёт streamtts.desktop, с --supertonic —
│                          # supertonic-server), requirements.txt
│                          # (окружение сборки), release.py (latest.json релиза), THIRD-PARTY.txt и
│                          # THIRD-PARTY.en.txt (сторонние лицензии);
│                          # сборка и релиз в CI — .github/workflows/windows.yml
├── scripts/
│   ├── download-model.sh  # загрузка модели и фронтенда с докачкой
│   ├── import-voices.sh   # регистрация голосов из пар .wav + .txt и сохранение в voices/
│   ├── pack-handoff.py    # архив кода без данных для внешнего исполнителя (handoff/, git bundle и zip)
│   └── measure-docs.py    # замер навигации: файлы и токены задач docs/index.md сейчас и на базе, стоимость входа
├── tools/                 # подбор образцов голосов (docs/voice-pipeline.md), запуск из корня проекта
│   ├── common.py          # общее для инструментов: ROOT, load (сосед по пути), ffmpeg, duration, decode, frames_db,
│   │                      # read_json/write_json; грузится по пути, при импорте — только stdlib
│   ├── voices/            # общее: страница выбора, синтез и оценки, модель эмоций и её сверка на слух, CAM++;
│   │                      # pack-sets.py — коллекции для импорта voice-sets/<модель>/<набор>.zip
│   ├── wc3/               # реплики Warcraft III: нарезка кандидатов, проверка текстов (docs/voices-wc3.md)
│   │                      # (lora-clips.py — реплики и образец 6–7 с для LoRA ESpeech)
│   ├── sources/           # реплики из вики, файлов игр и видео, отбор по голосу, сборка вариантов
│   │                      # (extract-morrowind.py — реплики Morrowind с субтитрами для LoRA ESpeech;
│   │                      # lora-lines.py — то же для мемных голосов из lines.json и папок игр;
│   │                      # fetch-portal2.py — реплики Portal 2: субтитры игры + звук с CDN Portal Wiki)
│   └── espeech/           # ESpeech: prepare.py (папка модели models/espeech из кеша Hugging Face — через
│                          # espeech/prepare.py),
│                          # make-voice.py (голос .espeech из образца или по manifest.json набора),
│                          # train.py (обучение LoRA на парах wav+txt → адаптер в файле голоса)
├── effects/               # эффекты поверх синтеза (post в manifest.json), их запускает и stream-tts:
│                          # villager-fx.py — «житель Minecraft»
├── tests/                 # автотесты (раздел «Тесты»); requirements.txt — pytest для stream-tts/.venv
│   └── e2e/               # e2e stream-tts: fake_cosyvoice.py (поддельный сервер cosyvoice или espeech),
│                          # fake_irc.py (поддельный IRC
│                          # Twitch), fake_twitch.py (поддельный Twitch: вход, Helix, EventSub; сценарии —
│                          # test_twitch.py), harness.py (процесс, API, тестовый оверлей, разбор звука), conftest.py
│                          # (фикстуры, маркер real), test_*.py (сценарии; редактор ESpeech и словарь ударений —
│                          # test_editor_espeech.py, загрузка ESpeech программой — test_espeech_download.py,
│                          # CUDA по кнопке (программа и том Docker) — test_cuda_download.py, вкладка «Загрузки»
│                          # и очередь загрузок — test_downloads_tab.py,
│                          # загрузка Supertonic 3 и его запуск программой и в Docker — test_supertonic_download.py,
│                          # обучение LoRA — test_train.py, инварианты репозитория без процесса —
│                          # test_invariants.py, пути и вопросы docs/index.md — test_index.py, роуты —
│                          # test_routes.py, скрипты админки и она сама в браузере — test_admin_scripts.py,
│                          # test_admin_smoke.py, общий код tools/ — test_tools_common.py, границы слоёв
│                          # streamtts — test_layers.py),
│                          # test_real.py и test_real_espeech.py
│                          # (настоящие модели),
│                          # asr.py (faster-whisper для test_real.py, запускается из корневого .venv)
│   └── espeech/           # espeech-server в espeech/.venv: test_text.py (нарезка, нормализатор, словарь),
│                          # test_normalize_corpus.py (эталонный корпус нормализатора),
│                          # test_server.py (сервер с настоящей моделью), test_train.py (обучение LoRA),
│                          # test_train_api.py (обучение через API сервера)
│   └── supertonic/        # supertonic-server в stream-tts/.venv: test_text.py (текст, нарезка, 44,1 → 24 кГц),
│                          # test_server.py (сервер с настоящей моделью models/supertonic)
├── docs/                  # index.md (задача → файлы, для ИИ-агентов), development.md (этот файл),
│                          # voice-pipeline.md, voices.md, voices-wc3.md,
│                          # stream-tts.md (спецификация), stream-tts-deploy.md, stream-tts-windows.md,
│                          # espeech.md (ESpeech как вторая модель: замеры, espeech-server, план),
│                          # supertonic.md (Supertonic 3 как третья модель: supertonic-server, замеры, лицензии);
│                          # local/ — не в git: ТЗ, план и отчёт рефакторинга, голоса реальных людей
│
│   Данные (в git не попадают):
├── models/                # модели и пакеты (volume; админка качает сюда); espeech/ — модель ESpeech
├── espeech-runtime/       # Docker: python/ — Python 3.14 с torch для ESpeech (volume; админка качает сюда)
├── voices/                # установленные голоса .gguf и .espeech (volume): <голос>.<ext> или <набор>/<голос>.<ext>
├── voice-clips/           # исходные данные голосов: <модель>/<набор>/<голос>/; общие кеши *.json в корне
│   ├── cosyvoice/         # варианты образцов, их синтез, оценки и выбор: wc3, streamers, игры по одной
│   │                      # (dota2, portal, stalker, minecraft, neco-arc, altushka)
│   └── espeech/           # LoRA ESpeech: wc3/, morrowind/ — <голос>/lora/ (реплики), адаптеры, <голос>.espeech;
│                          # picks.json набора — выбор на слух для коллекции
├── voice-refs/cosyvoice/  # итоговые образцы CosyVoice (.wav + .txt) и manifest.json по наборам
├── voice-sets/            # коллекции для импорта: <модель>/<набор>.zip (tools/voices/pack-sets.py)
├── voice-origs/           # сырые звуковые паки
├── tools/local.json       # записи инструментов о голосах реальных людей (lora-lines.py, emotion-eval.py)
├── samples/               # тестовые аудио
├── runtime/               # нативный cosyvoice-server с DLL и ffmpeg для stream-tts из исходников (Windows)
└── stream-tts-data/       # настройки, токены и история stream-tts (volume)
```

## Образы

Корневой `Dockerfile`, цели выбирает `docker-compose.yml`:
- `stream-tts` (`stream-tts:latest`, один и с видеокартой, и без) — stream-tts со встроенным cosyvoice-server
  ([stream-tts-deploy.md](stream-tts-deploy.md#docker)); CUDA в него не входит, её качает админка в том `./cuda`;
- `cosyvoice` (`cosyvoice-server:cpu`, `cosyvoice-server:cuda`) — только сервер для инструментов подготовки голосов
  (`scripts/`, `tools/`), профиль `server-only`.

Обе цели на `ubuntu:26.04`: релизы cosyvoice.cpp собраны там и требуют glibc 2.43 и ICU 78 (в `python:*-slim` —
2.41).
- Стадия `fetch` скачивает:
  - архив cosyvoice.cpp (`cosyvoice-server`, `cosyvoice-cli`, `quantize`, `libcosyvoice`, ONNX Runtime);
  - все `libggml*.so*` из релиза llama.cpp.
- Стадия `espeech-stubs` собирает gcc заглушки библиотек CUDA для облегчённого torch ESpeech
  (`packaging/linux/build-stubs.sh` по спискам `packaging/linux/stubs`; [espeech.md](espeech.md), «Поставка»).
- Стадия `base` — рантайм-библиотеки сервера (ICU, libgomp, FFmpeg `libav*`) и `/opt/cosyvoice`, без curl и
  компиляторов. Цель `stream-tts` добавляет Python 3.14 из apt (пакеты — в venv `/opt/venv`, PEP 668), ffmpeg и
  `/opt/cosyvoice/espeech` — код espeech-server и заглушки (сотни КБ; torch и модель качает админка в тома).
- `LD_LIBRARY_PATH=/opt/cosyvoice` задан только в цели `cosyvoice`: там свой `libonnxruntime.so` (1.30), в
  stream-tts он перебил бы onnxruntime из venv, поэтому путь серверу добавляет `engines/cosyvoice/launch.py`.
- Контекст сборки — весь проект, `.dockerignore` пускает в него только `cosyvoice/entrypoint.sh`,
  `stream-tts/requirements.txt`, `stream-tts/streamtts/`, `espeech/*.py` (с `THIRD-PARTY.txt`) и `packaging/linux/`.

**GGML** в релизах cosyvoice.cpp не поставляется, его берут из llama.cpp. Копируются все варианты
`libggml-cpu-<isa>.so`, и сервер сам выбирает лучший для текущего процессора (на Zen 3 это `haswell`).

**GPU.** Образ `cosyvoice-server:cuda` (`GPU=cuda`) берёт GGML из CUDA-сборки того же релиза llama.cpp
(`libggml-cuda.so`) и рантайм CUDA 13 (cudart, cuBLAS) из архива `cudart-llama-*`. У `stream-tts` CUDA в образе нет:
те же два архива (`cuda.linux` в `downloads.json`) качает кнопка на вкладке «Загрузки» в том `/cuda`
(`STREAM_TTS_CUDA`), а `engines/cosyvoice/launch.py` подключает оттуда `libggml-cuda.so` через `GGML_BACKEND_PATH` и
её библиотеки через `LD_LIBRARY_PATH`. Ядро GGML в образе при этом из сборки для процессора: `libggml-base.so` у
двух сборок llama.cpp разные, но той же версии, и речь та же (проверено 2026-11-06: распознанный текст фразы
совпал с образом, собранным с `GPU=cuda`). Новая версия llama.cpp — новые `cuda.linux` и проверка звука на
видеокарте (`tests/e2e/test_cuda_download.py` сверяет, что архивы — из релиза и CUDA в `Dockerfile`). Без двух
настроек CUDA-бэкенд выдаёт плохой звук, и обе уже заданы:
- `GGML_CUDA_DISABLE_GRAPHS=1` (в образе и в `engines/cosyvoice/launch.py`). С включёнными CUDA Graphs звук тихий (около −36 дБ
  вместо −17) и шумный, а с flash attention в LLM вместо речи получается мусор. Ошибок в логах при этом нет. Это тот
  же эффект, что в [Lourdle/cosyvoice.cpp#15](https://github.com/Lourdle/cosyvoice.cpp/issues/15). GGML,
  собранный из исходников, без этой настройки ведёт себя так же, поэтому образ использует готовую сборку.
- `--llm-flash-attn 0` (`engines/cosyvoice/launch.py` на бэкенде CUDA, для `server-only` — `docker-compose.gpu.yml`). С flash
  attention в LLM в начале фразы чаще появляются лишние звуки: 8 из 24 тестовых фраз против 4 из 24 без него и
  3 из 24 на CPU с теми же seed. Flash attention во flow на качество не влияет и остаётся включённым.

**`cosyvoice/entrypoint.sh`** (только профиль `server-only`; встроенному серверу аргументы собирает `engines/cosyvoice/launch.py`):
- проверяет поддержку AVX2;
- передаёт серверу `--model`: из `MODEL` или единственный `*.gguf` в `/models`;
- регистрирует голоса из `/voices/**/*.gguf` (путь в подпапках превращается в имя через `_`; у встроенного сервера
  имя голоса — имя файла);
- передаёт пути ONNX-фронтенда (`--speech-tokenizer`, `--campplus`). В режиме WebUI флаги фронтенд не загружают, а
  только задают пути (`GET /frontend/model`): загружают его по ним те, кому он нужен, — stream-tts при создании
  голоса, `scripts/import-voices.sh` и `tools/voices/synth.py` (`POST /frontend/model/load`). Так сервер не держит
  ~300 МБ, пока голоса не создаются.

**Сеть:** порты пробрасываются только на `127.0.0.1`. Чтобы открыть сервер для локальной сети, уберите `127.0.0.1:`
у порта 8080 в `docker-compose.yml` и задайте серверу `--api-key`.

`--max-llm-len` (по умолчанию 4096) на видеопамять не влияет, а 1024 уже не хватает длинным образцам.

## Обновление версий

Версии задаются аргументами в `Dockerfile`:

```dockerfile
ARG COSYVOICE_TAG=v0.2.0
ARG COSYVOICE_ASSET=cosyvoice-67d45fe-linux-x86_64-ffmpeg.tgz
ARG LLAMACPP_TAG=b11500
```

Релиз llama.cpp лучше брать ближайший по дате к релизу cosyvoice.cpp: у GGML нет стабильного ABI.
После обновления проверьте, что в выводе появился `loaded CPU backend` (с CUDA ещё и `CUDA0`; `./cuda` — том с
CUDA, скачанной в админке):

```bash
docker run --rm -e LD_LIBRARY_PATH=/opt/cosyvoice --entrypoint /opt/cosyvoice/cosyvoice-cli stream-tts --list-backends
docker run --rm --gpus all -v ./cuda:/cuda -e LD_LIBRARY_PATH=/opt/cosyvoice:/cuda -e GGML_BACKEND_PATH=/cuda/libggml-cuda.so --entrypoint /opt/cosyvoice/cosyvoice-cli stream-tts --list-backends
```

После обновления версий на GPU стоит проверить, не исправлена ли ошибка с CUDA Graphs: уберите
`GGML_CUDA_DISABLE_GRAPHS` (в `Dockerfile` и `engines/cosyvoice/launch.py`) и сравните громкость и разборчивость одной фразы с CPU-образом.

## Запуск

- Стек: `docker compose up -d --build` (`.env` настроен на GPU) — stream-tts со встроенным сервером; после правок —
  `docker compose up -d --build stream-tts`. Только сервер — `docker compose up -d cosyvoice` (профиль `server-only`),
  stream-tts сначала остановить: оба занимают 8080 и видеокарту. Сервисы могут быть в работе (стрим): перезапуск
  контейнеров — по согласованию с пользователем.
- Окружения Python: `stream-tts/.venv` (3.14) — stream-tts и `tests/e2e`; `espeech/.venv` (3.14,
  `espeech/requirements.txt`) — espeech-server и `tests/espeech`; корневой `.venv` (3.11) — инструменты `tools/`
  (numpy, onnxruntime, faster-whisper, torch cu124 и др.; cuBLAS и cuDNN для faster-whisper — из `torch/lib`) и
  `scripts/measure-docs.py` (tiktoken). `review.py` и `pick.py` обходятся стандартной библиотекой.
- Инструменты запускаются из корня проекта: `python tools/wc3/review.py b02`. Везде нужны `ffmpeg`/`ffprobe` в PATH;
  звук в пайплайне — 24 кГц моно.
- Shell-скрипты — bash (Git Bash), из PowerShell не запускаются.

**Порты и конфигурации** (`.claude/launch.json`):

| Порт | Что |
|---|---|
| 8080 | cosyvoice-server (в Docker — внутри контейнера stream-tts) |
| 8090 | stream-tts в Docker |
| 8092 | `stream-tts` — из исходников, данные в `stream-tts-data/native/`; его cosyvoice-server — на свободном порту |
| 8093 | `stream-tts-en` — английский интерфейс без модели |
| 8094 | `espeech` — espeech-server |
| 8095 | `stream-tts-espeech` — свои данные, голоса и словарь ударений в `stream-tts-data/espeech-test/` |
| 8097 | `stream-tts-espeech-dl` — загрузка ESpeech как у программы для Windows, всё в `stream-tts-data/espeech-dl/` |
| 8098 | `streamtts-exe-test` — собранный exe с данными в `stream-tts-data/exe-test/` |
| 8099 | `stream-tts-ui-edit` — правка строк интерфейса на поддельном сервере (`stream-tts/ui-edit.py`) |
| 8765, 8766, 8767 | `review`, `pick`, `emotion-eval` — страницы инструментов |

Встроенный espeech-server (выбрана ESpeech) — на свободном порту или `ESPEECH_PORT`, supertonic-server (выбрана
Supertonic 3) — или `SUPERTONIC_PORT`.

## stream-tts из исходников

`cd stream-tts && python -m venv .venv && .venv/Scripts/pip install -r requirements.txt`,
затем `.venv/Scripts/python -m streamtts`: stream-tts сам запускает нативный cosyvoice-server из `runtime/` в
корне проекта ([stream-tts-deploy.md](stream-tts-deploy.md)) с моделью из `models/` и голосами из `voices/`.
Конфигурация `stream-tts` в `.claude/launch.json` — на порту 8092 и с данными в `stream-tts-data/native/`, чтобы не
мешать Docker-версии. Английский интерфейс — конфигурация `stream-tts-en` (порт 8093, без модели) и
проверка словаря `python stream-tts/check-i18n.py` (её же запускает `tests/e2e/test_invariants.py`). Свои варианты строк интерфейса —
`stream-tts-ui-edit` (порт 8099, [stream-tts.md](stream-tts.md), «Правка текста интерфейса»). ESpeech со своими данными — `stream-tts-espeech` (порт 8095,
данные, голоса и словарь ударений в `stream-tts-data/espeech-test/`, модель из `models/`; ESpeech выбирается на
вкладке «Сервер»): редактор голоса и словарь проверяются, не трогая `voices/` проекта. Загрузку ESpeech, как у
программы для Windows, — `stream-tts-espeech-dl` (порт 8097, всё в `stream-tts-data/espeech-dl/`: `rt/` — копия
`packaging/windows/build/runtime/core` и `cuda` после `build-runtime.py --cuda`, туда же скачанный рантайм и модель),
собранный `build-exe.py --runtime all` exe — `streamtts-exe-test` (порт 8098, данные, модели и голоса в
`stream-tts-data/exe-test/`, ESpeech качается в `dist/StreamTTS/espeech-runtime`). Программа для Windows —
[stream-tts-windows.md](stream-tts-windows.md).

## Тесты

e2e-тесты stream-tts — `tests/e2e/`, pytest в `stream-tts/.venv`:

```bash
stream-tts/.venv/Scripts/pip install -r tests/requirements.txt
stream-tts/.venv/Scripts/python -m pytest tests/e2e
```

Тест запускает настоящий процесс stream-tts (`python -m streamtts`) на свободном порту, данные, модели, голоса и
проект у него во временной папке. Вместо cosyvoice-server — поддельный (`fake_cosyvoice.py`, адрес — в `STREAM_TTS_TEST_SERVERS`, только для
тестов): те же адреса API, а
вместо речи синус, у каждого голоса своя частота и громкость. `FakeCosy(engine="espeech", voices=…)` — поддельный
espeech-server: `/status` с `engine`, `capabilities` и схемой `params`, `/accent`, словарь ударений `/accents`
(синтез записывает, как прочитал бы текст), голос из образца без фронтенда с полем `tempo`, поток кусками по 1 с,
проверка `nfe_steps`. Так тест узнаёт голос фразы и проверяет писк 800 Гц
и выравнивание громкости. Голос, извлечённый из образца (редактор), звучит основной частотой образца. Видеокарта,
модель и сеть не нужны (нужен только `ffmpeg` в PATH, для mp3 примеров), Docker-версию на 8090 тесты не трогают,
весь набор идёт ~1 мин. Оверлей играет сам тест: подключается к `/overlay/ws`, сообщает `ready` и `ended`, собирает
звук.

- Фикстура `tts` — общий на сессию процесс, перед каждым тестом приведённый в исходное состояние (настройки,
  очередь, баны, голоса, примеры готовы). После теста она проверяет, что к cosyvoice не шло двух запросов
  одновременно. `spawn` — отдельный процесс для другого окружения (пароль, адрес, фронтенд), перезапуска или чистой
  базы. `editor` — процесс на модуль с поддельным сервером, у которого есть ONNX-фронтенд; черновик перед тестом
  удаляется.
- Ожидание — `wait_for` с опросом `/api/state` и истории в SQLite (`StreamTTS.event`, `StreamTTS.sql`), без
  фиксированных пауз. Что запрос встал в очередь к серверу синтеза и ждёт, видно по `cosy.waiting` в `/api/state`.
- Поддельным сервером тест управляет на ходу: `hold` (синтез ждёт, чтобы застать фразу на синтезе; строка — ждут
  только тексты с ней), `fail` (ошибка 500), `down` (сервер недоступен), `length_factor` (звук длиннее расчётного),
  `fail_save` (ошибка `/speaker/save`), `save_delay` (задержка ответа `/speaker/save`), `voices` (голоса сервера:
  добавить или убрать). Запросы записываются: `tts` (синтез), `calls` (извлечение, сохранение, удаление голоса,
  загрузка и выгрузка фронтенда), `lists` (сколько раз перечитан список голосов).
- Список голосов stream-tts перечитывает раз в 60 с, сервер проверяет раз в 5 с; в тестах — раз в секунду:
  `STREAM_TTS_VOICES_CHECK`, `STREAM_TTS_COSY_CHECK` (`__main__.py`, только для тестов).
- Чат Twitch — поддельный IRC (`fake_irc.py`, фикстура `chat`): stream-tts в режиме «Только чтение чата» читает
  канал `testchan`, а тест шлёт от имени Twitch сообщения с тегами (бейджи, эмоуты, биты, ответ), `CLEARMSG`,
  `CLEARCHAT` и `RECONNECT`. Адрес и таймауты — `STREAM_TTS_IRC_URL`, `STREAM_TTS_IRC_JOIN_WAIT`,
  `STREAM_TTS_IRC_IDLE`, `STREAM_TTS_IRC_PONG_WAIT` (`chat_irc.py`, только для тестов). Что сообщение
  проигнорировано, проверяется после `chat.sync()`: поддельный IRC шлёт PING с меткой и ждёт PONG, а stream-tts
  разбирает строки одного соединения по очереди, так что к ответу всё прежнее уже обработано.
- Twitch-приложение — поддельный Twitch (`fake_twitch.py`, фикстура `twitch`: процесс на модуль, перед тестом
  выход, чистый канал и новый вход по Device Code Flow). Адрес — `STREAM_TTS_TEST_TWITCH` (`config.py`, только для
  тестов): вход — `<адрес>/oauth2`, Helix — `<адрес>/helix`, EventSub — `ws://…/ws`. Тест шлёт от имени Twitch
  погашения (`redeem`, с `deliver=False` — без уведомления, как за время обрыва) и сообщения чата (`say`), управляет
  входом (`authorize`, `expire_tokens`, `refresh_ok`), наградой (`affiliate`, `rewards`) и EventSub
  (`session_reconnect`, `drop`, `mute`, `keepalive`, `revoke_access`); запросы stream-tts записываются (`statuses`,
  `chat`, `calls`, `revoked`, `refreshes`). Пропущенное уведомление проверяется после следующего, дошедшего до
  оверлея: соединение EventSub stream-tts разбирает по очереди.
- Процесс останавливается `terminate`, то есть без `on_cleanup`: корректная остановка (награда на паузу) тестами
  не проверяется.

Сценарии: `test_access.py` — запуск, голоса и примеры, пароль, защита POST, `STREAM_TTS_BIND` без пароля,
настройки после перезапуска, пропажа cosyvoice; `test_queue.py` — очередь через `/api/test`: голос и порядок,
громкость, отказы, фильтры, стриминг и `chunk_tokens`, обрезка, ошибка синтеза, пропуск и очистка, оверлеи;
`test_chat.py` — режим «Только чтение чата»: подключение и «канал не найден», команда озвучки (доступ по бейджам,
кулдауны, «Всегда доступно», своё имя), «все сообщения», что из сообщения читается, `!ttssf`, команды модера,
`CLEARMSG`/`CLEARCHAT`, переподключение по `RECONNECT` и по тишине; `test_twitch.py` — Twitch-приложение: вход и
выход, неверный Client ID, вход из токена после перезапуска с возвратом зависших погашений, награда из админки
(создание, правка, удаление, ошибки Twitch, пауза), погашение → FULFILLED или CANCELED с ответом зрителю, повтор
уведомления, команда и сообщения чата через EventSub, `session_reconnect`, обрыв с догоном погашений, тишина без
keepalive, обновление токена, истёкший вход, отозванный доступ; `test_voices.py` — голос по хешу ника и после
выключения, новый и удалённый голос на сервере, «Включить/Выключить все», страница голосов и `voices.zip`,
`gain_db`, смена фразы примеров, живая фраза прерывает пример, приоритет «послушать», разделы по наборам (папки
обеих моделей), эффекты `post` и отказ скриптам не из `effects/`; `test_voicesets.py` — наборы из zip без процесса
stream-tts (`voicesets.py` напрямую): коллекции CosyVoice и ESpeech в одной папке набора, замена и удаление по модели,
занятые имена, битые архивы; `test_espeech_download.py` — загрузка ESpeech без сети (`partzip.py`,
`engines/espeech/download.py` напрямую, локальный HTTP-сервер с Range): только нужные файлы zip диапазонами, докачка
после обрыва без повторной загрузки, выбор по списку, подмена хвоста или файла, распаковка колёс и Python из tar.gz,
состояние рантайма, закреплённое в `downloads.json` для обеих систем (и заглушки к нему);
`test_cuda_download.py` — CUDA по кнопке без сети (`install.py` и запуск cosyvoice-server напрямую): распаковка zip
и tar.gz, загрузка в отдельную папку (том `/cuda` в Docker) с локального HTTP-сервера, когда кнопка предлагается,
`GGML_BACKEND_PATH` для CUDA не рядом с сервером, закреплённые архивы Linux — из релиза и CUDA `Dockerfile`, образ
`stream-tts` один;
`test_supertonic_download.py` — загрузка Supertonic 3 без сети (stream-tts с поддельным встроенным supertonic-server,
файлы с локального HTTP-сервера по закреплению из `STREAM_TTS_TEST_SUPERTONIC` — только тестам): докачка после обрыва,
неверный sha256, готовые голоса в группе страницы голосов с примерами, запуск ждущего сервера, «Поставить голоса»,
удаление модели с голосами; закрепление настоящей модели; командная строка сервера у собранной программы и в Docker,
`StreamTTS.py --supertonic` и модули `espeech/` в сборке exe; `test_editor.py` — редактор голоса: загрузка записей (ffmpeg, ошибки,
предел), варианты и их проверки, синтез ячеек (фронтенд, `_draft-*`, перерегистрация, кэш, сравнение), сохранение
(имя, путь в `<голоса>/<набор>/`, manifest, образец, ошибка сервера), черновик после перезапуска и его удаление, сервер
без фронтенда, очередь с живой озвучкой, выгрузка фронтенда, «убрать музыку» без пакета; `test_stats.py` —
`/api/stats` (подсчёты, по часам, `hours`), удаление записей старше 30 дней, миграция базы и `settings.json` со
старых версий; `test_engines.py` — две модели на двух поддельных серверах: список и смена модели,
настройки по схеме `params` (`nfe_steps` в запросе, значение не из схемы — отказ), голос без файла для модели
(выбор зрителя сохраняется), свои примеры у каждой модели, порог стриминга, «как прочитает», выбор после
перезапуска; `test_editor_espeech.py` — редактор с ESpeech (пределы 4–12 с, «Ударения» без фильтров, регистрация
без фронтенда с текстом в разметке и темпом по отрезку, путь `<набор>/<имя>.espeech`, голос другой модели — в его
наборе) и словарь ударений (разметка и синтез, отказ, удаление); `test_train.py` — обучение LoRA голоса ESpeech
(поддельный espeech-server с `train=True`): набор (реплики файлами — 24 кГц моно, пределы 0,3–30 с; запись для
нарезки и реплика из отрезка; удаление), старт (набор WAV PCM16 парами audio + text, шаги по объёму или заданные,
отказ без текста), ход, второй старт — `busy`, «последний» во время обучения, остановка, прослушивание
`none`/`last`/`best` по частоте, сохранение (пример голоса заново), сброс; проверка на 30 репликах — лучший снимок и
«пора остановиться»; несохранённые результаты (`unsaved`, старт с `discard`), `oom` и затем экономия видеопамяти; в
режиме managed (поддельный сервер отдельным процессом как `python -m espeech` из venv во временной папке) — отказ
действий с перезапуском сервера во время обучения, вопрос с несохранёнными результатами, `force`, «результаты
пропали» после падения сервера; `test_every_route_guarded` обходит все POST `/api/*` из `web.make_app`: каждый — в
группе `RESTARTS` (во время обучения 409), `KEEPS` (сервер тот же, обучение идёт) или `SKIPS` (ручки обучения и
запросы в сеть), новый роут без группы — ошибка. `test_invariants.py` — без процесса stream-tts, секунды: компиляция
всех `.py` из git, `check-i18n.py`, `docker compose config -q` (без docker — пропуск), флаги GPU у встроенного
сервера, в `Dockerfile` и `docker-compose.gpu.yml`, `KEEPALIVE` < 5 с, `__version__` только в
`streamtts/__init__.py`, фильтр `scripts/pack-handoff.py`. `test_tools_common.py` — в `tools/` нет своих копий
функций `tools/common.py` и загрузчиков по пути, у `common.py` импорты верхнего уровня только из stdlib.
`test_layers.py` — импорты `streamtts/**/*.py` разбором AST: `aiohttp.web` только в `web/` и `__main__.py`, движки не
импортируют слои над собой (`ABOVE_ENGINES`), циклов импорта нет, считая импорты внутри функций; сам разбор
проверяется на маленьком пакете с нарушениями. `test_index.py` — таблица `docs/index.md`: строки на все
эталонные вопросы, пути из git (сейчас и на базе), разбор `scripts/measure-docs.py`. `test_routes.py` — снимок
всех пар «метод + путь» из `web.make_app`: URL — публичные имена. `test_admin_scripts.py` — скрипты из
`admin.html` есть, верхнего уровня объявления не повторяются между файлами (классические скрипты с общей
областью), `node --check` (без node — пропуск). `test_admin_smoke.py` — админка в headless Edge или Chrome по
CDP (без браузера — пропуск): английский интерфейс, все вкладки у CosyVoice и ESpeech, падает на ошибках JS, ошибках
загрузки и строках без перевода (`[...I18N_MISSING]`). Twitch с приложением (вход, награда, EventSub) пока не
покрыт.

**С настоящей моделью** (`test_real.py`, маркер `real`, без него тесты пропускаются):

```bash
stream-tts/.venv/Scripts/python -m pytest tests/e2e -m real
```

stream-tts сам запускает свой cosyvoice-server из `runtime/` на свободном порту, со своими данными.
Модель `CosyVoice3-2512_Q5_K_M.gguf`, ONNX-фронтенд и htdemucs — жёсткие ссылки (на другом диске — копии, удаляются
после прогона) во временной папке моделей, голос `dota2-pudge` — копия; оригиналы в `models/` и `voices/` не
меняются. Это второй сервер на той же видеокарте, поэтому сначала проверяется свободная видеопамять (`nvidia-smi`,
нужно 2,5 ГБ); если её мало (стрим), тесты пропускаются, Docker не останавливается. Фраза проверяется по звуку,
потому что сбой CUDA-бэкенда (без `GGML_CUDA_DISABLE_GRAPHS=1` и `--llm-flash-attn 0`) — тихий шум или мусор без
ошибок в логах: длина в разумных пределах, громкость около −16 LUFS, текст, распознанный faster-whisper small
(`asr.py` подпроцессом из корневого `.venv`, cuBLAS и cuDNN — из `torch/lib`, без CUDA — на процессоре), совпадает
с исходным хотя бы на 60 % слов. Проверяется синтез целиком и стримингом, голос из записи через редактор (с
очисткой от музыки, если есть `models/demucs`) и его сохранение в `<голоса>/<набор>/`, потом то же на CPU
(`/api/backend`, сервер перезапускается). `test_real_espeech.py` — ESpeech: espeech-server из
`espeech/.venv` (`ESPEECH_SERVER`), модель — ссылки на `models/espeech`, голос `wc3-jaina` собирает
`tools/espeech/make-voice.py` из `voice-refs/cosyvoice/wc3/wc3-jaina.wav` (без них уровень пропускается): синтез короткой
(целиком) и длинной (потоком) фразы, «как прочитает», голос из редактора (кусок записи Джайны, темп по всей
записи, сохранение в `.espeech`, синтез им), словарь ударений (омограф «замок», файл `<данные>/espeech/accents.json`),
проверка скорости на 32 шагах, обучение LoRA со страницы обучения (40 шагов с экономией видеопамяти, прослушивание
«последний» и «без LoRA», вопрос перед перезапуском, сохранение в голос и синтез им), а если есть и cosyvoice-server с
Q5_K_M — смена модели туда и обратно со своими голосами у каждой. Время синтеза и первого звука выводится в конце
прогона («замеры с настоящей моделью»), без порогов; весь уровень идёт ~3 мин.

**supertonic-server** — `tests/supertonic/`, pytest в `stream-tts/.venv` (~40 с; `test_server.py` пропускается без
`models/supertonic`, путь — `SUPERTONIC_MODELS`):

```bash
stream-tts/.venv/Scripts/python -m pytest tests/supertonic
```

**espeech-server** — `tests/espeech/`, pytest в `espeech/.venv` (`espeech/requirements.txt`):

```bash
espeech/.venv/Scripts/python -m pytest tests/espeech
```

`test_text.py` — нарезка фразы, длительность кусков, нормализатор, без модели; `test_normalize_corpus.py` —
эталонный корпус нормализатора (фраза чата → ожидаемый текст), без модели. `test_server.py` — сервер отдельным
процессом на свободном порту с голосами во временной папке и настоящей моделью из `models/espeech` (пропускается без
неё или без образца `voice-refs/cosyvoice/wc3/wc3-jaina.wav`): голос создаётся через API, из него — голоса с нулевым и
случайным адаптером LoRA (нулевой должен дать звук бит в бит как без адаптера); сохранение по пути от папки
голосов; словарь ударений (омограф «замок» по словарю, звук другой, файл `--user-accents`). `test_train.py` —
обучение LoRA на двух образцах (`wc3-jaina`, `wc3-arthas`): шаги, остановка по флагу и продолжение, тот же лосс с
gradient checkpointing, после обучения синтез без адаптера и с нулевым адаптером бит в бит как до него, обученный
адаптер в файле голоса меняет звук; CLI `tools/espeech/train.py` (звук 44,1 кГц стерео, тексты без «+») → голос,
которым синтезирует espeech-server. `test_train_api.py` — обучение через API сервера (`POST /train` и др.) на
четырёх образцах `voice-refs/wc3` и правила без модели (деление на проверку, шаги по объёму, признак остановки,
multipart): ошибки запроса, ход, 409 на второй старт, синтез между шагами бит в бит как без обучения, остановка,
прослушивание last / best / none, сохранение в файл голоса с `.bak` и синтез им, 30 реплик — проверка по правилу и
шагов по объёму, сброс во время обучения, выгрузка модели во время обучения,
нехватка видеопамяти (второй сервер с `--vram-limit-mb 1400`: ошибка `oom`, память вернулась, с checkpointing
идёт). ~2 мин на видеокарте всё вместе.
