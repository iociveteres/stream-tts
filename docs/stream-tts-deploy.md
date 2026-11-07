# Stream TTS: развёртывание

Часть спецификации [stream-tts.md](stream-tts.md): как запускается сервис и откуда берёт сервер синтеза, модель и
голоса. Программа для Windows — [stream-tts-windows.md](stream-tts-windows.md).

## Сервер синтеза

stream-tts всегда сам запускает сервер синтеза и обслуживает его: админка и сервер — на одной машине (компьютер
стримера с программой для Windows или Linux-сервер с Docker, к админке которого ходят по локальной сети). Моделей
три — CosyVoice3 (cosyvoice-server), ESpeech (espeech-server, [espeech.md](espeech.md)) и Supertonic 3
(supertonic-server, [supertonic.md](supertonic.md)), работает одна, выбранная
на вкладке «Сервер» (`settings.engine`; спецификация — [stream-tts.md](stream-tts.md), «Модели синтеза»).

cosyvoice-server — нативный `cosyvoice-server(.exe)`: папка `runtime/` рядом с программой (в исходниках — в корне
проекта), в образе Docker — `/opt/cosyvoice` через `STREAM_TTS_RUNTIME`, или путь в `COSYVOICE_SERVER`. Если не
нашёлся ни один сервер, stream-tts не запускается. stream-tts запускает сервер (адрес `COSYVOICE_HOST`, по умолчанию
`127.0.0.1`; порт — свободный или `COSYVOICE_PORT`), ходит к нему по `127.0.0.1`, перезапускает при падении и
завершает при выходе (на Windows через Job Object, даже если stream-tts убит, на Linux через `PR_SET_PDEATHSIG`).
Аргументы собирает `engines/cosyvoice/launch.py`: модель (`MODEL` или единственный `*.gguf` в папке моделей; без
модели сервер ждёт её появления), голоса `*.gguf` из папки голосов и её подпапок-наборов (имя голоса — имя файла), пути ONNX-фронтенда из `<модели>/frontend-onnx` (сам фронтенд грузится,
только когда создаётся голос), проверка AVX2. Бэкенд выбирается по `cosyvoice-cli --list-backends`: CUDA, если есть
`ggml-cuda` и видеокарта NVIDIA, иначе Vulkan (дискретная раньше встроенной), иначе CPU;
`COSYVOICE_BACKEND=cuda|vulkan|cpu` задаёт его явно. Всегда `GGML_CUDA_DISABLE_GRAPHS=1`, на CUDA ещё
`--llm-flash-attn 0`. В админке рядом с адресом сервера видны «встроенный», бэкенд, модель и число голосов.

ESpeech (`engines/espeech/launch.py`): `python -m espeech` интерпретатором его окружения — `ESPEECH_SERVER` (путь
к `python(.exe)` или папке с ним), иначе скачанный рантайм, если в `<рантайм>/espeech/` лежит код espeech-server
(программа для Windows и образ Docker: Python с torch и модель качаются кнопкой на вкладке «Загрузки»,
[espeech.md](espeech.md), «Поставка»; `ESPEECH_RUNTIME` — папка рантайма), иначе `espeech/.venv` проекта;
`--models <модели>/espeech` (без весов сервер ждёт, причина — в админке), `--voices <голоса>`, `--device cuda`, если
есть драйвер NVIDIA (`ESPEECH_DEVICE=cpu|cuda` или выбор в админке).

Supertonic 3 (`engines/supertonic/launch.py`) своего рантайма не имеет — numpy и onnxruntime у stream-tts уже есть:
программа для Windows запускает сервер сама (`StreamTTS.exe --supertonic …`, код скомпилирован в exe), в Docker —
`python -m supertonic` Python образа с кодом из `/opt/cosyvoice/supertonic` и `/opt/cosyvoice/espeech` в
`PYTHONPATH`, из исходников — интерпретатором stream-tts с корнем проекта в `PYTHONPATH` (`SUPERTONIC_SERVER` —
другой python или папка с ним); `--models <модели>/supertonic` (без модели сервер ждёт, причина — в админке),
`--voices <голоса>`, `--threads`, если задана `SUPERTONIC_THREADS`. Устройство — только процессор. Модель (~400 МБ) и
10 готовых голосов ставит кнопка «Скачать» в карточке «Supertonic 3» на вкладке «Загрузки» — везде одинаково
([supertonic.md](supertonic.md), «Поставка»).

Встроенный сервер один: смена модели перезапускает его с другим движком на том же порту (`COSYVOICE_PORT`,
`ESPEECH_PORT` или `SUPERTONIC_PORT` модели, выбранной при запуске, иначе свободный).

## Первый запуск

Вкладка «Загрузки» в админке (`install.py`, `static/admin-downloads.js`), без модели админка открывается сразу на
ней; вариант CosyVoice3 и модель синтеза выбираются на вкладке «Сервер»:
- **Модель.** В карточке «CosyVoice3» — кванты Q8_0 (по умолчанию), Q6_K и Q5_K_M (`QUANTS` в `downloads.py`):
  размер файла, видеопамять и пример звучания (`static/samples/cosyvoice-<квант>.mp3`, синтез с seed 42). Голос примеров
  `ohf-denis` — из датасета [ru_RU-denis](https://github.com/OHF-Voice/voice-datasets) (CC0: записи, переданные
  добровольцами для синтеза речи Home Assistant), поэтому mp3 лежат в git и в установщике. Образец — четыре
  реплики из `ru-RU/3000000001_3000000300_Chat` (3000000223, 026, 112, 274), склейка 10,8 с, 24 кГц; синтез —
  cosyvoice-server на CPU, mp3 24 кГц 48 кбит/с. Видеопамять — пик cosyvoice-server на CUDA, RTX 3060 Ti, 2026-10-17: обычно / на самом длинном
  образце (21,7 с) — Q8_0 ≈1,5 / 1,9 ГБ, Q6_K ≈1,45 / 1,8 ГБ, Q5_K_M ≈1,3 / 1,7 ГБ; на Vulkan пик примерно
  на 0,5 ГБ больше. На 54 фразах (6 голосов × 3 фразы × 3 seed) расшифровка Whisper (cer 0,011–0,017) и сходство
  CAM++ (0,810–0,820) у трёх квантов в пределах шума. Остальные кванты — `scripts/download-model.sh`; положенный
  вручную `*.gguf` тоже виден в списке вариантов CosyVoice3 на вкладке «Сервер». Загрузка — как в
  `scripts/download-model.sh`: файл с Hugging Face пишется в `<имя>.part` с докачкой по Range и пятью повторами,
  сверяется с sha256 из заголовка `x-linked-etag` и только потом переименовывается. Прогресс, скорость и остаток
  времени видны в админке; «Отменить» оставляет `.part`, «Продолжить» докачивает с того же места; загрузки идут по
  одной, остальные ждут в очереди. ONNX-фронтенд — пакет «Голос из записи» в карточке «Создание голосов»
  ([stream-tts.md](stream-tts.md), «Голоса»): для озвучки он не нужен. Первая скачанная модель становится выбранной
  (`settings.engine`), квант — вариантом CosyVoice3 (`settings.model`), если варианта ещё нет, и сервер запускается
  сам; следующие загрузки выбор не меняют. Из нескольких установленных моделей используется выбранная. Установщик для Windows модель не
  качает: при первом запуске без модели админка открывается на этой вкладке.
- **Устройство синтеза**: «Автоматически», CUDA, Vulkan или процессор (`settings.backend`); варианты, которых нет на
  машине, недоступны. Смена перезапускает сервер. `COSYVOICE_BACKEND` и `MODEL` выбор в админке перекрывают.
- **Скорость синтеза** (`bench.py`) — замер на машине стримера вместо чисел из нашей разработки. «Проверить
  текущее»: короткая фраза-разогрев, затем фраза ~6 с целиком (RTF = время синтеза / длина звука, меньше — быстрее) и она же
  стримингом с текущим `chunk_tokens` до первого куска (задержка первого звука, дальше `/tts/stop`). «Сравнить все
  устройства» — то же на CUDA, Vulkan и CPU по очереди с перезапуском сервера, в конце возвращается выбранное
  устройство. Всё время проверки доступ к cosyvoice занят с приоритетом «послушать», так что озвучка ждёт в очереди
  и не попадает на перезапуск; во время озвучки проверка не запускается. Результат — последний замер на пару
  устройство + модель (`settings.bench`), ячейка RTF зелёная до 0,5, жёлтая до 0,9, красная выше.
  Везде в интерфейсе скорость — RTF с двумя знаками («RTF 0,31»). На RTX 3060 Ti, Q8_0, 2026-10-18, три прогона:
  CUDA RTF 0,21–0,27 (первый звук 1,3–1,6 с), Vulkan 0,25–0,36 (1,4 с), CPU 4,5 (31 с).
- **Наборы голосов** — zip с голосами (`*.gguf` CosyVoice, `*.espeech` ESpeech) и `manifest.json` (ключи — имена
  голосов: `title`, `post`…) в корне архива или в одной папке; обычно это коллекция одной модели одного источника
  (`voice-sets/<модель>/<набор>.zip`, `tools/voices/pack-sets.py`). Имя набора — эта папка или имя zip (латиница,
  цифры, `-`, `_`), оно же группа на странице голосов. Набор ставится в `<голоса>/<набор>/`: голоса обеих моделей
  одного набора лежат в одной папке, повторная установка заменяет голоса только тех моделей, что есть в zip, а
  записи манифеста голосов, у которых не осталось файлов, убираются. В списке наборов — строка на модель («wc3 ·
  ESpeech»), «✕» удаляет голоса этой модели (последней — вместе с папкой). Голос с именем, которое у этой модели уже
  есть в другом наборе или без набора, пропускается; файлы не на верхнем уровне и не голоса игнорируются, `.gguf`
  проверяется по сигнатуре, `.espeech` — по заголовку safetensors; голос — до 64 МБ, архив — до 2 ГБ (адаптер LoRA
  ESpeech ~10 МБ). Пока файлы меняются, сервер остановлен (на Windows он держит `.gguf` открытыми),
  затем запускается с новым списком; stream-tts перечитывает голоса после каждого запуска сервера, примеры новых
  голосов синтезируются в фоне. `post` из `manifest.json` может ссылаться только на `effects/*.py`. Zip грузится
  сырым телом (`Content-Type: application/zip`, имя в `X-Filename`): такой запрос с чужой страницы требует
  CORS-проверки, как и JSON. `manifest.json` в архиве — до 1 МБ (читается в память целиком).

**Доступ** (`access.py`, `auth_middleware` в `web/__init__.py`). Пароль необязателен. Без пароля пускаются (все
страницы, и оверлей) только запросы из локальной сети: адрес клиента не из интернета (частные сети, loopback,
link-local, 100.64.0.0/10 — Tailscale), без заголовков прокси (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`,
`CF-Connecting-IP`), а в `Host` — IP не из интернета, `localhost`, имя без точки или зоны `.local`, `.lan`,
`.home.arpa`, `.internal`. Проверка `Host` нужна и против DNS rebinding (сайт, чьё имя ведёт на адрес в сети), и
для Docker Desktop: там адрес клиента — всегда частный адрес шлюза, и из интернета запрос отличает только публичное
имя или IP в `Host`. Отказ — 403 с подсказкой; последний отказ показывается в админке («Настройки» → «Сервер»).
С паролем пускается любой адрес после входа, `Host` не проверяется — чужую страницу останавливает cookie.

Пароль задают, меняют и снимают в админке; хранится scrypt-хэш с солью в `<данные>/admin-password`. Смена и снятие —
с текущим паролем, снять можно только из локальной сети (иначе вход сразу закроется). Токен cookie — HMAC от
`<данные>/secret` и хэша, поэтому смена выкидывает все прочие входы; сокеты админки закрываются кодом 4001, вкладка,
сменившая пароль, переподключается с новой cookie, остальные перезагружаются. `STREAM_TTS_PASSWORD` главнее файла и
закрывает поле в админке. Забыли пароль — удалить `admin-password` и перезапустить.

**Защита от чужих страниц**: POST в `/api/` — только JSON (или тип загрузки) и без чужого `Origin`; сокеты `/api/ws`
и `/overlay/ws` с чужим `Origin` — 403 (WebSocket браузер открывает без CORS). Оверлей принимает `ended` только от
играющего клиента и только для текущей фразы.

## Рантайм и пути

Рантайм для Windows (проверено 17.10.2026 на RTX 3060 Ti): cosyvoice.cpp v0.2.0
(`cosyvoice-67d45fe-windows-x64-ffmpeg.zip`), GGML из llama.cpp b11500 (`ggml*.dll` со всеми `ggml-cpu-*.dll`,
`libomp.dll`; `ggml-cuda.dll` с `cublas64_13`, `cublasLt64_13`, `cudart64_13` из `cudart-llama-bin-win-cuda-13.4-x64`;
`ggml-vulkan.dll`), ICU 78 (`icudt78`, `icuuc78`, `icuin78`), FFmpeg 8.1 lgpl-shared (DLL и `ffmpeg.exe`), рантайм
VC++ 14.51+ (cosyvoice собран тулсетом 14.51; со старым `msvcp140.dll` не запускается). Ядро без бэкендов GPU —
188 МБ, CUDA — ещё 664 МБ, Vulkan — 45 МБ. Скорость (RTF, фраза целиком):
CUDA 0,22–0,32, Vulkan 0,22–0,38 (первый запрос на новой машине ~35 с — компиляция шейдеров, кэш драйвера живёт
между запусками), CPU 4–9.

Пути (`paths.py`). Из исходников проекта — как раньше: `stream-tts-data/`, `models/`, `voices/`, `voice-refs/` (манифесты
`voice-refs/<модель>/<набор>/manifest.json`),
`runtime/` в корне (CUDA, скачанная кнопкой, ложится туда же, к серверу; `STREAM_TTS_CUDA` — другая папка, в Docker —
том). Программа, поставленная установщиком (рядом с exe лежит файл-признак `portable`), хранит всё
в своей папке: данные — `data\`, модели — `models\`, голоса — `voices\`; собранная программа без признака (например,
`build-exe.py --runtime`) — в `%LOCALAPPDATA%\StreamTTS\` (Linux: `$XDG_DATA_HOME/stream-tts`). Код и статику она
только читает. Всё переопределяется переменными `STREAM_TTS_DATA`,
`STREAM_TTS_MODELS`, `STREAM_TTS_VOICES`, `STREAM_TTS_PROJECT`, `STREAM_TTS_RUNTIME`, `STREAM_TTS_CUDA`. ffmpeg ищется в рантайме и
рядом с программой, потом в `PATH` (или `FFMPEG`).

В папке данных: `settings.json`, `token.json`, `stream-tts.sqlite` (история за 30 дней), `profanity.txt` (корни мата,
правится и из интерфейса), `demos/` (mp3-примеры голосов), `admin-password`.

## Docker

Сервис `stream-tts` в `docker-compose.yml` — со встроенным сервером из того же образа (корневой
`Dockerfile`, цель `stream-tts`; пользователь берёт готовый `ghcr.io/iociveteres/stream-tts`, из репозитория —
сборка через `docker-compose.dev.yml`, [development.md](development.md), «Образы»), `restart: unless-stopped`. Возможности те же, что у программы для Windows:
вкладки «Сервер» и «Загрузки», наборы голосов из zip, загрузка пакетов, выгрузка модели, очистка образца от музыки. Образ один —
с видеокартой и без: CUDA (два архива релиза llama.cpp, ~590 МБ, на диске ~730 МБ; `cuda.linux` в `downloads.json`)
качает кнопка в карточке «Видеокарта NVIDIA» на вкладке «Загрузки» в том `./cuda`, если контейнер видит видеокарту NVIDIA (запуск с
`docker-compose.gpu.yml` или `--gpus all`: тогда в контейнере есть `libcuda.so.1`). Сервер подключает её из тома
(`GGML_BACKEND_PATH`, `engines/cosyvoice/launch.py`), пересборка образа её не трогает. Проверка обновлений выключена:
обновление — `docker compose pull && docker compose up -d` (из исходников — `git pull && docker compose up -d --build`). ESpeech — так же, как у программы: в образе только код
espeech-server и заглушки библиотек CUDA (сотни КБ), Python 3.14 с torch (скачать ~580 МБ, на диске ~1,4 ГБ) и
модель (1,8 ГБ) качает кнопка «Скачать» в карточке «ESpeech» на вкладке «Загрузки» в тома `./espeech-runtime` и
`./models` — пересборка образа их не трогает. С CUDA ESpeech работает на видеокарте (cuBLAS и cudart torch берёт из
тома `./cuda`; если её нет, а видеокарта есть, она качается вместе с ESpeech), без неё — только на процессоре
([espeech.md](espeech.md), «Поставка»).
Supertonic 3: в образе — код supertonic-server (десятки КБ), его запускает Python образа; модель (~400 МБ) и готовые
голоса кнопка «Скачать» в карточке «Модель Supertonic 3» кладёт в тома `./models/supertonic` и `./voices/supertonic`.
С Docker Desktop на Windows загрузка дольше, чем у программы (~10 мин): запись тысяч файлов рантайма в том на диске
Windows медленная.

- Образ: `ubuntu:26.04` (glibc 2.43 для cosyvoice.cpp), Python 3.14 из apt в venv `/opt/venv`, ffmpeg, aiohttp,
  numpy, onnxruntime; сервер — `/opt/cosyvoice`, CUDA — в томе.
- Пути: `STREAM_TTS_RUNTIME=/opt/cosyvoice` (в его `espeech/` — код espeech-server и заглушки, в `supertonic/` — код
  supertonic-server),
  `STREAM_TTS_MODELS=/models`, `STREAM_TTS_VOICES=/voices`, `STREAM_TTS_DATA=/data`, `STREAM_TTS_PROJECT=/project`,
  `ESPEECH_RUNTIME=/espeech-runtime/python`, `STREAM_TTS_CUDA=/cuda`.
- Тома: `./stream-tts-data:/data` (SQLite, настройки, токены, кеш примеров), `./models:/models`, `./voices:/voices`,
  `./espeech-runtime:/espeech-runtime` и `./cuda:/cuda` с записью (админка качает модели, рантайм ESpeech и CUDA и
  ставит наборы; рантайм — в `espeech-runtime/python/`, рядом при загрузке — `python.dl/` и `python.tmp/`),
  `voice-refs/` и `effects/`
  (скрипты эффектов) только для
  чтения. Режим `cut` в `effects/villager-fx.py` звуки жителя не читает. Остальным режимам нужна
  `voice-origs/minecraft-villager/`.
- Порты: 8090 — интерфейс и оверлей, 8080 (`PORT`) — WebUI и API сервера для `scripts/` и `tools/`, оба на
  `127.0.0.1`. Сервер в контейнере слушает `0.0.0.0` (`COSYVOICE_HOST`), порт `COSYVOICE_PORT=8080`.
- Перезапуск stream-tts перезапускает и сервер (модель и голоса грузятся за несколько секунд), `docker stop`
  завершает оба. Сервер ненадолго останавливается и при установке или удалении набора, выгрузке модели, смене
  модели или устройства и проверке скорости.
- `MODEL` в stream-tts не передаётся: он закрепил бы модель и отключил выбор в админке. При нескольких `*.gguf` в
  `models/` модель выбирается на вкладке «Сервер» (`settings.model`).

**Инструменты подготовки голосов** (`scripts/import-voices.sh`, `tools/voices/synth.py`, `pick.py`) ходят к
встроенному серверу на порту 8080 (`PORT`) — отдельного контейнера с сервером нет. Синтез stream-tts в это время не
запускать: сервер выполняет запросы строго по одному.

## Переменные

Переменные `.env` (в программе для Windows — `stream-tts.env`, см. [stream-tts-windows.md](stream-tts-windows.md)):

| Переменная | По умолчанию | Зачем |
|---|---|---|
| `STREAM_TTS_PORT` | `8090` | порт интерфейса и оверлея |
| `STREAM_TTS_BIND` | `127.0.0.1`, в Docker `0.0.0.0` | `0.0.0.0` — админка и оверлей в сети (OBS на другом ПК) |
| `STREAM_TTS_PASSWORD` | — | пароль админки вместо заданного в админке (поле там закрыто); необязателен, см. «Доступ» |
| `STREAM_TTS_LANG` | язык установщика, иначе `ru` | `ru` или `en`: язык при первом запуске, дальше — переключатель в админке |
| `ESPEECH_SERVER` | скачанный рантайм, иначе `espeech/.venv` проекта | python окружения espeech-server или папка с ним |
| `ESPEECH_RUNTIME` | `espeech-runtime` рядом с программой, `/espeech-runtime/python` в Docker | куда кнопка «Скачать» ставит Python с torch для ESpeech |
| `ESPEECH_DEVICE` | `auto` | `cuda` или `cpu` для ESpeech |
| `ESPEECH_PORT`, `ESPEECH_HOST` | свободный, `127.0.0.1` | как `COSYVOICE_PORT` и `COSYVOICE_HOST`, если при запуске выбрана ESpeech |
| `SUPERTONIC_SERVER` | сама программа для Windows, python stream-tts в Docker и в исходниках | python для supertonic-server или папка с ним |
| `SUPERTONIC_THREADS` | — | потоков процессора у Supertonic 3 (`--threads`) |
| `SUPERTONIC_PORT`, `SUPERTONIC_HOST` | свободный, `127.0.0.1` | как `COSYVOICE_PORT` и `COSYVOICE_HOST`, если при запуске выбрана Supertonic 3 |
| `COSYVOICE_SERVER` | `runtime/cosyvoice-server(.exe)` | путь к серверу или его папке |
| `COSYVOICE_BACKEND` | `auto` | `cuda`, `vulkan` или `cpu` |
| `COSYVOICE_PORT` | свободный, `8080` в Docker | порт сервера |
| `COSYVOICE_HOST` | `127.0.0.1`, `0.0.0.0` в Docker | адрес, на котором слушает сервер (stream-tts ходит к нему по `127.0.0.1`) |
| `MODEL` | выбранный в админке или единственный `*.gguf` | файл модели, выбор в админке отключается (в compose не передаётся) |
| `PORT` | `8080` | Docker: порт сервера на хосте |
| `TWITCH_CLIENT_ID` | — | можно задать и в интерфейсе |

## Отдельный домашний сервер

`stream-tts` со встроенным сервером синтеза — на сервере, на ПК стримера только OBS с
Browser Source `http://<сервер>:8090/overlay`. Оверлей открыт без пароля, он только читает. Админка (настройки,
токены) закрыта паролем. Синтез на одной машине и `stream-tts` на другой не поддерживаются.

## Из репозитория: `setup.sh`

Для инструментов подготовки голосов (`scripts/`, `tools/`), выбора кванта при установке и сборки образа из исходников.
Команды — в bash: Linux, WSL2 или Git Bash на Windows (из PowerShell скрипт не запустится; Docker Desktop должен быть
запущен). В WSL держите проект в домашней папке (`~/...`), а не на `/mnt/c/...`: модель грузится заметно быстрее.

```bash
git clone https://github.com/iociveteres/stream-tts.git && cd stream-tts && ./setup.sh
```

Скрипт проверяет Docker, качает модель (по умолчанию `Q8_0`) и ONNX-фронтенд в `./models` (имеющиеся файлы
пропускает), пишет в `.env` порт и набор compose-файлов, скачивает образ (с `--build` — собирает) и запускает
stream-tts, ждёт ответа сервера и печатает адреса, в том числе для других ПК в сети. WebUI сервера синтеза —
**http://localhost:8080**, админка — **http://localhost:8090**.

```bash
./setup.sh Q6_K                  # другой квант
./setup.sh Q8_0 --frontend full  # полный ONNX-фронтенд (~1 ГБ) вместо int8
./setup.sh Q8_0 --frontend none  # без фронтенда (клонирование из аудио недоступно)
PORT=9000 ./setup.sh             # другой порт сервера синтеза на хосте
./setup.sh Q8_0 --gpu            # видеокарта NVIDIA (CUDA)
./setup.sh Q8_0 --build          # образ из исходников, эффекты и manifest.json голосов — из папок проекта
```

`COMPOSE_FILE` в `.env`: с `--gpu` — ещё `docker-compose.gpu.yml`, с `--build` — `docker-compose.dev.yml`, поэтому
обычные `docker compose up/down/logs` работают с тем же набором. Вернуться на CPU или к готовому образу —
`./setup.sh` без флагов. После `git pull` с `--build` — `docker compose up -d --build`.

Без `setup.sh`, с готовым образом и видеокартой: чтобы не писать `-f` в каждой команде, положите рядом с
compose-файлами `.env` со строками `COMPOSE_FILE=docker-compose.yml:docker-compose.gpu.yml` и
`COMPOSE_PATH_SEPARATOR=:`.

## Кванты CosyVoice3

`scripts/download-model.sh --list` печатает кванты с размерами.

| Квант | Размер | Видеопамять (CUDA) | Комментарий автора модели |
|---|---|---|---|
| `Q8_0` | 944 МБ | ≈1,5 ГБ, до 1,9 | **по умолчанию**, почти без потерь |
| `Q6_K` | 840 МБ | ≈1,45 ГБ, до 1,8 | хорошее качество |
| `Q5_K_M` / `Q5_1` | ~683 МБ | ≈1,3 ГБ, до 1,7 | приемлемо |
| `Q4_K_M` | 629 МБ | — | иногда звук приглушённый |
| `Q3_*`, `Q2_*` | < 530 МБ | — | заметные артефакты |

Видеопамять — пик cosyvoice-server на RTX 3060 Ti, обычно и на самом длинном образце (~22 с); сравнение квантов —
«Первый запуск». Если Hugging Face недоступен, файлы кладут вручную:

```
models/
├── CosyVoice3-2512_Q8_0.gguf
└── frontend-onnx/
    ├── campplus.int8.onnx            (или campplus.onnx)
    └── speech_tokenizer_v3.int8.onnx (или speech_tokenizer_v3.onnx)
```

Несколько `.gguf` в `models/` — нужная выбирается на вкладке «Сервер».

## WebUI и API cosyvoice-server

Порт 8080 (только `127.0.0.1`), для инструментов и опытов; озвучку stream-tts в это время не заказывать — сервер
выполняет запросы строго по одному.

Голос в WebUI: вкладка **Extract** → аудио 5–15 с чистой речи (или запись с микрофона) → **точная расшифровка** и имя.
Голос живёт в памяти до перезапуска; чтобы сохранить, **Save speaker** с путём `/voices/<имя>.gguf` или
`/voices/<набор>/<имя>.gguf` (подпапку создать заранее). Из двух файлов с одним именем подключается первый, второй
пропускается с предупреждением в логе.

| Режим | Что делает |
|---|---|
| **zero-shot** | голос и манера образца: нужная интонация — из образца, где она уже есть |
| **instruct** | тот же голос плюс инструкция к стилю («говори шёпотом»); английские и китайские, вероятно, надёжнее |
| **cross-lingual** | голос образца на другом языке |

```bash
# регистрация голоса из аудио
curl -X POST "http://localhost:8080/speaker?type=extract" -F name=myvoice \
  -F "text=Точная расшифровка записи." -F "audio=@sample.wav"
# синтез; с инструкцией: "mode":"instruct","instruction":"Speak in a slow, mysterious whisper."
curl -X POST http://localhost:8080/tts -H 'Content-Type: application/json' \
  -d '{"text":"Привет!","voice":"myvoice","mode":"zero-shot","format":"wav"}' -o out.wav
# сохранение на диск
curl -X POST http://localhost:8080/speaker/save -H 'Content-Type: application/json' \
  -d '{"name":"myvoice","path":"/voices/myvoice.gguf"}'
```

Состояние: `GET /status`, `GET /speaker`, `GET /formats`. Справочник —
[docs/TOOLS.md](https://github.com/Lourdle/cosyvoice.cpp/blob/v0.2.0/docs/TOOLS.md) cosyvoice.cpp, но `POST /model/load`
ждёт поле `model`, а не `model_path`, и после выгрузки модели голоса сбрасываются. Формат — поле `format` (`wav`,
`mp3`, `opus`, `flac`, `aac`, `m4a`, `pcm`); `response_format`, как у OpenAI, сервер молча пропускает и отдаёт WAV.
Чтобы звук начинал играть раньше, в WebUI включают Streaming с MP3 или Opus.

Свой набор: пары `<имя>.wav` + `<имя>.txt` (точная расшифровка) в папке → `scripts/import-voices.sh <папка>`. С
`--force` скрипт пересобирает сохранённые голоса (все или перечисленные после флага), сначала выгружая их: иначе 409.

**Перенос голосов.** `voices/<имя>.gguf` (~120 КБ) самодостаточен: мел-спектрограмма образца, эмбеддинг говорящего
(CAM++), речевые токены и текст, без весов модели. Достаточно скопировать файл в `voices/` другого инстанса и
перезапустить его; фронтенд там не нужен. Файл работает с любым квантом CosyVoice3, но не с другими семействами
(CosyVoice2). Если формат сменится в новой cosyvoice.cpp, голоса пересобирают из образцов — поэтому пары `.wav` +
`.txt` стоит хранить (в этом проекте — `voice-refs/cosyvoice/<набор>`); здесь фронтенд уже нужен.

**Скорость** (Ryzen 5 5600G, RTX 3060 Ti, Q8_0): zero-shot 8,5 с звука на CPU — ~36 с, instruct 11,1 с — ~54 с;
на GPU 6,7 и 8,3 с — ~2,1 с. RTF на CPU около 4 (для подготовки, не для живого диалога), на GPU 0,25–0,33, короткая
фраза — 1,5–2,5 с. Ограничение CPU до 6 потоков (по физическим ядрам) было медленнее, поэтому потоков — по умолчанию.

## Решение проблем

| Симптом | Причина и решение |
|---|---|
| `Docker daemon не запущен` | Запустите Docker Desktop. |
| `CPU без AVX2` | Процессор не поддерживается рантаймом; macOS на Apple Silicon и ARM-Linux тоже (бинарники под x86_64, эмуляция без AVX2). |
| `bash\r: No such file or directory` | Скрипты сохранены с CRLF: `sed -i 's/\r$//' *.sh`. Репозиторий защищён `.gitattributes`. |
| Извлечение голоса недоступно | Нет файлов в `models/frontend-onnx/`: `scripts/download-model.sh --frontend int8`. |
| Сервер не стартует, несколько моделей | Выберите модель на вкладке «Сервер». |
| Модель не качается | Скачайте вручную с Hugging Face в `models/` («Кванты CosyVoice3»). |
| `Docker не видит GPU` | Обновите драйвер NVIDIA; в Docker Desktop — бэкенд WSL2, на Linux — NVIDIA Container Toolkit. Проверка: `docker run --rm --gpus all ubuntu:26.04 nvidia-smi`. |
| На GPU тихий шум или мусор вместо речи | Нет `GGML_CUDA_DISABLE_GRAPHS=1` или включён `--llm-flash-attn` ([development.md](development.md), «Образы»). |
| В начале фразы лишний звук («точка», «пой») | Особенность модели, и на CPU, примерно 1 фраза из 8. Сгенерируйте заново с другим `seed`. |
