"""HTTP: админка (под паролем), оверлей и страница голосов (открыты), API и WebSocket."""
import asyncio, contextlib, hashlib, hmac, json, logging, os, secrets, tempfile, time, urllib.parse

from aiohttp import web

from . import config, install, runtime, stats
from .twitch import TwitchError, explain

log = logging.getLogger("web")
STATIC = os.path.join(config.HERE, "static")
COOKIE = "stream_tts"
PUBLIC = ("/overlay", "/voices", "/static/overlay", "/static/common", "/login", "/favicon")
UPLOADS = {"/api/voicesets/import": ("application/zip", "application/x-zip-compressed", "application/octet-stream")}
MAX_UPLOAD = 512 << 20


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
    app["token"] = hmac.new(_secret().encode(), config.PASSWORD.encode(), hashlib.sha256).hexdigest()
    r = app.router
    r.add_get("/", page("admin.html"))
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
    r.add_get("/api/state", api_state)
    r.add_get("/api/ws", admin_ws)
    r.add_get("/api/stats", api_stats)
    r.add_get("/api/preview", api_preview)
    r.add_get("/api/profanity", api_profanity_get)
    for path, fn in POSTS.items():
        r.add_post(path, fn)
    r.add_post("/api/voicesets/import", api_voiceset_import)
    return app


def page(name):
    async def handler(request):
        return web.FileResponse(os.path.join(STATIC, name), headers={"Cache-Control": "no-cache"})
    return handler


@web.middleware
async def auth_middleware(request, handler):
    path = request.path
    if config.PASSWORD and not path.startswith(PUBLIC):
        if not hmac.compare_digest(request.cookies.get(COOKIE, ""), request.app["token"]):
            if path.startswith("/api/"):
                raise web.HTTPUnauthorized(text="нужен пароль")
            raise web.HTTPFound("/login")
    if request.method == "POST" and path.startswith("/api/"):
        # Защита от запросов с чужих страниц: только JSON и только со своего адреса
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:
            raise web.HTTPForbidden(text="чужой Origin")
        # Загрузка файла — сырым телом с нестандартным типом: такой запрос с чужой страницы требует CORS-проверки
        if request.content_type != "application/json" and request.content_type not in UPLOADS.get(path, ()):
            raise web.HTTPUnsupportedMediaType(text="нужен application/json")
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
    return await request.app["ctx"].engine.overlay.handle(request)


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


# --- состояние ---
SETTINGS_KEYS = ("enabled", "access", "filter_links", "filter_profanity", "read_nick", "subtitles", "queue_limit",
                 "streaming", "max_chars", "token_ratio", "chunk_tokens", "demo_phrase", "guide_done")


def state(ctx, full=True):
    s = ctx.settings
    out = dict(
        engine=ctx.engine.snapshot(),
        twitch=ctx.twitch.public(),
        cosy=dict(ok=ctx.cosy.ok, error=ctx.cosy.error, url=ctx.cosy.url,
                  busy=ctx.cosy.lane.holder[1] if ctx.cosy.lane.holder else "",
                  runtime=ctx.runtime.public() if ctx.runtime else None),
        gpu=ctx.gpu.data,
        demos=dict(ctx.voices.progress),
        setup=ctx.install.public() if ctx.install else None,
        ts=time.time(),
    )
    if full:
        out.update(
            settings={k: s[k] for k in SETTINGS_KEYS},
            voices=ctx.voices.public(),
            bans=ctx.db.bans(),
            config=dict(password=bool(config.PASSWORD), bind=config.BIND, port=config.PORT),
        )
    return out


async def api_state(request):
    return web.json_response(state(request.app["ctx"]))


async def admin_ws(request):
    ctx = request.app["ctx"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    ctx.admins += 1
    try:
        await ws.send_json(state(ctx))
        reader = asyncio.create_task(ws.receive())
        while not ws.closed:
            ctx.engine.dirty.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ctx.engine.dirty.wait(), 2)
            await asyncio.sleep(0.15)  # пачка изменений одним сообщением
            if reader.done():
                break
            await ws.send_json(state(ctx, full=False))
        reader.cancel()
    except (ConnectionResetError, RuntimeError):
        pass
    finally:
        ctx.admins -= 1
    return ws


async def api_stats(request):
    hours = min(24 * 30, max(1, int(request.query.get("hours", 24))))
    return web.json_response(stats.period(request.app["ctx"].db, hours, request.query.get("test") == "1"))


async def api_preview(request):
    ctx = request.app["ctx"]
    name = request.query.get("name", "")
    if name not in ctx.voices.names():
        raise web.HTTPNotFound()
    try:
        path = await ctx.voices.preview(name)
    except Exception as e:
        raise web.HTTPServiceUnavailable(text=f"синтез: {e}")
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


async def api_profanity_get(request):
    ctx = request.app["ctx"]
    with open(ctx.profanity.path, encoding="utf-8") as f:
        return web.json_response(dict(text=f.read()))


# --- действия ---
def ok(**kw):
    return web.json_response(dict(ok=True, **kw))


def fail(msg, status=400):
    return web.json_response(dict(ok=False, error=msg), status=status)


async def post_settings(request, ctx, data):
    s = ctx.settings
    for k, v in data.items():
        if k not in SETTINGS_KEYS or k == "enabled":
            continue
        if k in ("queue_limit", "max_chars"):
            v = max(1, int(v))
        elif k == "token_ratio":
            v = max(0.0, float(v))
        elif k == "chunk_tokens":
            v = min(1000, max(10, int(v)))
        elif k == "access" and v not in ("all", "subs", "vip", "mods"):
            continue
        s.data[k] = v
    s.save()
    if "demo_phrase" in data:
        ctx.voices.wake.set()
    return ok()


async def post_voice(request, ctx, data):
    names = ctx.voices.names() if data.get("all") else [data["name"]]
    for n in names:
        if n not in ctx.voices.names():
            return fail("нет такого голоса")
        v = ctx.settings.voice(n)
        if "enabled" in data:
            v["enabled"] = bool(data["enabled"])
        if "gain_db" in data and not data.get("all"):
            v["gain_db"] = max(-20.0, min(20.0, float(data["gain_db"])))
    ctx.settings.save()
    ctx.voices.wake.set()
    return ok()


async def post_demos_rebuild(request, ctx, data):
    ctx.voices.index.clear()
    ctx.voices.wake.set()
    return ok()


async def post_client_id(request, ctx, data):
    try:
        await ctx.twitch.set_client_id(str(data.get("client_id", "")))
    except TwitchError as e:
        return fail(e.message)
    return ok()


async def post_login(request, ctx, data):
    try:
        await ctx.twitch.begin_login()
    except TwitchError as e:
        return fail(e.message)
    return ok()


async def post_logout(request, ctx, data):
    await ctx.twitch.logout()
    return ok()


async def post_guide(request, ctx, data):
    done = set(ctx.settings["guide_done"])
    (done.add if data.get("done") else done.discard)(int(data["step"]))
    ctx.settings.update(guide_done=sorted(done))
    return ok()


async def post_reward(request, ctx, data):
    try:
        await ctx.twitch.save_reward(data)
    except (TwitchError, KeyError, ValueError) as e:
        return fail(explain(e))
    return ok()


async def post_reward_delete(request, ctx, data):
    await ctx.twitch.delete_reward()
    return ok()


async def post_enabled(request, ctx, data):
    await ctx.engine.set_enabled(bool(data.get("on")))
    return ok()


async def post_skip(request, ctx, data):
    await ctx.engine.skip()
    return ok()


async def post_clear(request, ctx, data):
    await ctx.engine.clear()
    return ok()


async def post_remove(request, ctx, data):
    return ok() if await ctx.engine.remove(data.get("id")) else fail("нет в очереди")


async def post_test(request, ctx, data):
    """Тестовая активация из интерфейса: проходит весь путь, кроме Twitch."""
    login = (data.get("login") or "tester").strip().lower()
    if data.get("voice"):
        if data["voice"] not in ctx.voices.enabled():
            return fail("голос выключен или не найден")
        ctx.db.set_voice(f"test:{login}", login, data["voice"])
    rid = f"test-{secrets.token_hex(4)}"
    await ctx.engine.redemption(rid, "", f"test:{login}", login, data.get("login") or login, data.get("text", ""),
                                test=True)
    return ok(id=rid)


async def post_ban(request, ctx, data):
    login = str(data.get("login", "")).lstrip("@").strip()
    if not login:
        return fail("ник?")
    ctx.db.ban(login, bool(data.get("on", True)))
    return ok()


async def post_profanity(request, ctx, data):
    with open(ctx.profanity.path, "w", encoding="utf-8") as f:
        f.write(str(data.get("text", "")))
    ctx.profanity.reload()
    sample = str(data.get("sample", ""))
    return ok(sample=ctx.profanity.clean(sample, beep="[писк]") if sample else "")


# --- первый запуск (режим managed): модель, устройство, наборы голосов ---
def _install(ctx):
    if not ctx.install:
        raise ValueError("сервер синтеза внешний (COSYVOICE_URL): модель и голоса ставятся на нём")
    return ctx.install


async def post_model_download(request, ctx, data):
    _install(ctx).start_download(str(data.get("quant") or install.DEFAULT_QUANT), bool(data.get("frontend")))
    return ok()


async def post_model_cancel(request, ctx, data):
    _install(ctx).cancel()
    return ok()


async def post_model_use(request, ctx, data):
    _install(ctx).use_model(str(data["name"]))
    await ctx.runtime.restart()
    return ok()


async def post_backend(request, ctx, data):
    _install(ctx)
    backend = str(data.get("backend", "auto"))
    if backend not in runtime.BACKENDS:
        return fail("ожидалось auto, cuda, vulkan или cpu")
    ctx.settings.update(backend=backend)
    await ctx.runtime.restart()
    return ok()


async def post_voiceset_delete(request, ctx, data):
    await _install(ctx).delete_set(str(data["name"]))
    return ok()


async def api_voiceset_import(request):
    """Набор голосов: zip сырым телом, имя файла — в X-Filename (URL-кодировано)."""
    ctx = request.app["ctx"]
    try:
        inst = _install(ctx)
    except ValueError as e:
        return fail(str(e))
    filename = urllib.parse.unquote(request.headers.get("X-Filename", "voices.zip"))
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    try:
        size = 0
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    return fail("архив больше 512 МБ")
                f.write(chunk)
        result = await inst.import_set(tmp, filename)
    except ValueError as e:
        return fail(str(e))
    finally:
        os.remove(tmp)
    for v in result["added"]:  # голос мог смениться: пример пересинтезировать
        ctx.voices.index.pop(v, None)
    ctx.voices.wake.set()
    ctx.engine.dirty.set()
    return ok(**result)


def _wrap(fn):
    async def handler(request):
        try:
            data = await request.json() if request.can_read_body else {}
        except ValueError:
            return fail("неверный JSON")
        ctx = request.app["ctx"]
        try:
            resp = await fn(request, ctx, data or {})
        except (KeyError, ValueError, TypeError) as e:
            return fail(f"неверный запрос: {e}")
        ctx.engine.dirty.set()
        return resp
    return handler


POSTS = {path: _wrap(fn) for path, fn in {
    "/api/settings": post_settings,
    "/api/voice": post_voice,
    "/api/demos/rebuild": post_demos_rebuild,
    "/api/twitch/client_id": post_client_id,
    "/api/twitch/login": post_login,
    "/api/twitch/logout": post_logout,
    "/api/twitch/guide": post_guide,
    "/api/reward": post_reward,
    "/api/reward/delete": post_reward_delete,
    "/api/enabled": post_enabled,
    "/api/skip": post_skip,
    "/api/clear": post_clear,
    "/api/remove": post_remove,
    "/api/test": post_test,
    "/api/ban": post_ban,
    "/api/profanity": post_profanity,
    "/api/model/download": post_model_download,
    "/api/model/cancel": post_model_cancel,
    "/api/model/use": post_model_use,
    "/api/backend": post_backend,
    "/api/voicesets/delete": post_voiceset_delete,
}.items()}
