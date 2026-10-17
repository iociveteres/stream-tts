# Local Voice Gen: cosyvoice-server в Docker

Локальный синтез речи с клонированием голоса на базе [cosyvoice.cpp](https://github.com/Lourdle/cosyvoice.cpp)
(C++/GGML-порт CosyVoice3) и модели [Fun-CosyVoice3-0.5B-2512-GGUF](https://huggingface.co/Lourdle/Fun-CosyVoice3-0.5B-2512-GGUF).

- Работает на CPU, а на видеокарте NVIDIA (`./setup.sh --gpu`) примерно в 15 раз быстрее.
- WebUI в браузере и HTTP API.
- Клонирование голоса по короткой записи и управление стилем текстовой инструкцией.
- Модель не вшита в образ, а подключается из папки `./models`.
- Всё поднимается одной командой `./setup.sh`.

## Требования

| | |
|---|---|
| Процессор | x86_64 с **AVX2** (Intel с 4-го поколения, AMD Zen и новее) |
| ОС | Linux или Windows 10/11 с Docker Desktop (бэкенд WSL2) |
| Docker | Docker Engine с Compose v2 (`docker compose`) |
| Shell | bash: Linux, WSL2 или Git Bash на Windows |
| RAM | около 4 ГБ свободной памяти для Q8_0 |
| Диск | около 1,6 ГБ (образ 378 МБ, модель 944 МБ, фронтенд 250 МБ); с GPU образ весит 1,14 ГБ |
| GPU (необязательно) | NVIDIA с драйвером для CUDA 13 (R580+), около 1 ГБ видеопамяти; Docker с доступом к GPU (Docker Desktop с WSL2 или NVIDIA Container Toolkit) |

macOS на Apple Silicon и ARM-Linux не поддерживаются: бинарники собраны под x86_64, а при эмуляции нет AVX2.

## Быстрый старт

```bash
./setup.sh
```

Скрипт выполняет пять шагов:
1. Проверяет, что Docker установлен и запущен.
2. Скачивает модель (по умолчанию `Q8_0`) и ONNX-фронтенд для клонирования в `./models`. Если файлы уже есть, пропускает их.
3. Записывает выбранную модель в `.env`.
4. Собирает образ и запускает контейнер.
5. Ждёт, пока сервер ответит, и выводит адрес.

После этого WebUI доступен по адресу **http://localhost:8080**.

Параметры:

```bash
./setup.sh Q6_K                  # другой квант
./setup.sh Q8_0 --frontend full  # полный ONNX-фронтенд (~1 ГБ) вместо int8
./setup.sh Q8_0 --frontend none  # без фронтенда (клонирование из аудио будет недоступно)
PORT=9000 ./setup.sh             # другой порт на хосте
./setup.sh Q8_0 --gpu            # на видеокарте NVIDIA (CUDA)
```

С `--gpu` скрипт проверяет, что Docker видит видеокарту, и прописывает в `.env` оба compose-файла
(`COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml`). Поэтому обычные `docker compose up/down/logs`
дальше работают с GPU-вариантом. Чтобы вернуться на CPU, удалите из `.env` строки `COMPOSE_FILE` и
`COMPOSE_PATH_SEPARATOR` (или запустите `./setup.sh` без `--gpu`) и выполните `docker compose up -d`.

Управление контейнером:

```bash
docker compose logs -f   # логи
docker compose down      # остановить
docker compose up -d     # запустить снова (модель берётся из .env)
```

### Windows

Docker Desktop должен быть запущен. Скрипт выполняется из Git Bash или из WSL2-дистрибутива, из PowerShell напрямую не запустится.
Если работаете в WSL, держите проект в домашней папке WSL (`~/...`), а не на `/mnt/c/...`: модель будет загружаться заметно быстрее.

## Модели

Список квантов с размерами:

```bash
scripts/download-model.sh --list
```

| Квант | Размер | Комментарий автора модели |
|---|---|---|
| `Q8_0` | 944 МБ | **по умолчанию**, почти без потерь |
| `Q6_K` | 840 МБ | хорошее качество |
| `Q5_K_M` / `Q5_1` | ~683 МБ | приемлемо |
| `Q4_K_M` | 629 МБ | иногда звук приглушённый |
| `Q3_*`, `Q2_*` | < 530 МБ | заметные артефакты |

Если Hugging Face недоступен, файлы можно скачать вручную и положить так:

```
models/
├── CosyVoice3-2512_Q8_0.gguf
└── frontend-onnx/
    ├── campplus.int8.onnx            (или campplus.onnx)
    └── speech_tokenizer_v3.int8.onnx (или speech_tokenizer_v3.onnx)
```

Если в `models/` лежит несколько `.gguf`, нужная модель задаётся переменной `MODEL` в `.env`.

## Использование

### Клонирование голоса (WebUI)

1. Откройте вкладку **Extract**.
2. Загрузите аудио (5–15 секунд чистой речи без музыки и шума) или запишите его с микрофона.
3. Введите **точную расшифровку** того, что сказано на записи, и задайте имя голоса.
4. Голос появится в списке спикеров, и им можно озвучивать любой текст.

Чтобы голос пережил перезапуск контейнера, сохраните его через **Save speaker** по пути `/voices/<имя>.gguf`.
Все файлы из `./voices` подключаются автоматически при старте под именами файлов.
Подпапки тоже просматриваются, а слэши в пути заменяются на `_`: `voices/wc3/orc/thrall.gguf` станет голосом
`wc3_orc_thrall`. Если два файла дают одно имя (`wc3/arthas.gguf` и `wc3_arthas.gguf`), подключается первый
по алфавиту, а второй пропускается с предупреждением в логе. В **Save speaker** можно указать путь в подпапке,
например `/voices/wc3/orc/thrall.gguf`; саму подпапку создайте заранее.

### Режимы синтеза

| Режим | Что делает |
|---|---|
| **zero-shot** | Озвучивает текст голосом и в манере образца. Чтобы получить нужную интонацию, дайте образец, где она уже есть. |
| **instruct** | Тот же голос плюс текстовая инструкция к стилю («говори шёпотом, медленно»). Инструкции на английском или китайском, вероятно, работают надёжнее русских. |
| **cross-lingual** | Голос образца на другом языке. |

### HTTP API

Регистрация голоса из аудио:

```bash
curl -X POST "http://localhost:8080/speaker?type=extract" \
  -F name=myvoice \
  -F "text=Точная расшифровка записи." \
  -F "audio=@sample.wav"
```

Синтез:

```bash
curl -X POST http://localhost:8080/tts \
  -H 'Content-Type: application/json' \
  -d '{"text":"Привет!","voice":"myvoice","mode":"zero-shot","response_format":"wav"}' \
  -o out.wav
```

С инструкцией к стилю: `"mode":"instruct","instruction":"Speak in a slow, mysterious whisper."`.

Сохранение голоса на диск:

```bash
curl -X POST http://localhost:8080/speaker/save \
  -H 'Content-Type: application/json' \
  -d '{"name":"myvoice","path":"/voices/myvoice.gguf"}'
```

Состояние сервера: `GET /status`, `GET /speaker`, `GET /formats`. Полный справочник API есть в
[docs/TOOLS.md](https://github.com/Lourdle/cosyvoice.cpp/blob/v0.2.0/docs/TOOLS.md) проекта cosyvoice.cpp.
Учтите, что `POST /model/load` на самом деле ждёт поле `model`, а не `model_path`, как написано там.
Кроме того, после выгрузки модели все зарегистрированные голоса сбрасываются.

Форматы вывода: `wav`, `mp3`, `opus`, `flac`, `aac`, `m4a`, `pcm`.

### Готовые голоса

В `voices/` лежат голоса, собранные в этом проекте. Они подключаются при старте, как любые голоса из `./voices`.
Каталог: [docs/voices.md](docs/voices.md).

- **Warcraft III (64 голоса `wc3-*`)** из оригинального русского дубляжа (RoC + TFT, 2002–2003): персонажи кампаний
  (`wc3-arthas`, `wc3-thrall`, `wc3-jaina`, `wc3-illidan`, `wc3-narrator`…) и юниты (`wc3-peasant`, `wc3-footman`,
  `wc3-peon`…). Полный список и откуда взяты реплики: [docs/voices-wc3.md](docs/voices-wc3.md).
- **Другие игры**: `dota2-pudge`, `portal-glados`, `stalker-bandit`, `villager`, `neco-arc`, `neco-arc-angry`,
  `neco-arc-ja`, `***`.
- **Стримеры**: `***`, `***`. Голоса реальных людей только для шуток на стриме: не выдавайте синтез за
  их настоящие слова.

```bash
curl -X POST http://localhost:8080/tts -H 'Content-Type: application/json'   -d '{"text":"Работа никогда не заканчивается.","voice":"wc3-thrall","mode":"zero-shot","response_format":"wav"}'   -o out.wav
```

Как подбираются образцы (варианты, синтез тестовых фраз, оценки и модель эмоций, выбор на слух на локальной
странице, установка): [docs/voice-pipeline.md](docs/voice-pipeline.md).

Свой набор голосов: положите пары `<имя>.wav` + `<имя>.txt` (точная расшифровка) в папку и выполните
`scripts/import-voices.sh <папка>`. С `--force` скрипт пересобирает уже сохранённые голоса (все или только
перечисленные после флага): сначала выгружает голос из памяти сервера, иначе тот отвечает 409 на повторное имя.

### Озвучка наград Twitch (`stream-tts`)

Сервис рядом с cosyvoice: зритель активирует награду за баллы канала, вводит текст, и тот звучит на стриме
выбранным голосом (как в JeetBot TTS). Полная спецификация: [docs/stream-tts.md](docs/stream-tts.md).

```bash
docker compose up -d --build stream-tts   # вместе с cosyvoice: docker compose up -d --build
```

Интерфейс: **http://localhost:8090**. Дальше всё в нём:
1. **Twitch** → гайд по шагам: 2FA, своё приложение на dev.twitch.tv (Client Type: **Public**), Client ID.
   Вход по коду на twitch.tv/activate, канал берётся из токена. Затем «Создать награду».
2. **Очередь** → адрес оверлея `http://localhost:8090/overlay`: OBS → Источник → Браузер.
   В обычной вкладке нужен клик «Включить звук». Там же тестовая активация (весь путь, кроме Twitch).
3. **Голоса** → включение, поправка громкости, «послушать». Страница для зрителей: `/voices/`, zip для хостинга: `/voices.zip`.

Команды в чате: `!ttssf <голос>` (зритель выбирает голос), для модеров и стримера `!ttsskip`, `!ttsclear`,
`!ttsban <ник>`, `!ttsunban <ник>`, `!ttson`, `!ttsoff`.

Переменные `.env` (все необязательные):

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `STREAM_TTS_PORT` | `8090` | порт интерфейса и оверлея |
| `STREAM_TTS_BIND` | `127.0.0.1` | `0.0.0.0`, чтобы OBS на другом ПК дотянулся по сети |
| `STREAM_TTS_PASSWORD` | — | пароль админки; без него сервис с `BIND` не на localhost не стартует |
| `COSYVOICE_URL` | `http://cosyvoice:8080` | синтез на другой машине |
| `COSYVOICE_API_KEY` | — | ключ, если cosyvoice запущен с `--api-key` |
| `TWITCH_CLIENT_ID` | — | можно задать и в интерфейсе |

Данные лежат в `./stream-tts-data` (в git не попадает): `settings.json`, `token.json`, `stream-tts.sqlite`
(история за 30 дней), `profanity.txt` (корни мата, правится и из интерфейса), `demos/` (mp3-примеры голосов).

Как устроено:
- К cosyvoice идёт строго один запрос за раз: живая очередь, затем «послушать», затем фоновые примеры голосов.
  Активация останавливает фоновый синтез примера (`/tts/stop`), пример досинтезируется позже.
  Следующая фраза синтезируется, пока играет текущая.
- Громкость: без стриминга фраза доводится до −16 LUFS (BS.1770, numpy) с ограничителем пиков; со стримингом
  усиление голоса калибруется по его примеру. Поверх — ручная поправка ±дБ.
- Мат вырезается, фраза синтезируется кусками, а на месте мата звучит писк 800 Гц на ~9 дБ тише речи.
- Длина фразы: не больше 1,8× расчётной (12 символов в секунду) и не больше 60 с. `max_token_text_ratio`
  тоже передаётся, но в cosyvoice.cpp v0.2.0 на `/tts` он длину не меняет (проверено: 0,5–20 дают одно и то же).
- Стриминг дороже синтеза целиком: каждый кусок заново прогоняет flow и вокодер. Чем мельче кусок, тем медленнее.
  Замер на RTX 3060 Ti, фраза ~25 с:

  | Кусок, токенов | Первый звук | Синтез / длина звука |
  |---|---|---|
  | 28 (умолчание cosyvoice.cpp) | ~1 с | 0,9–1,4× |
  | 50 | 1–1,3 с | 0,7–0,8× |
  | 150 (**по умолчанию здесь**) | 1,6–2,1 с | ~0,4× |
  | 300 | 2,7–3,6 с | ~0,33× |
  | целиком | 5–6 с | 0,22–0,28× |

  Поэтому стримятся только фразы длиннее полутора кусков (при 150 токенах это ~108 символов), короткие идут
  целиком: первый звук у них всё равно пришёл бы почти одновременно с последним. Размер куска меняется в
  «Синтез → Дополнительно». `chunk_tokens` передаётся в каждом запросе: сервер запоминает значение из прошлого
  запроса и сам его не сбрасывает. На CPU стриминг заикается, интерфейс об этом предупреждает.
- Уровень доступа (сабы, VIP, модеры) проверяется по бейджам из сообщений чата: зритель должен хоть раз
  написать в чат.
- При остановке награда ставится на паузу, при старте снимается; активации, оставшиеся `UNFULFILLED`, возвращаются.

Разработка без Docker: `cd stream-tts && python -m venv .venv && .venv/Scripts/pip install -r requirements.txt`,
затем `COSYVOICE_URL=http://localhost:8080 .venv/Scripts/python -m streamtts` (или конфигурация `stream-tts`
в `.claude/launch.json`). Без `COSYVOICE_URL` stream-tts сам запускает нативный cosyvoice-server из `runtime/` в
корне проекта (режим managed, [docs/stream-tts.md](docs/stream-tts.md#развёртывание)) с моделью из `models/` и
голосами из `voices/`; конфигурация `stream-tts-managed` — на порту 8092 и с данными в `stream-tts-data/native/`,
чтобы не мешать Docker-версии.

Программа для Windows без Docker и Python (установщик с рантаймом, модель качается из админки) собирается в
`packaging/windows/`: [docs/stream-tts.md](docs/stream-tts.md#сборка-для-windows).

### Перенос голосов между инстансами

Голос в `voices/<имя>.gguf` — самодостаточный файл размером около 120 КБ. В нём хранятся мел-спектрограмма образца,
эмбеддинг говорящего (CAM++), речевые токены и текст расшифровки, а весов модели нет. Поэтому:

- для переноса достаточно скопировать `.gguf` в папку `voices/` другого инстанса и перезапустить его
  (`docker compose restart`). ONNX-фронтенд там не нужен;
- файл работает с любым квантом CosyVoice3 (Q8_0, Q6_K и т.д.), но не с другими семействами моделей, например CosyVoice2;
- голоса, созданные через WebUI или API без **Save speaker**, живут только в памяти и пропадут после перезапуска.

Если формат голосов сменится в новой версии cosyvoice.cpp, их можно пересобрать из исходных образцов.
Для этого перенесите `voice-refs/` и выполните `scripts/import-voices.sh voice-refs/<набор>` для каждого набора
(`wc3`, `games`, `streamers`). Здесь фронтенд уже нужен.

## Производительность

Замер на Ryzen 5 5600G (6 ядер, 12 потоков) и RTX 3060 Ti 8 ГБ, квант Q8_0:

| Задача | Устройство | Длина аудио | Время генерации |
|---|---|---|---|
| zero-shot | CPU | 8,5 с | ~36 с |
| instruct | CPU | 11,1 с | ~54 с |
| zero-shot | GPU | 6,7 с | ~2,1 с |
| instruct | GPU | 8,3 с | ~2,1 с |

На CPU генерация идёт примерно в 4 раза медленнее реального времени. Такой скорости хватает для подготовки озвучки, но не для живого диалога.
На GPU она в 3–4 раза быстрее реального времени, а короткая фраза занимает 1,5–2,5 секунды.
Ограничение до 6 потоков на CPU (по числу физических ядер) работало медленнее, поэтому используется значение по умолчанию, то есть все потоки.
Чтобы звук начинал играть быстрее, включите в WebUI потоковый режим (Streaming) с форматом MP3 или Opus.

## Устройство

```
.
├── setup.sh               # единая точка входа: модель, .env, сборка и запуск
├── docker-compose.yml     # cosyvoice + stream-tts
├── docker-compose.gpu.yml # надстройка для видеокарты NVIDIA
├── AGENTS.md              # заметки для ИИ-агентов (CLAUDE.md ссылается на него)
├── cosyvoice/             # образ cosyvoice-server
│   ├── Dockerfile         # двухстадийная сборка из готовых релизов (GPU=cuda — с CUDA-бэкендом)
│   └── entrypoint.sh      # собирает аргументы сервера из /models и /voices
├── stream-tts/            # озвучка наград Twitch (docs/stream-tts.md), второй сервис в docker-compose.yml
│   └── streamtts/         # пакет: paths.py (пути), runtime.py (встроенный сервер), install.py (модель и наборы
│                          # голосов из админки), desktop.py (точка входа программы для Windows), static/
├── packaging/windows/     # программа и установщик для Windows: build-runtime.py, build-exe.py (Nuitka),
│                          # StreamTTS.iss (Inno Setup); сборка в CI — .github/workflows/windows.yml
├── scripts/
│   ├── download-model.sh  # загрузка модели и фронтенда с докачкой
│   └── import-voices.sh   # регистрация голосов из пар .wav + .txt и сохранение в voices/
├── tools/                 # подбор образцов голосов (docs/voice-pipeline.md), запуск из корня проекта
│   ├── voices/            # общее: страница выбора, синтез и оценки, модель эмоций и её сверка на слух, CAM++
│   ├── wc3/               # реплики Warcraft III: нарезка кандидатов, проверка текстов (docs/voices-wc3.md)
│   └── sources/           # реплики из вики, файлов игр и видео, отбор по голосу, сборка вариантов
├── effects/               # эффекты поверх синтеза (post в manifest.json), их запускает и stream-tts
├── docs/                  # voice-pipeline.md, voices.md, voices-wc3.md, stream-tts.md
│
│   Данные (в git попадают только тексты и json):
├── models/                # модели (volume, только чтение)
├── voices/                # сохранённые голоса .gguf (volume)
├── voice-refs/            # итоговые образцы голосов (.wav + .txt) по наборам: wc3, games, streamers
├── voice-clips/           # варианты образцов, их синтез, оценки и выбор по наборам
├── voice-origs/           # сырые звуковые паки
├── samples/               # тестовые аудио
├── runtime/               # нативный cosyvoice-server с DLL и ffmpeg для режима managed (Windows)
└── stream-tts-data/       # настройки, токены и история stream-tts (volume)
```

**Образ** собирается на базе `ubuntu:26.04`: релизы cosyvoice.cpp собраны там и требуют glibc 2.43 и ICU 78.
- Стадия `fetch` скачивает:
  - архив cosyvoice.cpp (`cosyvoice-server`, `cosyvoice-cli`, `quantize`, `libcosyvoice`, ONNX Runtime);
  - все `libggml*.so*` из релиза llama.cpp.
- Итоговый образ содержит только рантайм-библиотеки (ICU, libgomp, FFmpeg `libav*`), без curl и компиляторов.

**GGML** в релизах cosyvoice.cpp не поставляется, его берут из llama.cpp. Копируются все варианты
`libggml-cpu-<isa>.so`, и сервер сам выбирает лучший для текущего процессора (на Zen 3 это `haswell`).

**GPU** (`GPU=cuda`, образ `cosyvoice-server:cuda`) берёт GGML из CUDA-сборки того же релиза llama.cpp
(`libggml-cuda.so`) и рантайм CUDA 13 (cudart, cuBLAS) из архива `cudart-llama-*`. Без двух настроек
CUDA-бэкенд выдаёт плохой звук, и обе уже заданы:
- `GGML_CUDA_DISABLE_GRAPHS=1` (в образе). С включёнными CUDA Graphs звук тихий (около −36 дБ вместо −17)
  и шумный, а с flash attention в LLM вместо речи получается мусор. Ошибок в логах при этом нет. Это тот же
  эффект, что в [Lourdle/cosyvoice.cpp#15](https://github.com/Lourdle/cosyvoice.cpp/issues/15). GGML,
  собранный из исходников, без этой настройки ведёт себя так же, поэтому образ использует готовую сборку.
- `--llm-flash-attn 0` (в `docker-compose.gpu.yml`). С flash attention в LLM в начале фразы чаще появляются
  лишние звуки: 8 из 24 тестовых фраз против 4 из 24 без него и 3 из 24 на CPU с теми же seed. Flash attention
  во flow на качество не влияет и остаётся включённым.

**`cosyvoice/entrypoint.sh`** делает четыре вещи:
- проверяет поддержку AVX2;
- передаёт серверу `--model`: из `MODEL` или единственный `*.gguf` в `/models`;
- регистрирует голоса из `/voices/**/*.gguf` (путь в подпапках превращается в имя через `_`);
- после старта загружает ONNX-фронтенд через `POST /frontend/model/load`. Это нужно, потому что в режиме WebUI флаги `--speech-tokenizer` и `--campplus` фронтенд не загружают.

**Сеть:** порт пробрасывается только на `127.0.0.1`. Чтобы открыть доступ из локальной сети, уберите `127.0.0.1:` в
`docker-compose.yml` и задайте серверу `--api-key`.

### Обновление версий

Версии задаются аргументами в `cosyvoice/Dockerfile`:

```dockerfile
ARG COSYVOICE_TAG=v0.2.0
ARG COSYVOICE_ASSET=cosyvoice-67d45fe-linux-x86_64-ffmpeg.tgz
ARG LLAMACPP_TAG=b11500
```

Релиз llama.cpp лучше брать ближайший по дате к релизу cosyvoice.cpp: у GGML нет стабильного ABI.
После обновления проверьте, что в выводе появился `loaded CPU backend` (для GPU-образа ещё и `CUDA0`):

```bash
docker run --rm --entrypoint /opt/cosyvoice/cosyvoice-cli cosyvoice-server:cpu --list-backends
docker run --rm --gpus all --entrypoint /opt/cosyvoice/cosyvoice-cli cosyvoice-server:cuda --list-backends
```

После обновления версий на GPU стоит проверить, не исправлена ли ошибка с CUDA Graphs: уберите
`GGML_CUDA_DISABLE_GRAPHS` и сравните громкость и разборчивость одной фразы с CPU-образом.

## Решение проблем

| Симптом | Причина и решение |
|---|---|
| `Docker daemon не запущен` | Запустите Docker Desktop. |
| `CPU без AVX2` | Процессор не поддерживается рантаймом. |
| `bash\r: No such file or directory` | Скрипты сохранены с CRLF. Исправить: `sed -i 's/\r$//' *.sh`. Репозиторий защищён `.gitattributes`. |
| Извлечение голоса недоступно | Нет файлов в `models/frontend-onnx/`: `scripts/download-model.sh --frontend int8`. |
| Сервер не стартует, несколько моделей | Задайте `MODEL=/models/<файл>.gguf` в `.env`. |
| Модель не качается | Скачайте вручную с Hugging Face в `models/` (см. раздел «Модели»). |
| `Docker не видит GPU` | Обновите драйвер NVIDIA. В Docker Desktop включите бэкенд WSL2, на Linux поставьте NVIDIA Container Toolkit. Проверка: `docker run --rm --gpus all ubuntu:26.04 nvidia-smi`. |
| На GPU тихий шумный звук или мусор вместо речи | Нет `GGML_CUDA_DISABLE_GRAPHS=1` или включён `--llm-flash-attn` (см. «Устройство»). |
| В начале фразы лишний звук («точка», «пой») | Особенность модели, бывает и на CPU, примерно в 1 фразе из 8. Сгенерируйте заново с другим `seed`. |

## Лицензии

- cosyvoice.cpp: MIT.
- Модели CosyVoice3 GGUF: Apache-2.0.
- FFmpeg из репозиториев Ubuntu может включать GPL-компоненты. Это важно только при публикации образа.

Это неофициальная сборка: ни cosyvoice.cpp, ни этот проект не связаны с командой CosyVoice.
Клонируйте только свой голос или голоса людей, которые дали на это согласие.
