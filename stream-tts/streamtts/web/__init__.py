"""HTTP: админка (под паролем), оверлей и страница голосов (открыты), API и WebSocket.

Роуты API — в модулях пакета (MODULES): у модуля таблицы GETS, POSTS (тело — JSON, обёртка common.json_post) и RAW
(файл сырым телом, допустимые типы — в UPLOADS). Здесь — сборка приложения, пароль и защита POST, страницы."""
import asyncio, hashlib, hmac, json, os, secrets

from aiohttp import WSMsgType, web

from .. import config, i18n, overlay
from ..i18n import tr
from . import admin, editor, model, server, state, train
from .common import json_post

STATIC = os.path.join(config.HERE, "static")
COOKIE = "stream_tts"
PUBLIC = ("/overlay", "/voices", "/static/overlay", "/static/common", "/static/i18n.js", "/login", "/favicon")
# Файлы, которые меняются под тем же адресом: статика при обновлении программы, примеры голосов при смене фразы
REVALIDATE = ("/static/", "/voices/demos/")
UPLOADS = {"/api/voicesets/import": ("application/zip", "application/x-zip-compressed", "application/octet-stream"),
           "/api/draft/upload": ("application/octet-stream",), "/api/train/upload": ("application/octet-stream",)}


def _secret():
    path = os.path.join(config.DATA, "secret")
    try:
        return open(path).read().strip()
    except OSError:
        s = secrets.token_hex(32)
        with open(path, "w") as f:
            f.write(s)
        return s


def make_app(ctx):
    app = web.Application(middlewares=[auth_middleware], client_max_size=1 << 20)
    app["ctx"] = ctx
    app.on_response_prepare.append(no_cache)
    app["token"] = hmac.new(_secret().encode(), config.PASSWORD.encode(), hashlib.sha256).hexdigest()
    r = app.router
    r.add_get("/", page("admin.html", edit=True))
    r.add_get("/login", page("login.html"))
    r.add_post("/login", login)
    r.add_get("/logout", logout)
    r.add_get("/overlay", page("overlay.html"))
    r.add_get("/overlay/ws", overlay_ws)
    r.add_get("/voices", lambda req: web.HTTPFound("/voices/"))
    r.add_get("/voices/", voices_page)
    r.add_get("/voices/demos/{name}.mp3", demo)
    r.add_get("/voices.zip", voices_zip)
    r.add_static("/static", STATIC)
    for m in MODULES:
        for path, fn in getattr(m, "GETS", {}).items():
            r.add_get(path, fn)
    for path, fn in POSTS.items():
        r.add_post(path, fn)
    for m in MODULES:
        for path, fn in getattr(m, "RAW", {}).items():
            r.add_post(path, fn)
    if i18n.EDITS_PATH:
        r.add_post("/api/ui-edit", json_post(admin.post_ui_edit))
    return app


async def no_cache(request, response):
    # Без Cache-Control браузер держит файл по эвристике (доля от возраста Last-Modified), то есть днями, и после
    # обновления берёт новую разметку со старыми admin.css/admin.js. no-cache — проверка при каждой загрузке:
    # FileResponse отвечает 304 по ETag (mtime и размер), так что сеть почти не тратится.
    if request.path.startswith(REVALIDATE):
        response.headers["Cache-Control"] = "no-cache"


def page(name, edit=False):
    async def handler(request):
        with open(os.path.join(STATIC, name), encoding="utf-8") as f:
            text = i18n.page(f.read(), edit)
        return web.Response(text=text, content_type="text/html", headers={"Cache-Control": "no-cache"})
    return handler


@web.middleware
async def auth_middleware(request, handler):
    path = request.path
    if config.PASSWORD and not path.startswith(PUBLIC):
        if not hmac.compare_digest(request.cookies.get(COOKIE, ""), request.app["token"]):
            if path.startswith("/api/"):
                raise web.HTTPUnauthorized(text=tr("нужен пароль"))
            raise web.HTTPFound("/login")
    if request.method == "POST" and path.startswith("/api/"):
        # Защита от запросов с чужих страниц: только JSON и только со своего адреса
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:
            raise web.HTTPForbidden(text=tr("чужой Origin"))
        # Загрузка файла — сырым телом с нестандартным типом: такой запрос с чужой страницы требует CORS-проверки
        if request.content_type != "application/json" and request.content_type not in UPLOADS.get(path, ()):
            raise web.HTTPUnsupportedMediaType(text=tr("нужен application/json"))
    return await handler(request)


async def login(request):
    form = await request.post()
    if config.PASSWORD and hmac.compare_digest(str(form.get("password", "")), config.PASSWORD):
        resp = web.HTTPFound("/")
        resp.set_cookie(COOKIE, request.app["token"], max_age=180 * 86400, httponly=True, samesite="Strict")
        raise resp
    await asyncio.sleep(1)
    raise web.HTTPFound("/login?bad=1")


async def logout(request):
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE)
    raise resp


# --- открытые страницы ---
async def overlay_ws(request):
    """Сокет оверлея OBS: здесь транспорт, кто играет и что сыграно — overlay.Overlay."""
    ov = request.app["ctx"].engine.overlay
    ws = web.WebSocketResponse(heartbeat=15)
    await ws.prepare(request)
    c = overlay.Client(ws)
    await ov.connect(c)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            await ov.message(c, json.loads(msg.data))
    finally:
        await ov.disconnect(c)
    return ws


async def voices_page(request):
    return web.Response(text=request.app["ctx"].voices.page(), content_type="text/html",
                        headers={"Cache-Control": "no-cache"})


async def demo(request):
    v = request.app["ctx"].voices
    name = request.match_info["name"]
    if name not in v.names() or not v.has_demo(name):
        raise web.HTTPNotFound()
    return web.FileResponse(v.demo_path(name))


async def voices_zip(request):
    data = await asyncio.to_thread(request.app["ctx"].voices.zip)
    return web.Response(body=data, content_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="tts-voices.zip"'})


MODULES = (admin, state, model, server, editor, train)
POSTS = {path: json_post(fn) for m in MODULES for path, fn in getattr(m, "POSTS", {}).items()}
