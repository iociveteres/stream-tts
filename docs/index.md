# Навигация: задача → файлы

Что открыть под типовую задачу; ниже — области, данные и связи между частями. Пути — от корня проекта, в скобках —
что искать в файле. «На базе» — пути на
коммите до рефакторинга (База: `939f37a`), «=» — те же. Замер файлов и токенов по таблице —
`scripts/measure-docs.py`, что пути живы и вопросы на месте — `tests/e2e/test_index.py`.

В — эталонные вопросы ([refactor-brief.md](refactor-brief.md), §3), З — частые задачи.

| № | Задача | Файлы сейчас | На базе | Тест |
|---|---|---|---|---|
| В1 | Где обрабатывается команда чата `!tts`, как добавить новую | `stream-tts/streamtts/engine.py` (Engine.chat, SERVICE_COMMANDS), `stream-tts/streamtts/chat_irc.py` (событие из IRC), `stream-tts/streamtts/static/en.json` (ответы) | = | `tests/e2e/test_chat.py` |
| В2 | Третий движок синтеза: что реализовать, где регистрируется | `stream-tts/streamtts/engines/__init__.py` (ENGINES, Tts), `stream-tts/streamtts/engines/client.py` (протокол, Client), `stream-tts/streamtts/engines/espeech/__init__.py` (описание), `stream-tts/streamtts/engines/espeech/client.py`, `stream-tts/streamtts/engines/espeech/launch.py` (Launcher, find_server), `stream-tts/streamtts/engines/espeech/editor.py` (Maker), `tests/e2e/fake_cosyvoice.py` | = | `tests/e2e/test_engines.py` |
| В3 | Настройка из админки → сервер модели (схема `params`) | `espeech/server.py` (/status, params), `stream-tts/streamtts/engines/client.py` (SETTINGS, schema), `stream-tts/streamtts/engines/espeech/client.py` (params()), `stream-tts/streamtts/web/__init__.py` (model_params, post_settings), `stream-tts/streamtts/store.py` (умолчания), `stream-tts/streamtts/static/admin.js` (#model-params) | `espeech/server.py` (/status, params), `stream-tts/streamtts/engines/client.py` (SETTINGS, schema), `stream-tts/streamtts/engines/espeech/client.py` (params()), `stream-tts/streamtts/web.py` (model_params, post_settings), `stream-tts/streamtts/store.py` (умолчания), `stream-tts/streamtts/static/admin.js` (#model-params) | `tests/e2e/test_engines.py` |
| В4 | Эффект `post`: как добавить, почему скрипт не из `effects/` не запускается | `stream-tts/streamtts/voices.py` (apply_post, load_effect), `effects/villager-fx.py` (apply), `stream-tts/streamtts/paths.py` (поиск в поставке) | = | `tests/e2e/test_voices.py` |
| В5 | «Server disconnected» у фразы после паузы | `stream-tts/streamtts/engines/client.py` (KEEPALIVE) | = | `tests/e2e/test_invariants.py` |
| В6 | Набор голосов из zip и что при этом со встроенным сервером | `stream-tts/streamtts/install.py` (Installer, _check_zip, _install_set), `stream-tts/streamtts/runtime.py` (paused), `stream-tts/streamtts/web/__init__.py` (api_voiceset_import) | `stream-tts/streamtts/install.py` (Installer, _check_zip, _install_set), `stream-tts/streamtts/runtime.py` (paused), `stream-tts/streamtts/web.py` (api_voiceset_import) | `tests/e2e/test_voicesets.py`, `tests/e2e/test_train.py` |
| В7 | Строка интерфейса с переводом на английский | `stream-tts/streamtts/i18n.py` (tr), `stream-tts/streamtts/static/i18n.js` (T, data-t), `stream-tts/streamtts/static/en.json`, `stream-tts/check-i18n.py` | = | `tests/e2e/test_invariants.py` |
| В8 | Порог стриминга, как фраза уходит в оверлей кусками | `stream-tts/streamtts/engines/cosyvoice/client.py` (streams), `stream-tts/streamtts/engines/espeech/client.py` (STREAM_MIN_SEC), `stream-tts/streamtts/engines/client.py` (stream), `stream-tts/streamtts/engine.py` (_synth, _play), `stream-tts/streamtts/static/overlay.html` | = | `tests/e2e/test_queue.py`, `tests/e2e/test_engines.py` |
| В9 | Действие с перезапуском встроенного сервера без потери обучения LoRA | `stream-tts/streamtts/web/common.py` (_restart_guard), `stream-tts/streamtts/engines/espeech/train.py` (Training.guard), `stream-tts/streamtts/static/admin.js` (confirm, force), `tests/e2e/test_train.py` (RESTARTS, KEEPS, SKIPS) | `stream-tts/streamtts/web.py` (_restart_guard), `stream-tts/streamtts/engines/espeech/train.py` (Training.guard), `stream-tts/streamtts/static/admin.js` (confirm, force), `tests/e2e/test_train.py` (RESTARTS, KEEPS, SKIPS) | `tests/e2e/test_train.py` |
| З1 | Новая настройка stream-tts в админке | `stream-tts/streamtts/store.py` (умолчания), `stream-tts/streamtts/web/__init__.py` (SETTINGS_KEYS), `stream-tts/streamtts/static/admin.html`, `stream-tts/streamtts/static/admin.js`, `stream-tts/streamtts/static/en.json` | `stream-tts/streamtts/store.py` (умолчания), `stream-tts/streamtts/web.py` (SETTINGS_KEYS), `stream-tts/streamtts/static/admin.html`, `stream-tts/streamtts/static/admin.js`, `stream-tts/streamtts/static/en.json` | `tests/e2e/test_access.py` |
| З2 | Новый POST-роут админки | `stream-tts/streamtts/web/__init__.py` (make_app, POSTS), `stream-tts/streamtts/static/admin.js`, `tests/e2e/test_train.py` (группа роута) | `stream-tts/streamtts/web.py` (make_app, POSTS), `stream-tts/streamtts/static/admin.js`, `tests/e2e/test_train.py` (группа роута) | `tests/e2e/test_train.py` |
| З3 | Новый запрос к серверу синтеза | `stream-tts/streamtts/engines/client.py`, `espeech/server.py`, `tests/e2e/fake_cosyvoice.py` (оба режима) | = | `tests/e2e/test_engines.py` |
| З4 | Фильтр текста (ссылки, мат, ники) | `stream-tts/streamtts/filters.py`, `stream-tts/streamtts/engine.py` (chat_message) | = | `tests/e2e/test_queue.py` |
| З5 | Новое поле события чата | `stream-tts/streamtts/twitch.py` (EventSub), `stream-tts/streamtts/chat_irc.py` (теги IRC), `stream-tts/streamtts/engine.py`, `tests/e2e/fake_irc.py` | = | `tests/e2e/test_chat.py` |
| З6 | Редактор голоса (новый голос из записи) | `stream-tts/streamtts/editor.py`, `stream-tts/streamtts/engines/cosyvoice/editor.py`, `stream-tts/streamtts/engines/espeech/editor.py`, `stream-tts/streamtts/static/editor.js` | = | `tests/e2e/test_editor.py`, `tests/e2e/test_editor_espeech.py` |
| З7 | Голоса: список, группы наборов, примеры | `stream-tts/streamtts/voices.py` (GROUPS) | = | `tests/e2e/test_voices.py` |
| З8 | Статистика и база SQLite | `stream-tts/streamtts/stats.py`, `stream-tts/streamtts/store.py` | = | `tests/e2e/test_stats.py` |
| З9 | Загрузки: CUDA, кванты, ESpeech | `stream-tts/streamtts/downloads.json`, `stream-tts/streamtts/install.py`, `stream-tts/streamtts/engines/espeech/download.py`, `stream-tts/streamtts/partzip.py`, `packaging/pin-espeech.py` | = | `tests/e2e/test_espeech_download.py` |
| З10 | Нормализация текста ESpeech (числа, латиница, ники) | `espeech/normalize.py`, `espeech/pipeline.py` | = | `tests/espeech/test_text.py` |
| З11 | Обучение LoRA голоса ESpeech | `stream-tts/streamtts/engines/espeech/train.py`, `stream-tts/streamtts/static/train.js`, `espeech/trainjob.py`, `espeech/train.py` | = | `tests/e2e/test_train.py`, `tests/espeech/test_train_api.py` |

## Куда смотреть

| Путь | Что | Документ |
|---|---|---|
| `stream-tts/streamtts/` | stream-tts на :8090: очередь (`engine.py`), HTTP и админка (`web.py`, `static/`), Twitch (`twitch.py`, `chat_irc.py`), голоса (`voices.py`), наборы и загрузки (`install.py`), редактор (`editor.py`), встроенный сервер (`runtime.py`) | [stream-tts.md](stream-tts.md), [stream-tts-deploy.md](stream-tts-deploy.md) |
| `stream-tts/streamtts/engines/` | модели синтеза: `cosyvoice/`, `espeech/` — описание, запуск, клиент, особенности редактора; `client.py` — общий протокол и очередь к серверу | [stream-tts.md](stream-tts.md), «Модели синтеза» |
| `espeech/` | espeech-server: синтез ESpeech (F5-TTS + RUAccent), HTTP API как у cosyvoice-server, обучение LoRA | [espeech.md](espeech.md) |
| `setup.sh`, `Dockerfile`, `docker-compose.yml`, `cosyvoice/` | Docker: stream-tts со встроенным cosyvoice-server; профиль `server-only` — только сервер для `scripts/` и `tools/` (`cosyvoice/entrypoint.sh`); образы из готовых релизов cosyvoice.cpp и llama.cpp, код сервера не наш | [development.md](development.md), «Образы» |
| `packaging/windows/` | программа (Nuitka) и установщик (Inno Setup 6.5+); CI — `.github/workflows/windows.yml`; `stubs/*.def` — DLL-заглушки torch ESpeech; `.venv`, `.inno`, `build/`, `dist/` — не в git | [stream-tts-windows.md](stream-tts-windows.md) |
| `packaging/pin-espeech.py`, `packaging/linux/` | загрузки ESpeech для обеих систем, заглушки библиотек torch | [espeech.md](espeech.md), «Поставка» |
| `scripts/` | загрузка модели, импорт голосов в `voices/*.gguf`, архив для внешнего исполнителя, этот замер | [voice-pipeline.md](voice-pipeline.md) |
| `tools/voices/`, `tools/wc3/`, `tools/sources/` | подбор образцов: страница выбора, синтез, оценки, коллекции наборов; реплики WC3 и других источников | [voice-pipeline.md](voice-pipeline.md), [voices-wc3.md](voices-wc3.md), [voices.md](voices.md) |
| `tools/espeech/` | папка модели ESpeech, голоса `.espeech`, обучение LoRA из командной строки | [espeech.md](espeech.md) |
| `effects/` | эффекты поверх синтеза (`post`) | [stream-tts.md](stream-tts.md), «Синтез и воспроизведение» |
| `tests/e2e/`, `tests/espeech/` | e2e stream-tts на поддельном сервере; тесты espeech-server | [development.md](development.md), «Тесты» |

## Данные (не в git)

Раскладка голосов — `<модель>/<набор>/<голос>`: модель — `cosyvoice` или `espeech` (id из
`stream-tts/streamtts/engines/`), набор — источник: `wc3` (`wc3-*`), `morrowind` (`mw-*`, только ESpeech), игры по
одной (`dota2`, `portal`, `stalker`, `minecraft`, `neco-arc`, `altushka`), `streamers`. Каталог — [voices.md](voices.md).

| Путь | Что |
|---|---|
| `voice-clips/<модель>/<набор>/` | исходные данные голосов (тексты реплик из игр): `cosyvoice/` — варианты образцов, синтез, оценки и выбор; `espeech/` — реплики для LoRA, адаптеры и голоса `.espeech` |
| `voice-refs/cosyvoice/<набор>/` | итоговые образцы CosyVoice и `manifest.json` |
| `voice-sets/<модель>/<набор>.zip` | коллекции для импорта в stream-tts, по одной на «модель + набор» (`tools/voices/pack-sets.py`) |
| `voices/`, `models/`, `voice-origs/`, `samples/`, `stream-tts-data/` | установленные голоса, модели, ассеты игр и записи с YouTube, исследования, данные stream-tts |
| `runtime/`, `espeech-runtime/`, `.env`, `.venv*` | нативный cosyvoice-server для Windows, Python с torch для ESpeech в Docker, секреты, окружения Python |

## Связи, которые легко порвать

- **`tools/`** считают корень как `ROOT = dirname(dirname(HERE))` и грузят соседние модули по пути
  (`spec_from_file_location`): перед переносом или переименованием — `grep spec_from_file_location`.
- **Имя набора** — имя папки источника в `voice-clips/<модель>/`, `voice-refs/cosyvoice/`,
  `voice-sets/<модель>/<набор>.zip` и установленного набора `voices/<набор>/`; по нему stream-tts группирует страницу
  голосов (GROUPS в `stream-tts/streamtts/voices.py`). Голоса обеих моделей одного набора лежат в одной папке
  `voices/<набор>/`, а коллекцию из zip stream-tts ставит и удаляет по модели (`stream-tts/streamtts/install.py`).
  Инструменты знают свою модель (`pick.py` — `voice-clips/cosyvoice`, `lora-clips.py` — `voice-clips/espeech/wc3`);
  кеши `voice-clips/*.json` общие, их ключи — пути от корня: после переноса папок их надо переписать.
- **`post`** в `alts.json` и `manifest.json` — `[скрипт, аргументы…]`, путь от корня. stream-tts ищет скрипт в
  `STREAM_TTS_PROJECT` (в контейнере `/project`), грузит через importlib и вызывает `apply(звук, аргументы)`;
  командная строка эффекта нужна `tools/voices/synth.py`.
- **Модели синтеза** — `engines/<id>/` (описание, запуск, клиент), одна единовременно (`settings.engine`). Ядро зовёт
  у клиента только lane/synth/stream/streams/stop/speakers/ok/status. Поле запроса модели — в `params()` её клиента,
  особенности редактора голоса — в `Maker` (`engines/<id>/editor.py`), словарь ударений хранит сервер модели
  (`/accents`), настройка модели — схемой `params` в `/status` сервера (`SETTINGS` клиента), новый запрос к серверу —
  и в `tests/e2e/fake_cosyvoice.py` (оба режима). Голос без файла для текущей модели (`.gguf`/`.espeech`) зрителям не
  выдаётся, но `users.voice` не перезаписывается; примеры и `calib_db` — свои у каждой модели (`voices.py`).
- **`Engine.chat`** принимает событие в форме EventSub `channel.chat.message`; режим «Только чтение чата»
  (`chat_irc.py`) собирает такое же из тегов IRC: новое поле события — в обоих местах, новый тег IRC — и в
  `tests/e2e/fake_irc.py`.
