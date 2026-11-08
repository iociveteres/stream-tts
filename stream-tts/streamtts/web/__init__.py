"""HTTP: админка (под паролем), оверлей и страница голосов (открыты), API и WebSocket. Страница голосов для туннеля —
на отдельном порту (public.py).

Роуты API — в модулях пакета (MODULES): у модуля таблицы GETS, POSTS (тело — JSON, обёртка common.json_post) и RAW
(файл сырым телом, допустимые типы — в UPLOADS). Здесь — сборка приложения, доступ (пароль, локальная сеть) и защита
POST, страницы. Пароль хранит access.py."""
import asyncio, hashlib, hmac, json, logging, os, secrets, urllib.parse, weakref

from aiohttp import WSCloseCode, WSMsgType, web

from .. import access, config, i18n, overlay
from ..i18n import tr
from . import admin, editor, model, public, server, state, train
from .common import fail, json_post, ok
from .public import demo, voices_page

STATIC = os.path.join(config.HERE, "static")
COOKIE = "stream_tts"
PUBLIC = ("/overlay", "/voices", "/static/overlay", "/static/common", "/static/i18n.js", "/login", "/favicon")
# Файлы, которые меняются под тем же адресом: статика при обновлении программы, примеры голосов при смене фразы
REVALIDATE = ("/static/", "/voices/demos/")
log = logging.getLogger("stream-tts")
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
    app["sockets"] = weakref.WeakSet()
    app["admin_sockets"] = weakref.WeakSet()   # /api/ws: закрываются при смене пароля
    app.on_response_prepare.append(no_cache)
    app.on_response_prepare.append(track_socket)
    app.on_shutdown.append(close_sockets)
    app["token"] = token()
    r = app.router
    r.add_get("/", page("admin.html", edit=True))
    r.add_get("/dock", page("dock.html"))   # док-панель OBS: управление очередью, под тем же паролем, что админка
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


async def track_socket(request, response):
    if isinstance(response, web.WebSocketResponse):
        request.app["sockets"].add(response)
        if request.path == "/api/ws":
            request.app["admin_sockets"].add(response)


async def close_sockets(app):
    """Открытые сокеты админки и оверлея сами не кончаются: без этого остановка по SIGTERM (docker stop, Linux) ждала
    их shutdown_timeout и ещё столько же — 10 с при открытой админке (замер в контейнере)."""
    socks = list(app["sockets"])
    app["ctx"].engine.dirty.set()   # admin_ws спит до 2 с в ожидании изменений
    await asyncio.gather(*(ws.close(code=WSCloseCode.GOING_AWAY, message=b"shutdown") for ws in socks),
                         return_exceptions=True)


def page(name, edit=False):
    async def handler(request):
        with open(os.path.join(STATIC, name), encoding="utf-8") as f:
            text = i18n.page(f.read(), edit)
        return web.Response(text=text, content_type="text/html", headers={"Cache-Control": "no-cache"})
    return handler


def foreign_origin(request):
    origin = request.headers.get("Origin")
    return bool(origin) and origin.split("://", 1)[-1] != request.host


def token():
    return hmac.new(_secret().encode(), access.fingerprint().encode(), hashlib.sha256).hexdigest()


def logged_in(request):
    return hmac.compare_digest(request.cookies.get(COOKIE, "").encode(), request.app["token"].encode())


def local_request(request):
    return access.local_request(request.remote, request.host, request.headers)


@web.middleware
async def auth_middleware(request, handler):
    path = request.path
    if not access.source():
        # Без пароля — только локальная сеть: адрес клиента и имя в Host (DNS rebinding, публичное имя или IP
        # при пробросе порта). Оверлей тоже: в интернете без пароля нечего делать и ему.
        if not local_request(request):
            if access.refuse(request.remote, request.host, request.headers):
                log.warning("без пароля отклонён запрос с %s по адресу %s: не из локальной сети",
                            access.refused["addr"], request.host)
            raise web.HTTPForbidden(text=tr("без пароля stream-tts открыт только из локальной сети: откройте его "
                                            "по IP-адресу сервера или задайте пароль в админке (Настройки → Сервер)"))
    elif not path.startswith(PUBLIC) and not logged_in(request):
        # С паролем Host не проверяется: страницу с DNS rebinding останавливает cookie, она привязана к настоящему адресу
        if path.startswith("/api/"):
            raise web.HTTPUnauthorized(text=tr("нужен пароль"))
        # после входа — обратно: док-панель OBS открывает /dock, и вход не должен уводить её в админку
        raise web.HTTPFound("/login" if path == "/" else "/login?" + urllib.parse.urlencode(dict(next=path)))
    # WebSocket браузер открывает с любой страницы без CORS: чужая страница читала бы очередь и настройки (/api/ws)
    # или перехватывала звук оверлея
    if request.headers.get("Upgrade", "").lower() == "websocket" and foreign_origin(request):
        raise web.HTTPForbidden(text=tr("чужой Origin"))
    if request.method == "POST" and path.startswith("/api/"):
        # Защита от запросов с чужих страниц: только JSON и только со своего адреса
        if foreign_origin(request):
            raise web.HTTPForbidden(text=tr("чужой Origin"))
        # Загрузка файла — сырым телом с нестандартным типом: такой запрос с чужой страницы требует CORS-проверки
        if request.content_type != "application/json" and request.content_type not in UPLOADS.get(path, ()):
            raise web.HTTPUnsupportedMediaType(text=tr("нужен application/json"))
    return await handler(request)


def set_login(resp, request):
    resp.set_cookie(COOKIE, request.app["token"], max_age=180 * 86400, httponly=True, samesite="Strict")


def local_path(path):
    """Адрес возврата после входа — только свой путь: «//host» и «/» с обратной косой браузер понимает как
    чужой сайт."""
    path = str(path or "")
    return path if path.startswith("/") and not path.startswith(("//", "/\\")) else "/"


async def login(request):
    form = await request.post()
    nxt = local_path(form.get("next"))
    if access.source() and await asyncio.to_thread(access.check, str(form.get("password", ""))):
        resp = web.HTTPFound(nxt)
        set_login(resp, request)
        raise resp
    await asyncio.sleep(1)
    raise web.HTTPFound("/login?bad=1" + ("&" + urllib.parse.urlencode(dict(next=nxt)) if nxt != "/" else ""))


async def logout(request):
    resp = web.HTTPFound("/login")
    resp.del_cookie(COOKIE)
    raise resp


async def post_password(request, ctx, data):
    """Пароль из админки: {current, new}; new="" — снять. Пароль из переменной окружения здесь не меняется.
    Смена — новый токен: этот ответ ставит новую cookie, остальные входы и вкладки выходят на страницу входа."""
    current, new = str(data.get("current", "")), str(data["new"])
    src = access.source()
    if src == "env":
        return fail(tr("пароль задан переменной STREAM_TTS_PASSWORD — меняется там, с перезапуском"), 409)
    if src and not await asyncio.to_thread(access.check, current):
        await asyncio.sleep(1)   # как у входа: подбор пароля через открытую вкладку не быстрее формы входа
        return fail(tr("неверный текущий пароль"), 403)
    if new and len(new) < access.MIN_LEN:
        return fail(tr("пароль короче {n} символов", n=access.MIN_LEN))
    if not new and not local_request(request):
        # без пароля этот адрес не пускается: следующий же запрос — 403, и войти больше нельзя
        return fail(tr("снять пароль можно только из локальной сети: отсюда без пароля админка не откроется"), 409)
    await asyncio.to_thread(access.set_password, new)
    request.app["token"] = token()
    log.info("пароль админки %s", "задан" if new else "снят")
    resp = ok(password=access.source())
    if new:
        set_login(resp, request)
    # Ответ с новой cookie — раньше закрытия сокетов: вкладка, сменившая пароль, переподключается уже с ней.
    # Остальные по коду 4001 перезагружаются: со старой cookie — на страницу входа, без пароля — с новым состоянием.
    await resp.prepare(request)
    await resp.write_eof()
    socks = list(request.app["admin_sockets"])
    await asyncio.gather(*(ws.close(code=4001, message=b"password") for ws in socks), return_exceptions=True)
    return resp


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


async def voices_zip(request):
    data = await asyncio.to_thread(request.app["ctx"].voices.zip)
    return web.Response(body=data, content_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="tts-voices.zip"'})


MODULES = (admin, state, model, server, editor, train)
POSTS = {path: json_post(fn) for m in MODULES for path, fn in getattr(m, "POSTS", {}).items()}
POSTS["/api/password"] = json_post(post_password)
