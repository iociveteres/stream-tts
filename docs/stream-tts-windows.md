# Stream TTS: программа для Windows

Часть спецификации [stream-tts.md](stream-tts.md); режимы, пути и переменные — в
[stream-tts-deploy.md](stream-tts-deploy.md).

Программа без Docker и Python. Setup.exe (~100 МБ) ставит StreamTTS для текущего пользователя, без прав
администратора и окна UAC: по умолчанию в `%LOCALAPPDATA%\Programs\StreamTTS`, папку можно выбрать любую, куда можно
писать (например, `D:\StreamTTS`; в Program Files нельзя — установщик проверяет запись и просит выбрать другую, а в
непустую чужую папку ставит только после вопроса). Всё живёт в папке программы: `data\` (настройки, база, примеры,
`stream-tts.env`, логи), `models\`, `voices\`, `runtime\` (cosyvoice-server с DLL), `espeech-runtime\` (Python с
PyTorch для ESpeech, если его скачали); `paths.py` узнаёт такую
установку по файлу `portable`, который кладёт установщик, а переменные `STREAM_TTS_*` по-прежнему важнее. Рантайм
VC++ лежит рядом с `cosyvoice-server.exe` (и с `StreamTTS.exe` — его кладёт Nuitka), поэтому VC++ Redistributable не
нужен: Windows сначала ищет DLL в папке exe, загруженные модули сервера — из `runtime\` (проверено по списку модулей
процесса; на машине без Redistributable не проверялось).

## Установщик

Установщик ничего не качает: в нём программа и рантайм без CUDA (~395 МБ на диске), без страниц выбора компонентов
и модели. Модель, CUDA и ESpeech программа качает по кнопкам на вкладке «Сервер»; при первом запуске без модели
админка открывается на ней (каталог квантов с размером, видеопамятью и примерами звука из `static/quants/`,
`install.py`, [stream-tts-deploy.md](stream-tts-deploy.md), «Первый запуск»), а если в системе есть драйвер NVIDIA и
нет CUDA, в строке «Модель не установлена» подсказано скачать и её. В сводке «Готово к установке» — что программа предложит
скачать потом: модель (если в папке её ещё нет) и CUDA (если есть `nvcuda.dll`) с размерами из `downloads.json`
(`build-exe.py` передаёт их через `/D`). Язык установщика пишется в `{app}\setup-lang.txt`: на нём админка
открывается в первый раз.

Установка поверх: `[InstallDelete]` очищает `{app}\runtime` целиком (GGML грузит все `ggml-*.dll` из папки, и DLL
старой версии могут оказаться несовместимыми), `data`, `models`, `voices` и `espeech-runtime` не трогаются. CUDA,
скачанная в программе, сохраняется, если её версия (`runtime\cuda-version.txt`) совпадает с `downloads.json`: перед
очисткой её файлы (`cuda.files`) переносятся в `{app}\.cuda-keep` и возвращаются после установки. CUDA другой
сборки удаляется вместе с `runtime`, и вкладка «Сервер» предлагает скачать новую. `setup-model.txt` (выбор модели в
установщике до 0.2.0) удаляется. Удаление спрашивает «удалить также модели, голоса и настройки?» (по умолчанию нет,
тихое удаление их оставляет); оставленные подхватит повторная установка в ту же папку.

CUDA — два архива из релиза llama.cpp той же сборки, что и ядро GGML (b11500): `llama-…-cuda-13.4` (146 МБ, из него
нужен только `ggml-cuda.dll`) и `cudart-llama-…-cuda-13.4` (404 МБ: `cudart64_13`, `cublas64_13`, `cublasLt64_13`).
Свой архив не нужен: качается оригинал с закреплённым sha256. Если драйвер NVIDIA есть (`nvcuda.dll`), а
`ggml-cuda.dll` рядом с сервером нет, вкладка «Сервер» в блоке «Устройство синтеза» предлагает скачать её: оба архива
с докачкой и проверкой sha256, из них в папку сервера при остановленном сервере распаковываются только нужные DLL,
версия пишется в `cuda-version.txt`, архивы удаляются, сервер перезапускается и заново ищет устройства (`install.py`,
`start_cuda`; `STREAM_TTS_CUDA_URL` — другая папка с теми же архивами, для проверки без GitHub:
`python -m http.server` в `build/cache`). Модель качается с Hugging Face с закреплённой ревизией (`model.url` в
`downloads.json`).

## ESpeech

Вторая модель ([espeech.md](espeech.md)) в установщик не входит: в `{app}\runtime\espeech\` установщик кладёт только
код espeech-server и DLL-заглушки, а Python с PyTorch и модель программа качает по кнопке «Скачать» в карточке
«Модель ESpeech» на вкладке «Сервер» (`engines/espeech/download.py`). Карточка видна, пока ESpeech не скачан, и у
выбранной модели ESpeech; выбрать её можно и до загрузки — сервер ждёт с причиной «ESpeech не скачан». Всё качается
из первоисточников, адреса, размеры и sha256 закреплены в разделе `espeech` файла `downloads.json`
(`packaging/pin-espeech.py`, раздел `runtime.windows`; Docker качает так же, [espeech.md](espeech.md), «Поставка»), с докачкой после обрыва или отмены и общим прогрессом:

| Что | Откуда | Скачать | На диске |
|---|---|---|---|
| встраиваемый Python 3.14.7 | python.org | 12 МБ | `{app}\espeech-runtime\` |
| пакеты (numpy, onnxruntime, tokenizers, safetensors, onnx, sympy для обучения LoRA и др.) — распаковкой колёс, без pip | PyPI | 45 МБ | там же |
| torch 2.14.1+cu132 — только нужные файлы колеса, диапазонами (Range) | download.pytorch.org | 434 МБ из 1,9 ГБ | там же, всего ~1,1 ГБ |
| ESpeech — EMA-копия весов из чекпойнта, диапазонами; Vocos; RUAccent | Hugging Face, закреплённые ревизии | 1,84 ГБ | `{app}\models\espeech\`, 1,4 ГБ |
| CUDA, если её нет, а видеокарта NVIDIA есть | релиз llama.cpp (как выше) | 550 МБ | `{app}\runtime` |

Из колеса torch не берутся cuDNN (он выключен: без него синтез на 20 % быстрее), cuSPARSE, cuSOLVER, NVRTC, cuFFT
(STFT на процессоре) и cuBLAS: `torch_cuda.dll` импортирует их статически, поэтому вместо них — заглушки с теми же
экспортами (`stubs\`, собирает `build-runtime.py`), а cuBLAS torch берёт настоящий из CUDA cosyvoice-server (без неё —
заглушки `stubs\cublas\`, и ESpeech работает только на процессоре). Папки DLL передаются серверу в `ESPEECH_DLL_DIRS`
(`espeech/__init__.py`), путь к пакету `espeech` — в `python314._pth` (встраиваемый Python не читает `PYTHONPATH`).
Если `nvcuda.dll` в системе есть, а драйвер NVIDIA не работает (torch: «Found no NVIDIA driver»), ESpeech загружается
на процессоре: при `auto` устройство выбирает torch, а CUDA-модель при сбое повторяется на CPU ([espeech.md](espeech.md)).
Рядом с `python.exe` кладётся CRT из `{app}\runtime`: torch нужен `vcruntime140_threads.dll`, которого во встраиваемом
Python нет. Обучению LoRA нужны ещё sympy и mpmath: `torch.optim` импортирует `torch._dynamo` (+81 МБ ОЗУ, только
на обучении); заглушённые библиотеки CUDA обучение не вызывает, с экономией видеопамяти и без (проверено на
скачанном рантайме и в собранной программе, [espeech.md](espeech.md), «Python 3.14»). После распаковки — байткод (`compileall`, иначе каждый
запуск компилирует torch заново) и проверка импортом (с `torch._dynamo`); рантайм собирается в `espeech-runtime.tmp`
и переименовывается, версия — хэш закреплённого рантайма в `version.txt` (при другой закреплённой версии карточка предлагает «Обновить»). Модель собирает `python -m
espeech.prepare` в скачанном рантайме (~1,5 мин, из них 80 с — словарь ударений в SQLite), исходники
(`{app}\models\.espeech-src\`) потом удаляются. На время загрузки нужно ~5 ГБ свободного места (проверяется до
начала). Кнопка «✕» удаляет рантайм и модель (голоса `.espeech` остаются).

`espeech-runtime` лежит вне `runtime`, потому что установка поверх очищает `runtime` целиком: обновление программы
ESpeech заново не качает. Удаление программы спрашивает и про него вместе с моделями.

### Обучение LoRA

Страница «LoRA» у голоса ESpeech на вкладке «Голоса» ([stream-tts.md](stream-tts.md), «Обучение LoRA голоса»)
работает и в программе: обучение идёт в процессе espeech-server на скачанном облегчённом torch. Заглушённые
библиотеки CUDA оно не вызывает — ни с экономией видеопамяти, ни без неё, вместе с остановкой, прослушиванием снимков
и сохранением в голос; из лишних для синтеза пакетов ему нужны только sympy и mpmath (они в рантайме, см. выше).
Проверено на скачанном рантайме и в собранной программе ([espeech.md](espeech.md), «Python 3.14»). Реплики
программа хранит в `data\espeech\train\<голос>\`, звук и видео приводит к WAV своим ffmpeg.

Сколько нужно (RTX 3060 Ti, 40 реплик, сценарий из espeech.md):

| | Видеопамять процесса espeech-server | ОЗУ (uss) |
|---|---|---|
| модель загружена / после синтеза | 0,89 / 0,94 ГБ | 1,37 / 1,47 ГБ |
| обучение без экономии видеопамяти | 3,48 ГБ | до ~1,95 ГБ |
| обучение с экономией | 1,31 ГБ, шаги в 1,4 раза дольше | до ~1,95 ГБ |

«Экономия видеопамяти» по умолчанию выключена: на картах от 6 ГБ обучению хватает и так. На картах меньше 6 ГБ
(видеопамять нужна ещё OBS и игре) её стоит включить; страница советует это сама, если свободно меньше 2,8 ГБ, а
при нехватке памяти предлагает «Включить экономию и начать снова». На RTX 3060 Ti 3000 шагов — около 15 минут. На
процессоре обучение возможно, но очень долгое.

## Supertonic 3

Третья модель ([supertonic.md](supertonic.md)) — лёгкая, только на процессоре: видеокарта остаётся игре, памяти —
~0,5 ГБ. Своего рантайма у неё нет: код supertonic-server (пакет `supertonic/` и нормализатор русского текста из
`espeech/`, чистый Python) скомпилирован в `StreamTTS.exe`, а numpy и onnxruntime в программе уже есть, поэтому
сервер запускает сама программа — `StreamTTS.exe --supertonic --models … --voices … --port …`
(`packaging/windows/StreamTTS.py`, `engines/supertonic/launch.py`). Веса в установщик не входят (лицензия
OpenRAIL-M требует передавать условия использования вместе с моделью): кнопка «Скачать» в карточке «Модель Supertonic
3» на вкладке «Сервер» сначала показывает условия лицензии и ссылку на неё, потом качает 17 файлов
`Supertone/supertonic-3` закреплённой ревизии с Hugging Face (раздел `supertonic` в `downloads.json`, 383 МБ, с
докачкой и проверкой sha256) в `{app}\models\supertonic\` и ставит 10 готовых голосов набором `supertonic` в
`{app}oices\supertonic\`. Карточка видна, пока модель не скачана, и у выбранной Supertonic 3; выбрать её можно и
до загрузки — сервер ждёт с причиной «нет модели», а после загрузки запускается сам. Своих голосов у модели нет
(редактор голоса для неё отказывает); пропавшие готовые голоса возвращает кнопка «Поставить голоса», «✕» удаляет
модель вместе с готовыми голосами.

## Сборка

Закреплённые загрузки — `stream-tts/streamtts/downloads.json`: CUDA (версия, нужные файлы и их размер, архивы
llama.cpp — адрес, размер, распакованный размер, sha256 и какие файлы из него берутся) и кванты модели (адрес
Hugging Face на конкретной ревизии, размеры, sha256), а ещё ESpeech (раздел `espeech`: рантаймы Windows и Linux и модель, его пишет
`packaging/pin-espeech.py`) и Supertonic 3 (раздел `supertonic`: ревизия Hugging Face, файлы с размером и sha256 —
таблица в [supertonic.md](supertonic.md); `tests/e2e/test_supertonic_download.py` сверяет, что закреплены все
нужные серверу файлы и стили). Его читают `downloads.py`, `build-runtime.py` (CUDA для `--cuda` и `--flat`, список заглушек
ESpeech) и `build-exe.py` (передаёт значения в `StreamTTS.iss` через `/D`). Переход на другую сборку
llama.cpp — новые `LLAMA` и `ASSETS` в `build-runtime.py` и новая запись `cuda` здесь (`build-runtime.py` не
соберётся, если CUDA не из сборки ядра). Файлы — в `packaging/windows/`:

- `build-runtime.py` — рантайм с закреплёнными версиями и sha256 (см. выше): `build/runtime/core/` (281 МБ, вместе с
  DLL рантайма VC++), с `--cuda` — ещё `build/runtime/cuda/` (664 МБ: `ggml-cuda.dll`, `cudart64_13`, `cublas64_13`,
  `cublasLt64_13` из архивов `downloads.json`) для `build-exe.py --runtime all`. Версия CUDA — CUDA и сборка
  llama.cpp (`13.4-b11500`): `ggml-cuda.dll` собран вместе с `ggml-base.dll` своей сборки. CRT (`msvcp140*`, `vcruntime140*`, `concrt140`) — самая новая полная копия не ниже 14.51 из
  `Microsoft.VC14*.CRT` установленных Visual Studio (`vswhere`) или из System32, либо из `--crt DIR`; на
  `windows-latest` в GitHub Actions стоит VS 2022 с redist 14.44, а в System32 — 14.51.36247, поэтому CI берёт её
  оттуда; `vcruntime140_threads.dll` в наборе — для torch ESpeech. В `core/espeech/` — пакет `espeech/` (только
  `*.py`) и DLL-заглушки из `stubs/*.def`: их собирает MSVC (`vswhere`, нужен C++ из Visual Studio или Build Tools,
  на `windows-latest` есть), `--flat` их не копирует. Нужна только стандартная библиотека Python.
- `../pin-espeech.py` (`packaging/pin-espeech.py`, общий с Docker) — закрепить загрузки ESpeech: раздел `espeech` в
  `downloads.json` (рантаймы обеих систем — Python, колёса PyPI под cp314 win_amd64 / manylinux, часть колеса torch;
  часть чекпойнта ESpeech — хвост zip с центральным каталогом, его sha256, выбор файлов и общий sha256 выбранных,
  `stream-tts/streamtts/partzip.py`), `stubs/*.def` (экспорт настоящих DLL из колеса torch и архива cudart) и
  `packaging/linux/stubs/**/*.map` для Docker. Запускать при смене версий в нём (`WIN_PYTHON`, `WIN_TORCH`,
  `PACKAGES` и Linux-варианты) или источников модели в `espeech/prepare.py` (`--what windows,linux,model` — что
  закреплять); колёса torch (1,9 ГБ и 533 МБ) и чекпойнт (2,7 ГБ) нужны целиком: `--torch-wheel`,
  `--linux-torch-wheel`, `--checkpoint` или загрузка в `build/cache`.
- `build-exe.py` — `StreamTTS.exe` через Nuitka (standalone, консольное приложение) в `dist/StreamTTS/` (113 МБ, из
  них 38 МБ — onnxruntime для очистки от музыки; его инструменты с onnx, sympy и protobuf не включаются):
  пакет `streamtts` со `static/` и `profanity.txt`, `effects/villager-fx.py`, а ещё supertonic-server — пакет
  `supertonic` и модули `espeech/` из `ESPEECH_MODULES` (нормализатор, ударения «+», WAV; без torch: RUAccent не
  включается). Их Nuitka берёт из корня проекта (`PYTHONPATH`), запускает сервер ключ `--supertonic` главного модуля
  `StreamTTS.py` — до импорта desktop, так что у копии-сервера нет ни блокировки одной копии, ни браузера. Новый
  импорт из `espeech/` в `supertonic/` — в `ESPEECH_MODULES` (сторож — `tests/e2e/test_supertonic_download.py`). У
  собранной программы `sys.executable` — несуществующий `python.exe` рядом с ней (так Nuitka изображает Python),
  настоящий путь к exe — `paths.PROGRAM`. Уведомление о коде Supertone (MIT) — `licenses\supertonic-THIRD-PARTY.txt`.
  Нужен сборочный venv
  (`requirements.txt`: зависимости stream-tts и Nuitka) на Python 3.14 и MSVC (Visual Studio или Build Tools с C++):
  MinGW Nuitka для Python 3.13+ не поддерживает. Первая сборка ~6 мин, повторные быстрее за счёт кэша компилятора.
  `--runtime core|all` кладёт рантайм рядом с exe для запуска без установщика, `--setup` собирает установщик.
  Собранную программу `paths.py` узнаёт по `__compiled__` (`sys.frozen` Nuitka не задаёт); данные пакета и
  `effects/` лежат рядом с exe. Пакет `streamtts` входит целиком, а импорты эффектов (они грузятся по пути) Nuitka не
  видит: их новые зависимости — в `--include-module` в `build-exe.py`. Собранный exe проверяют на другом порту и с
  отдельными данными (`STREAM_TTS_PORT`, `STREAM_TTS_DATA`; конфигурация `streamtts-exe-test`), чтобы не задеть
  Docker-версию на 8090.
- `StreamTTS.iss` — Inno Setup 6.5+: `dist/StreamTTS-Setup-<версия>.exe` (как он ставит, обновляет и удаляет —
  выше). Запущенную программу установщик и деинсталлятор видят по мьютексу `StreamTTS` и просят закрыть. Строка в
  `[Code]` не должна начинаться с `[`: Inno принимает её за заголовок секции. Языки — русский и английский: свои тексты в `[CustomMessages]`, список
  сторонних лицензий (`THIRD-PARTY.txt`, `THIRD-PARTY.en.txt`: что входит в установщик и что программа качает по
  кнопкам — модель, фронтенд, CUDA, Demucs, ESpeech, Supertonic 3) — страница «Информация», без принятия соглашения; в админке
  лицензий нет,
  а полные тексты (MIT, BSD и Apache требуют их прикладывать) ставятся в папку программы: `runtime\licenses\` —
  cosyvoice.cpp и встроенные в него библиотеки, GGML, ONNX Runtime, ICU, FFmpeg, OpenMP (`build-runtime.py`: из
  архивов и из репозиториев тех же версий, с проверкой sha256), `licenses\` — Python, пакеты из
  `stream-tts/requirements.txt` с зависимостями, исключение Nuitka для её runtime и сами списки (`build-exe.py`).
  Перенос русского текста посреди слова в окнах установщика —
  признак включённой в Windows бета-опции «UTF-8 для всех языков» (ANSI-кодовая страница 65001): так переносит
  сам `DrawText`, у остальных пользователей перенос по словам. Установщик — ~100 МБ, сжатие ~30 с (проверено на Inno
  Setup 6.7.3). Inno Setup можно не ставить в систему: `innosetup-6.x.exe /PORTABLE=1 /CURRENTUSER /VERYSILENT
  /DIR=packaging\windows\.inno` (папка в `.gitignore`, `build-exe.py` ищет ISCC там) или в любую папку и путь к
  `ISCC.exe` в переменной `ISCC`.
- `.github/workflows/windows.yml` — то же в GitHub Actions (`windows-latest`, по кнопке или тегу `v*`), установщик —
  артефакт сборки, а по тегу ещё и релиз на GitHub (`gh release create`): установщик и `latest.json`.
- `release.py` — файлы релиза: `dist/latest.json` и текст релиза из сообщения аннотированного тега. Версия одна —
  `__version__` в `stream-tts/streamtts/__init__.py` (её берёт `build-exe.py`, показывает админка); CI до сборки
  проверяет, что тег — `v<версия>`, а репозиторий совпадает с `REPO` в `updates.py`.

```bash
packaging/windows/.venv/Scripts/python.exe packaging/windows/build-runtime.py
packaging/windows/.venv/Scripts/python.exe packaging/windows/build-exe.py --setup
```

Выпуск версии: поднять `__version__`, закоммитить, `git tag -a v0.2.0 -m "что нового"` и `git push --tags`.

## Обновления

Своего сервера нет (`updates.py`): программа читает
`https://github.com/<REPO>/releases/latest/download/latest.json` — GitHub сам перенаправляет на файл последнего
релиза (pre-release не считаются), и это скачивание с CDN, а не API, так что лимит 60 запросов в час на IP на него
не действует. В `latest.json` — версия, дата, страница релиза, что нового и установщик с размером и sha256 (на
будущее: скачивание из программы). Частота — карточка «Обновления» в настройках: раз в день (по умолчанию), неделю,
месяц или не проверять; там же «Проверить сейчас». Первая проверка — через 30 с после запуска, дальше раз в час
программа смотрит, не пора ли; после ошибки сети — не чаще раза в 3 часа. Найденная версия хранится в настройках
(`update_latest`), поэтому плашка под вкладками видна и после перезапуска без сети; «Скрыть до следующей версии»
запоминает версию (`update_dismissed`). Обновление пока вручную: ссылка ведёт на страницу релиза, новый установщик
ставится поверх старого. Из исходников и в Docker проверки нет (обновление — `git pull`); `STREAM_TTS_UPDATE_URL`
задаёт другой адрес `latest.json` и включает её и там — так проверялось на локальном `python -m http.server`.

## Запуск

`desktop.py` (работает и из исходников: `python -m streamtts.desktop`):
- переменные из [таблицы](stream-tts-deploy.md#переменные) читаются ещё и из `stream-tts.env` в папке данных (`{app}\data\`, без установщика —
  `%LOCALAPPDATA%\StreamTTS\`; `KEY=VALUE`, переменные окружения важнее); при первом запуске там появляется пример с
  комментариями;
- одна копия на папку данных: повторный запуск открывает админку уже запущенной и выходит;
- админка открывается в браузере, когда сервер ответил (`--no-browser` — не открывать);
- лог — в консоли и в `logs\stream-tts.log` в папке данных (три старых файла по 2 МБ);
- закрыть окно консоли — выключить stream-tts, встроенный cosyvoice-server завершается вместе с ним. Если запуск
  не удался (порт занят), окно ждёт Enter, чтобы ошибку можно было прочитать.

Эффект `villager-fx.py` в программе работает только в режиме `cut`: звуков жителя из `voice-origs/` в поставке нет.
