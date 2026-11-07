# Навигация: задача → файлы

Что открыть под типовую задачу; ниже — области, данные и связи между частями. Пути — от корня проекта, в скобках —
что искать в файле. «На базе» — пути на
коммите до рефакторинга (База: `939f37a`), «=» — те же. Замер файлов и токенов по таблице —
`scripts/measure-docs.py`, что пути живы и вопросы на месте — `tests/e2e/test_index.py`.

В — эталонные вопросы замера навигации, З — частые задачи.

| № | Задача | Файлы сейчас | На базе | Тест |
|---|---|---|---|---|
| В1 | Где обрабатывается команда чата `!tts`, как добавить новую | `stream-tts/streamtts/commands.py` (chat, SERVICE_COMMANDS), `stream-tts/streamtts/rules.py` (MODS, R_*), `stream-tts/streamtts/chat_irc.py` (событие из IRC); ответы зрителю — через tr, перевод — В7 | `stream-tts/streamtts/engine.py` (Engine.chat, SERVICE_COMMANDS, R_*), `stream-tts/streamtts/chat_irc.py` (событие из IRC) | `tests/e2e/test_chat.py` |
| В2 | Третий движок синтеза: что реализовать, где регистрируется | `stream-tts/streamtts/engines/__init__.py` (ENGINES, Tts), `stream-tts/streamtts/engines/client.py` (протокол, Client), `stream-tts/streamtts/engines/espeech/__init__.py` (описание), `stream-tts/streamtts/engines/espeech/client.py`, `stream-tts/streamtts/engines/espeech/launch.py` (Launcher, find_server), `stream-tts/streamtts/engines/espeech/editor.py` (Maker); модель без голоса из записи и только на процессоре — `stream-tts/streamtts/engines/supertonic/editor.py` (Maker.EXTRACT), `stream-tts/streamtts/engines/supertonic/launch.py` (DEVICES, DEVICE_KEY) | `stream-tts/streamtts/engines/__init__.py` (ENGINES, Tts), `stream-tts/streamtts/engines/client.py` (протокол, Client), `stream-tts/streamtts/engines/espeech/__init__.py` (описание), `stream-tts/streamtts/engines/espeech/client.py`, `stream-tts/streamtts/engines/espeech/launch.py` (Launcher, find_server), `stream-tts/streamtts/engines/espeech/editor.py` (Maker), `tests/e2e/fake_cosyvoice.py` | `tests/e2e/test_engines.py`, `tests/e2e/fake_cosyvoice.py` (поддельный сервер) |
| В3 | Настройка из админки → сервер модели (схема `params`) | `espeech/engine.py` (status, PARAMS), `stream-tts/streamtts/engines/client.py` (SETTINGS, schema), `stream-tts/streamtts/engines/espeech/client.py` (SETTINGS, params()), `stream-tts/streamtts/engines/supertonic/client.py` (два параметра), `stream-tts/streamtts/store.py` (умолчания), `stream-tts/streamtts/web/admin.py` (post_settings), `stream-tts/streamtts/static/admin-server.js` (#model-params; схема — в /api/state) | `espeech/server.py` (/status, params), `stream-tts/streamtts/engines/client.py` (SETTINGS, schema), `stream-tts/streamtts/engines/espeech/client.py` (params()), `stream-tts/streamtts/web.py` (model_params, post_settings), `stream-tts/streamtts/store.py` (умолчания), `stream-tts/streamtts/static/admin.js` (#model-params) | `tests/e2e/test_engines.py` |
| В4 | Эффект `post`: как добавить, почему скрипт не из `effects/` не запускается | `stream-tts/streamtts/voices.py` (apply_post, load_effect), `effects/villager-fx.py` (apply), `stream-tts/streamtts/paths.py` (поиск в поставке) | = | `tests/e2e/test_voices.py` |
| В5 | «Server disconnected» у фразы после паузы | `stream-tts/streamtts/engines/client.py` (KEEPALIVE) | = | `tests/e2e/test_invariants.py` |
| В6 | Набор голосов из zip и что при этом со встроенным сервером | `stream-tts/streamtts/install.py` (Installer.import_set), `stream-tts/streamtts/voicesets.py` (_check_zip, _install_set), `stream-tts/streamtts/runtime.py` (paused), `stream-tts/streamtts/web/server.py` (api_voiceset_import) | `stream-tts/streamtts/install.py` (Installer, _check_zip, _install_set), `stream-tts/streamtts/runtime.py` (paused), `stream-tts/streamtts/web.py` (api_voiceset_import) | `tests/e2e/test_voicesets.py`, `tests/e2e/test_train.py` |
| В7 | Строка интерфейса с переводом на английский | `stream-tts/streamtts/i18n.py` (tr), `stream-tts/streamtts/static/i18n.js` (T, data-t), `stream-tts/check-i18n.py` (источники строк, --missing); словарь en.json целиком не читать — рецепт под таблицей | `stream-tts/streamtts/i18n.py` (tr), `stream-tts/streamtts/static/i18n.js` (T, data-t), `stream-tts/streamtts/static/en.json`, `stream-tts/check-i18n.py` | `tests/e2e/test_invariants.py`, `tests/e2e/test_admin_smoke.py` |
| В8 | Порог стриминга, как фраза уходит в оверлей кусками | `stream-tts/streamtts/engines/cosyvoice/client.py` (streams), `stream-tts/streamtts/engines/espeech/client.py` (STREAM_MIN_SEC), `stream-tts/streamtts/engines/supertonic/client.py` (STREAM_MIN_SEC), `stream-tts/streamtts/engines/client.py` (stream), `stream-tts/streamtts/engine.py` (_synth, _play), `stream-tts/streamtts/overlay.py` (Overlay.active), `stream-tts/streamtts/static/overlay.html` | `stream-tts/streamtts/engines/cosyvoice/client.py` (streams), `stream-tts/streamtts/engines/espeech/client.py` (STREAM_MIN_SEC), `stream-tts/streamtts/engines/client.py` (stream), `stream-tts/streamtts/engine.py` (_synth, _play), `stream-tts/streamtts/static/overlay.html` | `tests/e2e/test_queue.py`, `tests/e2e/test_engines.py` |
| В9 | Действие с перезапуском встроенного сервера без потери обучения LoRA | `stream-tts/streamtts/web/common.py` (_restart_guard), `stream-tts/streamtts/engines/espeech/train.py` (Training.guard), `stream-tts/streamtts/static/admin.js` (api: confirm, force), `tests/e2e/test_train.py` (RESTARTS, KEEPS, SKIPS) | `stream-tts/streamtts/web.py` (_restart_guard), `stream-tts/streamtts/engines/espeech/train.py` (Training.guard), `stream-tts/streamtts/static/admin.js` (confirm, force), `tests/e2e/test_train.py` (RESTARTS, KEEPS, SKIPS) | `tests/e2e/test_train.py` |
| З1 | Новая настройка stream-tts в админке | `stream-tts/streamtts/store.py` (умолчания), `stream-tts/streamtts/web/admin.py` (SETTINGS_KEYS, post_settings), `stream-tts/streamtts/static/admin.html`, `stream-tts/streamtts/static/admin-settings.js`; строки — В7 | `stream-tts/streamtts/store.py` (умолчания), `stream-tts/streamtts/web.py` (SETTINGS_KEYS), `stream-tts/streamtts/static/admin.html`, `stream-tts/streamtts/static/admin.js` | `tests/e2e/test_access.py` |
| З2 | Новый POST-роут админки | `stream-tts/streamtts/web/__init__.py` (MODULES, POSTS), `stream-tts/streamtts/web/common.py` (json_post, _restart_guard), `stream-tts/streamtts/web/admin.py` (POSTS — таблица модуля), `stream-tts/streamtts/static/admin.js` (api), `tests/e2e/test_train.py` (группа роута) | `stream-tts/streamtts/web.py` (make_app, POSTS), `stream-tts/streamtts/static/admin.js`, `tests/e2e/test_train.py` (группа роута) | `tests/e2e/test_train.py`, `tests/e2e/test_routes.py` |
| З3 | Новый запрос к серверу синтеза | `stream-tts/streamtts/engines/client.py`, `espeech/server.py`, `tests/e2e/fake_cosyvoice.py` (все режимы) | `stream-tts/streamtts/engines/client.py`, `espeech/server.py`, `tests/e2e/fake_cosyvoice.py` (оба режима) | `tests/e2e/test_engines.py` |
| З4 | Фильтр текста (ссылки, мат, ники) | `stream-tts/streamtts/filters.py`, `stream-tts/streamtts/engine.py` (chat_message) | = | `tests/e2e/test_queue.py` |
| З5 | Новое поле события чата | `stream-tts/streamtts/twitch/eventsub.py` (_event), `stream-tts/streamtts/chat_irc.py` (теги IRC), `stream-tts/streamtts/engine.py`, `tests/e2e/fake_irc.py`, `tests/e2e/fake_twitch.py` (say) | `stream-tts/streamtts/twitch.py` (EventSub), `stream-tts/streamtts/chat_irc.py` (теги IRC), `stream-tts/streamtts/engine.py`, `tests/e2e/fake_irc.py` | `tests/e2e/test_chat.py`, `tests/e2e/test_twitch.py` |
| З6 | Редактор голоса (новый голос из записи) | `stream-tts/streamtts/editor.py`, `stream-tts/streamtts/engines/cosyvoice/editor.py`, `stream-tts/streamtts/engines/espeech/editor.py`, `stream-tts/streamtts/engines/supertonic/editor.py` (отказ), `stream-tts/streamtts/static/editor.js` | `stream-tts/streamtts/editor.py`, `stream-tts/streamtts/engines/cosyvoice/editor.py`, `stream-tts/streamtts/engines/espeech/editor.py`, `stream-tts/streamtts/static/editor.js` | `tests/e2e/test_editor.py`, `tests/e2e/test_editor_espeech.py` |
| З7 | Голоса: список, группы наборов, примеры | `stream-tts/streamtts/voices.py` (GROUPS) | = | `tests/e2e/test_voices.py` |
| З8 | Статистика и база SQLite | `stream-tts/streamtts/stats.py`, `stream-tts/streamtts/store.py` | = | `tests/e2e/test_stats.py` |
| З9 | Загрузки: CUDA, кванты, пакеты | `stream-tts/streamtts/downloads.json`, `stream-tts/streamtts/downloads.py` (CUDA, кванты, PACKAGES), `stream-tts/streamtts/install.py` (Installer), `stream-tts/streamtts/fetch.py` (докачка, sha256) | `stream-tts/streamtts/downloads.json`, `stream-tts/streamtts/install.py` | `tests/e2e/test_cuda_download.py`, `tests/e2e/test_espeech_download.py`, `tests/e2e/test_supertonic_download.py`, `tests/e2e/test_train.py` (RESTARTS) |
| З10 | Нормализация текста ESpeech (числа, латиница, ники) | `espeech/normalize.py` (normalize), `espeech/numbers.py`, `espeech/latin.py`, `espeech/pipeline.py` | `espeech/normalize.py`, `espeech/pipeline.py` | `tests/espeech/test_text.py`, `tests/espeech/test_normalize_corpus.py` |
| З11 | Обучение LoRA голоса ESpeech | `stream-tts/streamtts/engines/espeech/train.py`, `stream-tts/streamtts/static/train.js`, `espeech/trainjob.py`, `espeech/train.py` | = | `tests/e2e/test_train.py`, `tests/espeech/test_train_api.py` |
| З12 | Загрузка ESpeech по кнопке: рантайм torch и модель | `stream-tts/streamtts/engines/espeech/download.py` (Steps), `stream-tts/streamtts/partzip.py`, `stream-tts/streamtts/fetch.py`, `packaging/pin-espeech.py` (закрепления пишет в downloads.json) | `stream-tts/streamtts/engines/espeech/download.py`, `stream-tts/streamtts/install.py`, `stream-tts/streamtts/partzip.py`, `packaging/pin-espeech.py` | `tests/e2e/test_espeech_download.py` |
| З13 | Twitch-приложение: вход, награда, статусы погашений, EventSub | `stream-tts/streamtts/twitch/auth.py` (begin_login, refresh), `stream-tts/streamtts/twitch/helix.py` (api, save_reward, set_status), `stream-tts/streamtts/twitch/eventsub.py` (_eventsub), `stream-tts/streamtts/web/admin.py` (post_login, post_reward), `stream-tts/streamtts/engine.py` (redemption, resolve), `stream-tts/streamtts/config.py` (TEST_TWITCH), `tests/e2e/fake_twitch.py` | `stream-tts/streamtts/twitch.py`, `stream-tts/streamtts/web.py` (post_login, post_reward), `stream-tts/streamtts/engine.py` (redemption, resolve) | `tests/e2e/test_twitch.py` |
| З14 | Supertonic 3 у пользователя: загрузка модели по кнопке, готовые голоса, запуск сервера программой для Windows и в Docker | `stream-tts/streamtts/engines/supertonic/download.py` (VOICES, download, install_voices, remove), `stream-tts/streamtts/engines/supertonic/launch.py` (find_server, FLAG, CODE), `stream-tts/streamtts/install.py` (start_supertonic), `stream-tts/streamtts/downloads.json` (supertonic), `stream-tts/streamtts/static/admin-server.js` (renderSupertonic), `packaging/windows/StreamTTS.py` (--supertonic), `packaging/windows/build-exe.py` (ESPEECH_MODULES), `Dockerfile` | `stream-tts/streamtts/install.py`, `stream-tts/streamtts/downloads.json` | `tests/e2e/test_supertonic_download.py` |
| З15 | Страница голосов в интернете: туннель Cloudflare, порт только со страницей, `!ttsvoices` | `stream-tts/streamtts/tunnel.py` (Tunnel, binary), `stream-tts/streamtts/web/public.py` (start), `stream-tts/streamtts/commands.py` (voices_link), `stream-tts/streamtts/static/admin-queue.js` (renderTunnel), `stream-tts/streamtts/downloads.json` (cloudflared), `tests/e2e/fake_cloudflared.py` | `stream-tts/streamtts/voices.py` (page), `stream-tts/streamtts/web.py` (voices_page) | `tests/e2e/test_tunnel.py` |
| З16 | Доступ к админке: пароль из админки, без пароля — только локальная сеть | `stream-tts/streamtts/access.py` (local_request, local_host, check, set_password), `stream-tts/streamtts/web/__init__.py` (auth_middleware, post_password, token), `stream-tts/streamtts/static/admin-settings.js` (renderAccess), `stream-tts/streamtts/static/admin.js` (authChanged), `stream-tts/streamtts/static/login.html`, `docker-compose.yml` (STREAM_TTS_BIND), `setup.sh` | `stream-tts/streamtts/web.py` (auth_middleware, login) | `tests/e2e/test_access.py` |

**Перевод строки (В7, а с ним В1, З1 и любая правка текста интерфейса).** Словарь
`stream-tts/streamtts/static/en.json` — ~19k токенов, целиком его не читать. `python stream-tts/check-i18n.py
--missing` печатает недостающие ключи строками `"ключ": ""` с отступом словаря: вписать переводы и вставить в конец
en.json правкой по последней строке (`tail -n 2` — последний ключ и `}`; последнему ключу — запятая). Затем
`python stream-tts/check-i18n.py` без флагов: ошибка — и ключ без перевода, и лишний ключ, строки которого нет ни в
одном источнике (строку убрали из кода — убрать и из en.json; новый источник строк — научить ему check-i18n).

## Куда смотреть

| Путь | Что | Документ |
|---|---|---|
| `stream-tts/streamtts/` | stream-tts на :8090: очередь (`engine.py`; правила приёма — `rules.py`, команды чата — `commands.py`, оверлеи — `overlay.py`), HTTP и админка (`web/` — роуты по модулям, `static/` — `admin.js` и `admin-<вкладка>.js`), Twitch (`twitch/` — вход, Helix, EventSub, `chat_irc.py`), голоса (`voices.py`), наборы (`voicesets.py`), загрузки (`install.py`, `downloads.py`, `fetch.py`), редактор (`editor.py`), встроенный сервер (`runtime.py`) | [stream-tts.md](stream-tts.md), [stream-tts-deploy.md](stream-tts-deploy.md) |
| `stream-tts/streamtts/engines/` | модели синтеза: `cosyvoice/`, `espeech/`, `supertonic/` — описание, запуск, клиент, особенности редактора; `client.py` — общий протокол и очередь к серверу | [stream-tts.md](stream-tts.md), «Модели синтеза» |
| `espeech/` | espeech-server: синтез ESpeech (F5-TTS + RUAccent), HTTP API как у cosyvoice-server, обучение LoRA | [espeech.md](espeech.md) |
| `supertonic/` | supertonic-server: синтез Supertonic 3 на onnxruntime, только CPU, 10 готовых голосов; тесты — `tests/supertonic/` | [supertonic.md](supertonic.md) |
| `setup.sh`, `Dockerfile`, `docker-compose.yml` | Docker: stream-tts со встроенным cosyvoice-server, он же сервер для `scripts/` и `tools/` (порт 8080); образ из готовых релизов cosyvoice.cpp и llama.cpp, код сервера не наш; сборка в CI — `docker.yml` в `.github/workflows/` (после тестов на push в main, `ci.yml`) | [development.md](development.md), «Образы» |
| `packaging/windows/` | программа (Nuitka) и установщик (Inno Setup 6.5+); CI — `.github/workflows/windows.yml` (после тестов на push в main и тег, `ci.yml`); `stubs/*.def` — DLL-заглушки torch ESpeech; `.venv`, `.inno`, `build/`, `dist/` — не в git | [stream-tts-windows.md](stream-tts-windows.md) |
| `packaging/pin-espeech.py`, `packaging/linux/` | загрузки ESpeech для обеих систем, заглушки библиотек torch | [espeech.md](espeech.md), «Поставка» |
| `scripts/` | загрузка модели, импорт голосов в `voices/*.gguf`, архив для внешнего исполнителя, этот замер | [voice-pipeline.md](voice-pipeline.md) |
| `tools/voices/`, `tools/wc3/`, `tools/sources/` | подбор образцов: страница выбора, синтез, оценки, коллекции наборов; реплики WC3 и других источников | [voice-pipeline.md](voice-pipeline.md), [voices-wc3.md](voices-wc3.md), [voices.md](voices.md) |
| `tools/espeech/` | папка модели ESpeech, голоса `.espeech`, обучение LoRA из командной строки | [espeech.md](espeech.md) |
| `effects/` | эффекты поверх синтеза (`post`) | [stream-tts.md](stream-tts.md), «Синтез и воспроизведение» |
| `tests/e2e/`, `tests/espeech/` | e2e stream-tts на поддельных сервере синтеза, IRC и Twitch; тесты espeech-server; в CI на push в main и PR в main — `tests.yml` в `.github/workflows/` (из `ci.yml`), на dev — локально | [development.md](development.md), «Тесты» |

## Данные (не в git)

Раскладка голосов — `<модель>/<набор>/<голос>`: модель — `cosyvoice` или `espeech` (id из
`stream-tts/streamtts/engines/`), набор — источник: `wc3` (`wc3-*`), `morrowind` (`mw-*`, только ESpeech), игры по
одной (`dota2`, `portal`, `stalker`, `postal`, `minecraft`, `neco-arc`, `altushka`), `streamers`. Каталог —
[voices.md](voices.md).

| Путь | Что |
|---|---|
| `voice-clips/<модель>/<набор>/` | исходные данные голосов (тексты реплик из игр): `cosyvoice/` — варианты образцов, синтез, оценки и выбор; `espeech/` — реплики для LoRA, адаптеры и голоса `.espeech` |
| `voice-refs/cosyvoice/<набор>/` | итоговые образцы CosyVoice и `manifest.json` |
| `voice-sets/<модель>/<набор>.zip` | коллекции для импорта в stream-tts, по одной на «модель + набор» (`tools/voices/pack-sets.py`) |
| `voices/`, `models/`, `voice-origs/`, `samples/`, `stream-tts-data/` | установленные голоса, модели, ассеты игр и записи с YouTube, исследования, данные stream-tts |
| `runtime/`, `espeech-runtime/`, `.env`, `.venv*` | нативный cosyvoice-server для Windows, Python с torch для ESpeech в Docker, секреты, окружения Python |

## Связи, которые легко порвать

- **`tools/`** грузят по пути `tools/common.py`, а соседние модули — через `common.load("tools/<группа>/<файл>.py")`
  (корень — `common.ROOT`): перед переносом или переименованием — `grep -r 'common.load(' tools`. Свои копии функций
  `common.py` в `tools/` ловит `tests/e2e/test_tools_common.py`.
- **Имя набора** — имя папки источника в `voice-clips/<модель>/`, `voice-refs/cosyvoice/`,
  `voice-sets/<модель>/<набор>.zip` и установленного набора `voices/<набор>/`; по нему stream-tts группирует страницу
  голосов (GROUPS в `stream-tts/streamtts/voices.py`). Голоса обеих моделей одного набора лежат в одной папке
  `voices/<набор>/`, а коллекцию из zip stream-tts ставит и удаляет по модели (`stream-tts/streamtts/voicesets.py`).
  Инструменты знают свою модель (`pick.py` — `voice-clips/cosyvoice`, `lora-clips.py` — `voice-clips/espeech/wc3`);
  кеши `voice-clips/*.json` общие, их ключи — пути от корня: после переноса папок их надо переписать.
- **`post`** в `alts.json` и `manifest.json` — `[скрипт, аргументы…]`, путь от корня. stream-tts ищет скрипт в
  `STREAM_TTS_PROJECT` (в контейнере `/project`), грузит через importlib и вызывает `apply(звук, аргументы)`;
  командная строка эффекта нужна `tools/voices/synth.py`.
- **Модели синтеза** — `engines/<id>/` (описание, запуск, клиент), одна единовременно (`settings.engine`). Ядро зовёт
  у клиента только lane/synth/stream/streams/stop/speakers/ok/status. Поле запроса модели — в `params()` её клиента,
  особенности редактора голоса — в `Maker` (`engines/<id>/editor.py`), словарь ударений хранит сервер модели
  (`/accents`), настройка модели — схемой `params` в `/status` сервера (`SETTINGS` клиента), новый запрос к серверу —
  и в `tests/e2e/fake_cosyvoice.py` (все режимы). Голос без файла для текущей модели (`.gguf`/`.espeech`/`.supertonic`)
  зрителям не выдаётся, но `users.voice` не перезаписывается; примеры и `calib_db` — свои у каждой модели
  (`voices.py`). Модель без голоса из записи (Supertonic 3) — `Maker.EXTRACT = False`: редактор отказывает, кнопки
  «Добавить голос» нет; с одним устройством — `DEVICE_KEY` пустой, выбора устройства нет.
- **supertonic-server и `espeech/`**: сервер импортирует нормализатор и WAV из `espeech/` (чистый Python). У программы
  для Windows они скомпилированы в StreamTTS.exe (`ESPEECH_MODULES` и `--include-package=supertonic` в
  `packaging/windows/build-exe.py`; сервер — `StreamTTS.exe --supertonic`, `packaging/windows/StreamTTS.py`), в Docker
  код лежит в `/opt/cosyvoice/supertonic` и `/opt/cosyvoice/espeech` (`Dockerfile`, `.dockerignore`). Новый импорт из
  `espeech/` в `supertonic/` — в `ESPEECH_MODULES` (держит `tests/e2e/test_supertonic_download.py`); имена готовых
  голосов — `VOICES` в `engines/supertonic/download.py`: переименование оставит у пользователей старые файлы.
- **Слои stream-tts**: `aiohttp.web` — только в `web/` и `__main__.py` (оверлей и очередь от веб-сервера не зависят);
  `engines/` не импортируют слои над собой (веб, загрузки и установку, редактор, ядро, чат — ABOVE_ENGINES);
  циклов импорта нет, и ленивых внутри функций тоже. Держит `tests/e2e/test_layers.py`: общий код для движков и
  слоя выше — в модуль ниже обоих (как `fetch.py`), а не ленивым импортом.
- **`Engine.chat`** принимает событие в форме EventSub `channel.chat.message`; режим «Только чтение чата»
  (`chat_irc.py`) собирает такое же из тегов IRC: новое поле события — в обоих местах, новый тег IRC — и в
  `tests/e2e/fake_irc.py`, новое поле EventSub — и в `tests/e2e/fake_twitch.py`. Новый запрос `twitch/` к Twitch —
  и в `fake_twitch.py`: иначе `tests/e2e/test_twitch.py` получит 404.
