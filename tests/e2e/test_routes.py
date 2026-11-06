"""Снимок адресов stream-tts: все пары «метод + путь» из самого приложения (web.make_app) без процесса, секунды.

URL — публичное имя (docs/refactor-brief.md, §5): по ним ходят админка, оверлей, страница голосов, редактор и
обучение. Перенос роутов между модулями web/ не должен потерять, переименовать или добавить ни одного роута. Новый роут —
осознанно: добавить его сюда (а POST /api/* — ещё и в группу в test_train.py, test_every_route_guarded)."""
from harness import make_app

ROUTES = {
    "GET /",
    "GET /login", "POST /login", "GET /logout",
    "GET /overlay", "GET /overlay/ws",
    "GET /voices", "GET /voices/", "GET /voices/demos/{name}.mp3", "GET /voices.zip",
    "GET /static",
    # состояние и чтение
    "GET /api/state", "GET /api/ws", "GET /api/stats", "GET /api/preview", "GET /api/profanity", "GET /api/accents",
    # админка
    "POST /api/settings", "POST /api/source", "POST /api/voice", "POST /api/demos/rebuild",
    "POST /api/twitch/client_id", "POST /api/twitch/mode", "POST /api/twitch/login", "POST /api/twitch/logout",
    "POST /api/reward", "POST /api/reward/delete",
    "POST /api/enabled", "POST /api/skip", "POST /api/clear", "POST /api/remove", "POST /api/test", "POST /api/ban",
    "POST /api/profanity",
    # модель синтеза и встроенный сервер
    "POST /api/download", "POST /api/download/unqueue", "POST /api/download/cancel", "POST /api/download/delete",
    "POST /api/model/use", "POST /api/backend",
    "POST /api/engine", "POST /api/accent", "POST /api/accents", "POST /api/accents/delete", "POST /api/bench",
    "POST /api/voicesets/delete", "POST /api/voicesets/import",
    "POST /api/models/unload", "POST /api/models/load", "POST /api/update/check", "POST /api/update/dismiss",
    # редактор голоса
    "GET /api/draft", "POST /api/draft", "GET /api/draft/source/{id}.wav", "GET /api/draft/sample",
    "GET /api/draft/synth", "POST /api/draft/upload", "POST /api/draft/accent", "POST /api/draft/source/delete",
    "POST /api/draft/save", "POST /api/draft/discard",
    # обучение LoRA
    "GET /api/train", "GET /api/train/status", "GET /api/train/item", "GET /api/train/source",
    "GET /api/train/synth", "POST /api/train/upload", "POST /api/train/items", "POST /api/train/items/delete",
    "POST /api/train/cut", "POST /api/train/source/delete", "POST /api/train/clear", "POST /api/train/start",
    "POST /api/train/stop", "POST /api/train/save", "POST /api/train/discard",
}
# Только в режиме правки строк (STREAM_TTS_UI_EDITS)
UI_EDIT = "POST /api/ui-edit"


def test_routes_unchanged():
    _, app = make_app()
    from streamtts import i18n
    routes = [f"{r.method} {r.resource.canonical}" for r in app.router.routes() if r.method != "HEAD"]
    assert len(routes) == len(set(routes)), "роут добавлен дважды"
    want = ROUTES | ({UI_EDIT} if i18n.EDITS_PATH else set())
    assert not set(routes) - want, f"новые роуты: {sorted(set(routes) - want)}"
    assert not want - set(routes), f"роутов больше нет: {sorted(want - set(routes))}"


def test_uploads_are_routes():
    """UPLOADS (сырое тело вместо JSON) — только у существующих POST-роутов."""
    web, app = make_app()
    posts = {r.resource.canonical for r in app.router.routes() if r.method == "POST"}
    assert set(web.UPLOADS) <= posts, set(web.UPLOADS) - posts
