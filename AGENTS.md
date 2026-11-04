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
| `setup.sh`, `Dockerfile`, `docker-compose*.yml`, `cosyvoice/` | Docker: stream-tts со встроенным cosyvoice-server (:8090 и :8080), профиль `server-only` — только сервер для `scripts/` и `tools/` (`cosyvoice/entrypoint.sh`); образы из готовых релизов cosyvoice.cpp и llama.cpp (код сервера не наш) | да |
| `espeech/` | espeech-server: синтез ESpeech (F5-TTS + RUAccent) для stream-tts, HTTP API как у cosyvoice-server ([docs/espeech.md](docs/espeech.md)); вторая модель stream-tts (`streamtts/engines/espeech`) | да, кроме `.venv` |
| `stream-tts/streamtts/` | stream-tts на :8090: озвучка наград Twitch, админка, оверлей OBS (спецификация [docs/stream-tts.md](docs/stream-tts.md), развёртывание `stream-tts-deploy.md`, Windows `stream-tts-windows.md`) | да |
| `packaging/windows/` | программа (Nuitka) и установщик (Inno Setup 6.5+) для Windows; CI — `.github/workflows/windows.yml`; `stubs/*.def` — DLL-заглушки torch ESpeech | да, кроме `.venv`, `.inno`, `build/`, `dist/` |
| `packaging/pin-espeech.py`, `packaging/linux/` | загрузки ESpeech для обеих систем в `downloads.json`; заглушки .so torch для Docker (`stubs/**/*.map`, `build-stubs.sh`) | да |
| `scripts/` | `download-model.sh`; `import-voices.sh` — голоса из `voice-refs/cosyvoice/<набор>/<голос>.wav + .txt` в `voices/*.gguf` | да |
| `tools/voices/`, `tools/wc3/`, `tools/sources/` | подбор образцов: общий (`pick.py`, `synth.py`, оценки, коллекции для импорта `pack-sets.py`), реплики WC3, прочие источники ([docs/voice-pipeline.md](docs/voice-pipeline.md), [docs/voices-wc3.md](docs/voices-wc3.md)) | да |
| `tools/espeech/` | папка модели ESpeech из кеша Hugging Face (`prepare.py`, сборка — `espeech/prepare.py`) голоса `.espeech` (`make-voice.py`), обучение LoRA (`train.py`) | да |
| `effects/` | эффекты поверх синтеза (`post`) | да |
| `tests/e2e/` | e2e-тесты stream-tts на поддельном cosyvoice-server (раздел «Тесты» в `docs/development.md`) | да |
| `tests/espeech/` | тесты espeech-server: нарезка без модели, сервер и обучение LoRA с настоящей моделью | да |
| `voice-clips/<модель>/<набор>/` | исходные данные голосов: `cosyvoice/` — варианты образцов, синтез, оценки и выбор; `espeech/` — реплики для LoRA, адаптеры и голоса `.espeech` по голосам | нет (тексты реплик из игр) |
| `voice-refs/cosyvoice/<набор>/` | итоговые образцы CosyVoice и `manifest.json` | нет |
| `voice-sets/<модель>/<набор>.zip` | коллекции для импорта в stream-tts, по одной на «модель + набор» (`tools/voices/pack-sets.py`) | нет |
| `voices/`, `models/`, `voice-origs/`, `samples/`, `stream-tts-data/`, `runtime/`, `espeech-runtime/`, `.env` | данные, секреты, нативный cosyvoice-server для Windows, Python с torch для ESpeech в Docker | нет |

Данные голосов — `<модель>/<набор>/<голос>`: модель — `cosyvoice` или `espeech` (id из `streamtts/engines/`), набор —
источник: `wc3` (`wc3-*`), `morrowind` (`mw-*`, только ESpeech), игры по одной (`dota2`, `portal`, `stalker`,
`minecraft`, `neco-arc`, `altushka`), `streamers`; каталог — [docs/voices.md](docs/voices.md).

## Связи, которые легко порвать

- Скрипты `tools/` считают корень как `ROOT = dirname(dirname(HERE))` и грузят соседние модули по пути
  (`spec_from_file_location`): перед переносом или переименованием — `grep spec_from_file_location`.
- Имя набора — имя папки источника в `voice-clips/<модель>/`, `voice-refs/cosyvoice/`, `voice-sets/<модель>/<набор>.zip`
  и установленного набора `voices/<набор>/`; по нему stream-tts группирует страницу голосов (`GROUPS` в
  `streamtts/voices.py`). Голоса обеих моделей одного набора stream-tts держит в одной папке `voices/<набор>/`, а
  коллекцию из zip ставит и удаляет по модели (`install.py`). Инструменты знают свою модель (`pick.py` —
  `voice-clips/cosyvoice`, `lora-clips.py` — `voice-clips/espeech/wc3`); кеши `voice-clips/*.json` общие, их ключи —
  пути от корня: после переноса папок их надо переписать.
- `post` в `alts.json` и `manifest.json` — `[скрипт, аргументы…]`, путь от корня проекта. stream-tts ищет скрипт в
  `STREAM_TTS_PROJECT` (в контейнере `/project`), грузит через importlib и вызывает `apply(звук, аргументы)`;
  командная строка эффекта нужна `synth.py`.
- Модели синтеза — `streamtts/engines/<id>/` (описание, запуск, клиент), одна единовременно (`settings.engine`).
  Ядро зовёт у клиента только lane/synth/stream/streams/stop/speakers/ok/status; поле запроса модели — в `params()`
  её клиента, особенности редактора голоса — в `Maker` (`engines/<id>/editor.py`, ключ варианта CosyVoice не менять:
  пропадёт кэш черновиков), словарь ударений хранит сервер модели (`/accents`), настройка модели — схемой `params`
  в `/status` сервера (`SETTINGS` клиента), новый запрос к серверу — и в `fake_cosyvoice.py` (оба режима). Голос без файла для текущей модели (`.gguf`/`.espeech`) зрителям не выдаётся,
  но `users.voice` не перезаписывается; примеры и `calib_db` — свои у каждой модели (`voices.py`).
- `engine.chat` принимает событие в форме EventSub `channel.chat.message`; режим «Только чтение чата»
  (`chat_irc.py`) собирает такое же из тегов IRC: новое поле события — в обоих местах.
- Порты: cosyvoice 8080 (в Docker — сервер внутри контейнера stream-tts), stream-tts 8090, `review.py` 8765, `pick.py` 8766, `emotion-eval.py` 8767, espeech-server 8094; из исходников
  `stream-tts` — 8092 (его cosyvoice-server — на свободном порту), `stream-tts-en` (английский интерфейс без
  модели) — 8093, `stream-tts-espeech` — 8095 (свои данные, голоса и словарь в `stream-tts-data/espeech-test/`),
  `stream-tts-espeech-dl` — 8097 (загрузка ESpeech как у программы для Windows, всё в
  `stream-tts-data/espeech-dl/`), `streamtts-exe-test` — собранный exe на 8098 с данными в `stream-tts-data/exe-test/`, `stream-tts-ui-edit` (режим
  правки строк интерфейса на поддельном сервере, `stream-tts/ui-edit.py`) — 8099.
  Встроенный espeech-server (выбор ESpeech) — на свободном порту или `ESPEECH_PORT`. Конфигурации — `.claude/launch.json`.

## Запуск

- Стек: `docker compose up -d --build` (`.env` уже настроен на GPU) — stream-tts со встроенным сервером; после правок —
  `--build stream-tts`. Только сервер — `docker compose up -d cosyvoice` (профиль `server-only`, stream-tts сначала
  остановить: оба занимают 8080 и видеокарту). Сервисы могут быть в работе (стрим): перезапуск контейнеров
  согласовывать с пользователем.
- Инструменты — из корня проекта: `python tools/wc3/review.py b02`. `review.py` и `pick.py` — только стандартная
  библиотека; остальным (numpy, onnxruntime, faster-whisper, torch cu124 и др.) нужен `.venv/Scripts/python.exe`
  (3.11), cuBLAS/cuDNN для faster-whisper — из `torch/lib`. Везде нужны `ffmpeg`/`ffprobe` в PATH; звук в
  пайплайне — 24 кГц моно.
- stream-tts без Docker: `stream-tts/.venv` (3.14), конфигурация `stream-tts` (порт 8092, данные в
  `stream-tts-data/native/`).
- espeech-server: `espeech/.venv` (3.14, `espeech/requirements.txt`), конфигурация `espeech` —
  `python -m espeech --models models/espeech --voices voices`; папка модели — `tools/espeech/prepare.py`. Занимает
  ~0,8 ГБ видеопамяти рядом с cosyvoice-server: видеокарта общая, замеры скорости — чередующимися прогонами.
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
- cosyvoice-server закрывает простаивающее keep-alive соединение через 5 с: клиент stream-tts держит их не дольше
  2 с (`KEEPALIVE` в `engines/client.py`), иначе фраза после паузы падает с «Server disconnected».
- ONNX-фронтенд (нужен для `POST /speaker?type=extract`) сервер держит выгруженным: флаги дают только пути
  (`GET /frontend/model`). Кто извлекает голос, сначала грузит его (`POST /frontend/model/load` с этими путями), как
  `import-voices.sh`, `synth.py` и `editor.py`; stream-tts выгружает его после 10 минут простоя.
- Повторная регистрация имени даёт 409: сначала `DELETE /speaker/<имя>` (так делает `import-voices.sh --force`).
  `POST /model/load` ждёт поле `model`; после выгрузки модели голоса сбрасываются.
- Не убирать `GGML_CUDA_DISABLE_GRAPHS=1` (образ, `engines/cosyvoice/launch.py`) и `--llm-flash-attn 0` (`launch.py`,
  `docker-compose.gpu.yml` для `server-only`): без них на GPU тихий шум или мусор вместо речи, причём без ошибок в
  логах.
- Замеры скорости — по `elapsed_ms` из `docker compose logs stream-tts` (вывод сервера — логгер `cosyvoice`) или
  через `127.0.0.1`: `localhost` из Python на Windows добавляет ~2 с (сначала пробует IPv6).
- stream-tts всегда сам запускает сервер выбранной модели, указать ему готовый сервер нельзя (только тестам —
  `STREAM_TTS_TEST_SERVERS`, в документацию для пользователя не писать): в Docker — из образа (`/opt/cosyvoice`), из
  исходников — `runtime/cosyvoice-server.exe`, и тогда это второй сервер на той же видеокарте (1,5–1,9 ГБ
  видеопамяти на Q8_0).
- Встроенный сервер останавливается, пока ставится или удаляется набор голосов, и перезапускается при выгрузке
  модели, смене модели или устройства и проверке скорости: `import-voices.sh` и `synth.py` в это время получат
  обрыв соединения. Сохранение голоса в редакторе сервер не останавливает.
- Обучение LoRA ESpeech и его несохранённые результаты живут в процессе espeech-server: новое действие, которое
  перезапускает или останавливает встроенный сервер, — через `_restart_guard` в `web.py` (во время обучения отказ,
  с несохранёнными результатами — вопрос `confirm`), иначе оно молча выбросит до 15 минут обучения.
- Имя голоса у встроенного сервера — имя файла (`voices/<имя>.gguf` или `voices/<набор>/<имя>.gguf`, один уровень
  подпапок); `entrypoint.sh` профиля `server-only` склеивает путь через `_` (`wc3/thrall.gguf` → `wc3_thrall`).
- `MODEL` закрепляет модель и отключает выбор в админке, поэтому в stream-tts не передаётся; он нужен только
  профилю `server-only`. Выбор модели stream-tts хранит в `settings.json` (`model`), при нескольких `*.gguf` без
  выбора сервер не запускается. В `/opt/cosyvoice` свой `libonnxruntime.so`: `LD_LIBRARY_PATH` в образе stream-tts
  не задавать, путь серверу добавляет `engines/cosyvoice/launch.py`.
- Nuitka: собранную программу `paths.py` узнаёт по `__compiled__` (`sys.frozen` не задаётся); данные пакета и
  `effects/` лежат рядом с exe. Импорты эффектов Nuitka не видит: новые зависимости — в `--include-module` в
  `build-exe.py`.
- Установщик ничего не качает: модель, CUDA и ESpeech — кнопки на вкладке «Сервер». CUDA — два архива из релиза
  llama.cpp той же сборки, что ядро GGML (`cuda.archives` в `downloads.json`; `ggml-cuda.dll` должен совпадать с
  `ggml-base.dll`). Новая сборка llama.cpp — `LLAMA`/`ASSETS` в `build-runtime.py` и `cuda` в `downloads.json`
  вместе. Строка в `[Code]` `StreamTTS.iss` не должна начинаться с `[` (Inno примет её за заголовок секции).
- Версия программы — только `__version__` в `streamtts/__init__.py`. Релиз — аннотированный тег `v<версия>`: CI
  собирает установщик и публикует релиз с `latest.json`, по которому программа ищет обновления (`REPO` в
  `updates.py` должен совпадать с репозиторием, иначе CI падает на шаге «Версия»).
- В `voice-clips/` ручная работа пользователя (`ratings.txt`, `text-fixes.json`, `phrase-tags.json`, `picks.json`,
  `alts.json`, `emotion-eval.json`), в git её нет: без нужды не перезаписывать, перед массовой правкой — копия в scratchpad.
- Переименование голоса: зарегистрировать новое имя, старое выгрузить (`DELETE /speaker/<имя>`) и удалить
  `voices/<имя>.gguf`, иначе вернётся после перезапуска. Настройки и выбор зрителей (`users.voice`) в stream-tts
  остаются на старом имени — такие зрители получат голос по хешу ника.
- Имена в русской локализации WC3 2002 года звучат по-старому (Артес, Тиренда, Малганус, Кел-Тузед, Мэв, Фарион,
  Маннорох) — и в расшифровках, и в тексте для синтеза.
- Модель эмоций (`emotions.py`) — CC BY-NC-SA 4.0: только для разметки образцов, не для синтеза.
- Голоса реальных людей (***, ***, ***) — только для шуток на стриме. Предпочтительны оригинальные
  записи; примеры JeetBot — запасной вариант, это уже синтез.
- `voice-origs/` (ассеты игр, записи с YouTube) не попадает ни в git, ни в образы.
- espeech-server: всё, что трогает модель, — в его рабочем потоке (`Engine.call`/`stream`): torch в потоках запросов
  копит память (~2 МБ за запрос). Модель — `requires_grad_(False)`, копии адаптеров LoRA — под `no_grad`, иначе
  история autograd растёт на каждой смене голоса. `model.py` совпадает с f5-tts бит в бит — после правки сверять
  (эталон — облегчённый f5-tts в `samples/espeech-lean`). `RUAccent.load` не вызывать: лезет в сеть и грузит
  RuleEngine (~1 ГБ, разметке не нужен). Обучение LoRA (`espeech/train.py`) — на той же модели: обучающий forward
  свой (в `model.py` не переносить), fp32-адаптеры — в `LoRALinear.fit` только на время шага, база не размораживается.
  В сервере (`trainjob.py`) каждый шаг — отдельное задание рабочего потока (синтез — между шагами), результаты — только
  в памяти процесса; подготовка реплик и запись адаптера — общие с CLI (`pairs.py`, `voices.save_adapter`).
- Веса ESpeech помечены Apache-2.0, но похожи на дообучение F5-TTS (CC-BY-NC-4.0): в установщик их не класть —
  программа качает их по кнопке пользователя из репозитория авторов (docs/espeech.md, «Лицензии»). Английский ESpeech
  не читает (сильный акцент).
- ESpeech у программы для Windows и в Docker качается кнопкой одним кодом (docs/espeech.md, «Поставка»): Python,
  пакеты, часть колеса torch — свои у каждой системы (`espeech.runtime.windows|linux` в `downloads.json`), исходники
  модели — общие. Сменил версии в `espeech/requirements.txt` или источники модели в `espeech/prepare.py` (`SOURCES`) —
  `packaging/pin-espeech.py` (пинит обе системы и пишет списки заглушек) и новый `version.txt` рантайма у
  пользователей (качается заново). torch там без cuDNN, cuSPARSE, cuSOLVER, NVRTC, cuFFT (на Linux ещё NCCL, cuPTI,
  cuRAND, cuSPARSELt, cuFile, NVSHMEM), cuBLAS и cudart — настоящие из CUDA cosyvoice-server, остальное — заглушки
  (`packaging/windows/stubs/*.def`, `packaging/linux/stubs/**/*.map`, в образе — стадия `espeech-stubs`): новый вызов
  этих библиотек в espeech-server (свёртка с cuDNN, FFT на GPU) сломает программу и Docker, хотя из `espeech/.venv`
  всё работает. Пакет `espeech` импортируется раньше torch: он подключает библиотеки (`ESPEECH_DLL_DIRS`; на Linux —
  загрузкой с `RTLD_GLOBAL`, `LD_LIBRARY_PATH` не задавать). Рантайм в Docker — том `./espeech-runtime`
  (`ESPEECH_RUNTIME=/espeech-runtime/python`: сборка идёт рядом, в `python.tmp`, и переименовывается).

## Соглашения

- Язык проекта — русский: документация, комментарии, интерфейс, коммиты, ответы пользователю.
- Английский интерфейс stream-tts (`i18n.py`): строки для пользователя пишутся по-русски через `tr("…")` в Python и
  `T("…")` в `admin.js`, разметка переводится сама (абзац со ссылкой или `<code>` — с `data-t`), перевод — в
  `static/en.json`. Словарь проверяет `tests/e2e` (`check-i18n.py` в `test_invariants.py`), строки разметки —
  `[...I18N_MISSING]` в консоли админки на `stream-tts-en`. Логи (`log.*`) не переводятся.
- Коммит: «область: что сделано» (`stream-tts: короткие фразы целиком…`), в теле — зачем и что проверено.
- Инструменты — самостоятельные скрипты с docstring в начале (назначение, `usage:`, входы и выходы). При изменении
  поведения обновлять docstring и документ в `docs/`.
- Комментарии короткие и объясняют «почему»: замеры, найденные баги, причины выбора значений.
- Shell-скрипты, `Dockerfile` и `*.yml` — с LF (`.gitattributes`); скрипты сами делают `cd` в корень проекта.
- Новые и перенесённые файлы — в дерево [docs/development.md](docs/development.md); новая папка — ещё и в таблицу выше.

## Проверка

Минимум после правок:
- stream-tts: `stream-tts/.venv/Scripts/python -m pytest tests/e2e` (~1 мин, без видеокарты и модели; свой процесс
  на свободном порту, Docker-версию не трогает). Новое поведение очереди, фильтров, синтеза, голосов, редактора,
  статистики, доступа — сценарием в `tests/e2e/`; новый запрос к cosyvoice-server — и в `fake_cosyvoice.py`, новый
  тег IRC — в `fake_irc.py`. После правок синтеза, `runtime.py`, `engines/` или обновления cosyvoice.cpp — ещё `-m real`
  (настоящая модель на видеокарте и CPU, распознавание faster-whisper; при нехватке видеопамяти пропускается).
- espeech-server: `espeech/.venv/Scripts/python -m pytest tests/espeech` (~1,5 мин, настоящая модель; без
  `models/espeech` пропускается).
- Синтаксис всех `.py`, словарь интерфейса, `docker compose config`, флаги GPU, `KEEPALIVE`, единственный
  `__version__`, архив `pack-handoff.py` — `tests/e2e/test_invariants.py` (входит в прогон `tests/e2e`). Что роуты
  админки не перезапускают сервер во время обучения LoRA — `test_every_route_guarded` в `test_train.py`: новый POST
  без группы в нём роняет тест.
- Страницы инструментов и stream-tts — через preview (`.claude/launch.json`) с проверкой консоли и запросов; образ —
  `docker compose build <сервис>`.
- Голоса и эффекты — тестовая фраза через `POST /tts`; оценку на слух делает пользователь.
- То, что надо проверять регулярно, — тестом (обычно в `tests/e2e/`), а не пунктом «проверить вручную» в документации.
