# Устройство и разработка

Для тех, кто меняет проект: дерево файлов, как собран образ cosyvoice-server, обновление версий, запуск stream-tts
из исходников. Как пользоваться — [README](../README.md); заметки для ИИ-агентов — [AGENTS.md](../AGENTS.md).

## Файлы

```
.
├── setup.sh               # единая точка входа: модель, .env, сборка и запуск
├── docker-compose.yml     # cosyvoice + stream-tts
├── docker-compose.gpu.yml # надстройка для видеокарты NVIDIA
├── AGENTS.md              # заметки для ИИ-агентов (CLAUDE.md ссылается на него)
├── LICENSE                # MIT — код проекта (что не входит — в README, «Лицензии»)
├── cosyvoice/             # образ cosyvoice-server
│   ├── Dockerfile         # двухстадийная сборка из готовых релизов (GPU=cuda — с CUDA-бэкендом)
│   └── entrypoint.sh      # собирает аргументы сервера из /models и /voices
├── stream-tts/            # озвучка наград Twitch (docs/stream-tts.md), второй сервис в docker-compose.yml
│   ├── check-i18n.py      # проверка английского словаря: строки из кода без перевода
│   └── streamtts/         # пакет: paths.py (пути), runtime.py (встроенный сервер), install.py (модель и наборы
│                          # голосов из админки), editor.py (новый голос из записи), bench.py (скорость устройств),
│                          # separate.py (очистка образца от музыки: htdemucs в ONNX),
│                          # desktop.py (точка входа программы для Windows), updates.py (проверка новой версии),
│                          # i18n.py (язык интерфейса), downloads.json (закреплённые загрузки: CUDA из llama.cpp и кванты
│                          # модели), static/ (static/en.json — английский словарь, static/i18n.js — перевод
│                          # страниц, static/quants/ — примеры звука квантов для админки и установщика)
├── packaging/windows/     # программа и установщик для Windows: build-runtime.py (рантайм с DLL VC++),
│                          # build-exe.py (Nuitka), StreamTTS.iss (Inno Setup: загрузка CUDA и модели),
│                          # release.py (latest.json релиза), THIRD-PARTY*.txt (сторонние лицензии);
│                          # сборка и релиз в CI — .github/workflows/windows.yml
├── scripts/
│   ├── download-model.sh  # загрузка модели и фронтенда с докачкой
│   └── import-voices.sh   # регистрация голосов из пар .wav + .txt и сохранение в voices/
├── tools/                 # подбор образцов голосов (docs/voice-pipeline.md), запуск из корня проекта
│   ├── voices/            # общее: страница выбора, синтез и оценки, модель эмоций и её сверка на слух, CAM++
│   ├── wc3/               # реплики Warcraft III: нарезка кандидатов, проверка текстов (docs/voices-wc3.md)
│   └── sources/           # реплики из вики, файлов игр и видео, отбор по голосу, сборка вариантов
├── effects/               # эффекты поверх синтеза (post в manifest.json), их запускает и stream-tts
├── docs/                  # development.md (этот файл), voice-pipeline.md, voices.md, voices-wc3.md,
│                          # stream-tts.md (спецификация), stream-tts-deploy.md, stream-tts-windows.md
│
│   Данные (в git не попадают):
├── models/                # модели (volume, только чтение)
├── voices/                # сохранённые голоса .gguf (volume)
├── voice-refs/            # итоговые образцы голосов (.wav + .txt) по наборам: wc3, games, streamers
├── voice-clips/           # варианты образцов, их синтез, оценки и выбор по наборам
├── voice-origs/           # сырые звуковые паки
├── samples/               # тестовые аудио
├── runtime/               # нативный cosyvoice-server с DLL и ffmpeg для режима managed (Windows)
└── stream-tts-data/       # настройки, токены и история stream-tts (volume)
```

## Образ

Образ собирается на базе `ubuntu:26.04`: релизы cosyvoice.cpp собраны там и требуют glibc 2.43 и ICU 78.
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

`--max-llm-len` (по умолчанию 4096) на видеопамять не влияет, а 1024 уже не хватает длинным образцам.

## Обновление версий

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

## stream-tts из исходников

`cd stream-tts && python -m venv .venv && .venv/Scripts/pip install -r requirements.txt`,
затем `COSYVOICE_URL=http://localhost:8080 .venv/Scripts/python -m streamtts` (или конфигурация `stream-tts`
в `.claude/launch.json`). Без `COSYVOICE_URL` stream-tts сам запускает нативный cosyvoice-server из `runtime/` в
корне проекта (режим managed, [stream-tts-deploy.md](stream-tts-deploy.md)) с моделью из `models/` и
голосами из `voices/`; конфигурация `stream-tts-managed` — на порту 8092 и с данными в `stream-tts-data/native/`,
чтобы не мешать Docker-версии. Английский интерфейс — конфигурация `stream-tts-en` (порт 8093, без модели) и
проверка словаря `python stream-tts/check-i18n.py`. Программа для Windows —
[stream-tts-windows.md](stream-tts-windows.md).
