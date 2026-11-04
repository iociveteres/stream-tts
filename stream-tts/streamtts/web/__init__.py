"""HTTP: админка (под паролем), оверлей и страница голосов (открыты), API и WebSocket.

Роуты API — в модулях пакета (MODULES): у модуля таблицы GETS, POSTS (тело — JSON, обёртка common.json_post) и RAW
(файл сырым телом, допустимые типы — в UPLOADS). Здесь — сборка приложения, пароль и защита POST, страницы."""
import asyncio, hashlib, hmac, logging, os, secrets, tempfile, urllib.parse

from aiohttp import web

from .. import config, i18n, install
from ..engines.client import LIVE
from ..engines.espeech import train as lora
from ..i18n import tr
from . import admin, editor, model, state, train
from .common import _restart_guard, fail, json_post, ok

log = logging.getLogger("web")
STATIC = os.path.join(config.HERE, "static")
COOKIE = "stream_tts"
PUBLIC = ("/overlay", "/voices", "/static/overlay", "/static/common", "/static/i18n.js", "/login", "/favicon")
# Файлы, которые меняются под тем же адресом: статика при обновлении программы, примеры голосов при смене фразы
REVALIDATE = ("/static/", "/voices/demos/")
UPLOADS = {"/api/voicesets/import": ("application/zip", "application/x-zip-compressed", "application/octet-stream"),
           "/api/draft/upload": ("application/octet-stream",), "/api/train/upload": ("application/octet-stream",)}
MAX_SET_UPLOAD = 2 << 30   # коллекция ESpeech: ~10 МБ на голос (адаптер LoRA), WC3 — ~680 МБ


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
    r.add_post("/api/voicesets/import", api_voiceset_import)
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


# --- первый запуск: модель, устройство, наборы голосов ---
def _install(ctx):
    if not ctx.install:   # готовый сервер в тестах: ставить некуда
        raise ValueError("no built-in server")
    return ctx.install


async def post_model_download(request, ctx, data):
    _install(ctx).start_download(str(data.get("quant") or install.DEFAULT_QUANT), bool(data.get("frontend")))
    return ok()


async def post_model_cancel(request, ctx, data):
    _install(ctx).cancel()
    return ok()


async def post_model_use(request, ctx, data):
    inst = _install(ctx)
    inst.use_model(str(data["name"]))
    if ctx.runtime.launcher is inst.cosy:   # выбрана ESpeech — квант CosyVoice пригодится при возврате к ней
        await ctx.runtime.restart()
    return ok()


async def post_backend(request, ctx, data):
    """Устройство синтеза выбранной модели (у каждой своя настройка: Launcher.DEVICE_KEY)."""
    _install(ctx)
    if stop := await _restart_guard(ctx, data):
        return stop
    backend, launcher = str(data.get("backend", "auto")), ctx.runtime.launcher
    devices = launcher.public()["devices"]
    if backend not in devices:
        return fail(tr("ожидалось {values}", values=", ".join(devices)))
    ctx.settings.update(**{launcher.DEVICE_KEY: backend})
    await ctx.runtime.restart()
    return ok()


async def post_bench(request, ctx, data):
    _install(ctx)
    if data.get("all"):   # сервер перезапускается на каждом устройстве
        if stop := await _restart_guard(ctx, data):
            return stop
    elif (t := await ctx.train.server_state()) and t.get("state") in lora.BUSY:
        return fail(tr("идёт обучение LoRA: видеокарта занята им, замер был бы неверным — проверьте скорость после"))
    ctx.bench.start(bool(data.get("all")))
    return ok()


async def post_voiceset_delete(request, ctx, data):
    inst = _install(ctx)
    if stop := await _restart_guard(ctx, data):
        return stop
    await inst.delete_set(str(data["name"]), data.get("model") or None)
    return ok()


async def api_voiceset_import(request):
    """Набор голосов: zip сырым телом, имя файла — в X-Filename (URL-кодировано)."""
    ctx = request.app["ctx"]
    try:
        inst = _install(ctx)
    except ValueError as e:
        return fail(str(e))
    if stop := await _restart_guard(ctx, dict(force=request.headers.get("X-Force") == "1")):
        return stop
    filename = urllib.parse.unquote(request.headers.get("X-Filename", "voices.zip"))
    fd, tmp = tempfile.mkstemp(suffix=".zip")
    try:
        size = 0
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                size += len(chunk)
                if size > MAX_SET_UPLOAD:
                    return fail(tr("архив больше 2 ГБ"))
                f.write(chunk)
        result = await inst.import_set(tmp, filename)
    except ValueError as e:
        return fail(str(e))
    finally:
        os.remove(tmp)
    ctx.voices.forget(result["added"])  # голос мог смениться: пример пересинтезировать
    ctx.voices.wake.set()
    ctx.engine.dirty.set()
    return ok(**result)


# --- необязательные пакеты ---
async def post_package_download(request, ctx, data):
    _install(ctx).start_package(str(data["id"]))
    return ok()


async def post_package_delete(request, ctx, data):
    inst = _install(ctx)
    if (install.PACKAGES.get(str(data["id"])) or {}).get("restart") and (stop := await _restart_guard(ctx, data)):
        return stop   # сервер останавливается, пока удаляются файлы фронтенда
    await inst.delete_package(str(data["id"]))
    return ok()


async def post_cuda_download(request, ctx, data):
    inst = _install(ctx)
    if stop := await _restart_guard(ctx, data):   # распаковка — при остановленном сервере
        return stop
    inst.start_cuda()
    return ok()


async def post_espeech_download(request, ctx, data):
    inst = _install(ctx)
    if stop := await _restart_guard(ctx, data):   # замена рантайма и перезапуск ESpeech после скачивания
        return stop
    inst.start_espeech()
    return ok()


async def post_espeech_delete(request, ctx, data):
    inst = _install(ctx)
    if stop := await _restart_guard(ctx, data):   # сервер ESpeech останавливается, пока удаляются файлы
        return stop
    await inst.delete_espeech()
    return ok()


# --- модели в памяти (вкладка «Статистика») ---
async def post_model_unload(request, ctx, data):
    what = str(data["id"])
    if what == "tts":
        if not ctx.runtime:   # готовый сервер в тестах
            return fail("no built-in server")
        if stop := await _restart_guard(ctx, data):   # выгрузка — остановка процесса сервера
            return stop
        # после текущего запроса к серверу, иначе он оборвётся на середине
        async with ctx.tts.lane.hold(LIVE, tr("выгрузка модели")):
            await ctx.runtime.unload()
        ctx.tts.ok, ctx.tts.error = False, tr("модель выгружена")
        log.info("модель синтеза выгружена: сервер остановлен")
    elif what == "frontend":
        try:
            await ctx.editor.unload_frontend()
        except Exception as e:
            return fail(str(e) or type(e).__name__)
    elif what == "music":
        ctx.editor.separator.unload()
    else:
        return fail(tr("нет такой модели"))
    return ok()


async def post_model_load(request, ctx, data):
    if str(data["id"]) != "tts" or not ctx.runtime:
        return fail(tr("эта модель загружается сама, когда нужна"))
    ctx.runtime.load()
    log.info("модель синтеза: запуск сервера")
    return ok()


# --- обновления (updates.py) ---
async def post_update_check(request, ctx, data):
    await ctx.updates.check()
    return ok(update=ctx.updates.public())


async def post_update_dismiss(request, ctx, data):
    """Скрыть плашку о новой версии до следующей."""
    ctx.settings.update(update_dismissed=str(data["version"]))
    return ok()


MODULES = (admin, state, model, editor, train)
POSTS = {path: json_post(fn) for path, fn in {
    **{p: f for m in MODULES for p, f in getattr(m, "POSTS", {}).items()},
    "/api/model/download": post_model_download,
    "/api/model/cancel": post_model_cancel,
    "/api/model/use": post_model_use,
    "/api/backend": post_backend,
    "/api/bench": post_bench,
    "/api/voicesets/delete": post_voiceset_delete,
    "/api/package/download": post_package_download,
    "/api/package/delete": post_package_delete,
    "/api/cuda/download": post_cuda_download,
    "/api/espeech/download": post_espeech_download,
    "/api/espeech/delete": post_espeech_delete,
    "/api/models/unload": post_model_unload,
    "/api/models/load": post_model_load,
    "/api/update/check": post_update_check,
    "/api/update/dismiss": post_update_dismiss,
}.items()}
