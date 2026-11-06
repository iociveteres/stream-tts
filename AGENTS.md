# AGENTS.md

Локальный синтез речи с клонированием голоса для озвучки наград Twitch: stream-tts (`stream-tts/streamtts/`, :8090)
сам запускает сервер синтеза — cosyvoice.cpp (CosyVoice3 0.5B, GGUF) или espeech-server (`espeech/`, F5-TTS) — и
играет фразы в оверлей OBS. Хост: Windows 10, Docker Desktop (WSL2), Ryzen 5 5600G, RTX 3060 Ti 8 ГБ; сервисы обычно работают на GPU.

**Куда идти — [docs/index.md](docs/index.md)**: задача → файлы → тест, области, данные, связи между частями.
Дерево файлов, порты, запуск, тесты — [docs/development.md](docs/development.md); README — только для пользователя.
Локальные доки (не в git) — `docs/local/`: рефакторинг под агентов, голоса реальных людей, публичная копия репозитория (`publish.py`).

## Инварианты

В скобках — тест-сторож из `tests/e2e` или почему его нет. Подробности — по ссылке.

- К cosyvoice-server — **строго один запрос за раз**: параллельная регистрация голосов роняет его (`double free`).
  (Фикстуры `tts`, `editor`, `tr` в `conftest.py` и `test_train.py`; docs/stream-tts.md, «Особенности
  cosyvoice-server».)
- `chunk_tokens` — в каждом запросе: сервер помнит прошлое значение. (`test_queue.py`, `test_engines.py`.)
- Keep-alive к серверу — не дольше 2 с (`KEEPALIVE` в `engines/client.py`): сервер закрывает простаивающее через 5 с,
  и фраза после паузы падает с «Server disconnected». (`test_invariants.py`.)
- `GGML_CUDA_DISABLE_GRAPHS=1` и `--llm-flash-attn 0` не убирать: без них на GPU тихий шум или мусор вместо речи без
  ошибок в логах. (`test_invariants.py`; звук — `test_real.py`, только на видеокарте.)
- ONNX-фронтенд сервер держит выгруженным: кто извлекает голос, сначала `POST /frontend/model/load`.
  (`test_editor.py`.)
- Имя голоса у встроенного сервера = имя файла: `voices/<набор>/<голос>.gguf` или `.espeech`, один уровень подпапок.
  (`test_voices.py`, `test_editor.py`, `test_voicesets.py`.)
- Ключ варианта CosyVoice в черновиках редактора не менять — пропадёт кэш черновиков. (`test_invariants.py`.)
- `post` — `[скрипт, аргументы…]`, путь от корня; stream-tts запускает только `effects/*.py`. (`test_voices.py`.)
- Действие, которое перезапускает или останавливает встроенный сервер, — через `_restart_guard` в `web/common.py`: обучение
  LoRA и его несохранённые результаты живут в процессе espeech-server, иначе молча пропадёт до 15 минут обучения.
  (`test_train.py`: `test_every_route_guarded` — новый POST без группы роняет тест.)
- espeech-server: всё, что трогает модель, — в рабочем потоке (`Engine.call`/`stream`), шаг обучения — отдельное
  задание потока; модель `requires_grad_(False)`, копии адаптеров — под `no_grad`. (Без теста: рост памяти;
  docs/espeech.md, «espeech-server».)
- `espeech/model.py` совпадает с f5-tts бит в бит; обучающий forward — свой в `espeech/train.py`. (Без теста:
  эталон в `samples/espeech-lean`.)
- Пакет `espeech` импортируется раньше torch (подключает библиотеки CUDA), `LD_LIBRARY_PATH` не задавать;
  `RUAccent.load` не вызывать. (`test_invariants.py`.)
- torch у программы и в Docker — без cuDNN, cuFFT и др. (заглушки): новая операция torch в espeech-server ломает
  поставку, хотя из `espeech/.venv` всё работает. (Без теста: нужен рантайм; docs/espeech.md, «Поставка».)
- Строки интерфейса — `tr("…")` в Python, `T("…")` в `static/*.js`, перевод — `static/en.json`; строки разметки
  переводятся сами (абзац со ссылкой или `<code>` — с `data-t`), логи (`log.*`) не переводятся. Ключ без перевода
  и лишний ключ (строки нет ни в одном источнике) — ошибка. (`test_invariants.py`: `check-i18n.py`; разметка —
  `test_admin_smoke.py`, админка на английском в headless браузере.)
- Nuitka не видит импорты эффектов: их зависимости — в `--include-module` в `build-exe.py`. (Без теста: сборка exe;
  docs/stream-tts-windows.md, «Сборка».)
- Слои stream-tts: `aiohttp.web` — только в `web/` и `__main__.py`, `engines/` не импортируют слои над собой, циклов
  импорта нет, и ленивых тоже. (`test_layers.py`; docs/index.md, «Связи».)
- Версия программы — только `__version__` в `streamtts/__init__.py`; релиз — аннотированный тег `v<версия>`.
  (`test_invariants.py`.)
- Пути и вопросы `docs/index.md` — живые: переименовал файл — поправь индекс. (`test_index.py`.)

## Запреты

- Docker-контейнеры не перезапускать без согласования: сервисы могут быть в работе (стрим). Собранный exe и
  stream-tts из исходников — на других портах и со своими данными.
- Не запускать вместе `scripts/import-voices.sh`, `tools/voices/synth.py` (и кнопку в `pick.py`) и синтез stream-tts.
- `voice-clips/` — ручная работа пользователя (`ratings.txt`, `text-fixes.json`, `phrase-tags.json`, `picks.json`,
  `alts.json`, `emotion-eval.json`), в git её нет: без нужды не перезаписывать, перед массовой правкой — копия в
  scratchpad.
- Данные (`voices/`, `models/`, `voice-clips/`, `voice-refs/`, `voice-origs/`, `samples/`, `stream-tts-data/`, `.env`)
  — ни в git, ни в образы.
- Лицензии: модель эмоций (`emotions.py`, CC BY-NC-SA 4.0) — только для разметки образцов, не для синтеза; веса
  ESpeech не класть в установщик — программа качает их по кнопке (docs/espeech.md, «Лицензии»).
- Голоса реальных людей — только для шуток на стриме; предпочтительны оригинальные
  записи, примеры JeetBot — запасной вариант.
- `STREAM_TTS_TEST_SERVERS` — только тестам, в документацию для пользователя не писать.

## Проверка

- stream-tts: `stream-tts/.venv/Scripts/python -m pytest tests/e2e` (~1,5 мин, без видеокарты и модели, Docker не
  трогает; с ним — синтаксис всех `.py`, словарь интерфейса и инварианты выше). Новое поведение — сценарием в
  `tests/e2e/`. После правок синтеза, `runtime.py`, `engines/` или обновления cosyvoice.cpp — ещё `-m real`.
- espeech-server: `espeech/.venv/Scripts/python -m pytest tests/espeech` (~1,5 мин, настоящая модель).
- Окружения: `stream-tts/.venv` и `espeech/.venv` (3.14), корневой `.venv` (3.11) — инструменты `tools/` и
  `scripts/measure-docs.py`; запуск из корня проекта (docs/development.md, «Запуск»).
- Страницы инструментов и stream-tts — через preview (`.claude/launch.json`) с проверкой консоли и запросов; образ —
  `docker compose build <сервис>`; голоса и эффекты — фраза через `POST /tts`, оценку на слух делает пользователь.
- То, что надо проверять регулярно, — тестом (обычно в `tests/e2e/`), а не пунктом «проверить вручную».

## Соглашения

- Язык проекта — русский: документация, комментарии, интерфейс, коммиты, ответы пользователю.
- Английский интерфейс: `static/en.json` (~19k токенов) целиком не читать — `python stream-tts/check-i18n.py
  --missing` печатает недостающие ключи, переводы вписать и вставить в конец словаря правкой по последней строке
  (docs/index.md, В7).
- Коммит: «область: что сделано» (`stream-tts: короткие фразы целиком…`), в теле — зачем и что проверено.
- Коммиты — в `dev`; `main` — сквош `dev` на выпуск, туда не коммитить (docs/development.md, «Ветки»).
- Инструменты — самостоятельные скрипты с docstring в начале (назначение, `usage:`, входы и выходы); поменял
  поведение — обнови docstring и документ в `docs/`.
- Комментарии короткие и объясняют «почему»: замеры, найденные баги, причины выбора значений.
- Shell-скрипты (bash, из Git Bash), `Dockerfile` и `*.yml` — с LF; скрипты сами делают `cd` в корень проекта.
- Новые и перенесённые файлы — в дерево `docs/development.md`; новая папка или задача — ещё и в `docs/index.md`.
- Имена в русской локализации WC3 2002 года звучат по-старому (Артес, Тиренда, Малганус, Кел-Тузед, Мэв, Фарион,
  Маннорох) — и в расшифровках, и в тексте для синтеза (docs/voices-wc3.md).
