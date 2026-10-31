"""HTTP: админка (под паролем), оверлей и страница голосов (открыты), API и WebSocket."""
import asyncio, contextlib, hashlib, hmac, json, logging, os, re, secrets, tempfile, time, urllib.parse

from aiohttp import web

from . import config, editor, filters, i18n, install, separate, stats, updates
from .engines.client import LIVE
from .i18n import tr
from .engine import SERVICE_COMMANDS
from .twitch import TwitchError, explain

log = logging.getLogger("web")
STATIC = os.path.join(config.HERE, "static")
COOKIE = "stream_tts"
PUBLIC = ("/overlay", "/voices", "/static/overlay", "/static/common", "/static/i18n.js", "/login", "/favicon")
# Файлы, которые меняются под тем же адресом: статика при обновлении программы, примеры голосов при смене фразы
REVALIDATE = ("/static/", "/voices/demos/")
UPLOADS = {"/api/voicesets/import": ("application/zip", "application/x-zip-compressed", "application/octet-stream"),
           "/api/draft/upload": ("application/octet-stream",)}
MAX_UPLOAD = 512 << 20
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
    r.add_get("/api/state", api_state)
    r.add_get("/api/ws", admin_ws)
    r.add_get("/api/stats", api_stats)
    r.add_get("/api/preview", api_preview)
    r.add_get("/api/profanity", api_profanity_get)
    r.add_get("/api/draft", api_draft)
    r.add_get("/api/draft/source/{id}.wav", api_draft_source)
    r.add_get("/api/draft/sample", api_draft_sample)
    r.add_get("/api/draft/synth", api_draft_synth)
    r.add_get("/api/accents", api_accents)
    for path, fn in POSTS.items():
        r.add_post(path, fn)
    r.add_post("/api/voicesets/import", api_voiceset_import)
    r.add_post("/api/draft/upload", api_draft_upload)
    if i18n.EDITS_PATH:
        r.add_post("/api/ui-edit", _wrap(post_ui_edit))
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


# --- состояние ---
SETTINGS_KEYS = ("enabled", "sources", "reply", "filter_links", "filter_profanity", "read_nick",
                 "queue_limit", "streaming", "max_chars", "token_ratio", "chunk_tokens", "demo_phrase", "lang",
                 "test_phrases", "update_check", "hidden_hints")
ACCESS_LEVELS = ("all", "subs", "vip", "mods")
HINT_ID = re.compile(r"[a-z0-9-]{1,40}")


def state(ctx, full=True):
    s = ctx.settings
    out = dict(
        engine=ctx.engine.snapshot(),
        twitch=ctx.twitch.public(),
        # сервер синтеза выбранной модели (ключ cosy — с тех пор, когда модель была одна)
        cosy=dict(ok=ctx.tts.ok, error=ctx.tts.error, url=ctx.tts.url,
                  busy=ctx.tts.lane.holder[1] if ctx.tts.lane.holder else "", waiting=ctx.tts.lane.queued(),
                  runtime=ctx.runtime.public() if ctx.runtime else None,
                  engine=ctx.tts.id, title=ctx.tts.title, engines=ctx.tts.engines(), switching=ctx.tts.switching,
                  capabilities=ctx.tts.capabilities, params=model_params(ctx)),
        load=ctx.load.public(history=full),
        models=models(ctx),
        demos=dict(ctx.voices.progress),
        setup=ctx.install.public() if ctx.install else None,
        # необязательные пакеты; у готового сервера в тестах файлов не видно — о фронтенде говорит GET /frontend/model
        packages=install.packages() if ctx.install else install.packages(dict(frontend=ctx.tts.frontend_ready)),
        bench=ctx.bench.public() if ctx.bench else None,
        update=ctx.updates.public(),
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


def model_params(ctx):
    """Настройки модели по схеме params из /status сервера (подписи — на языке интерфейса)."""
    return [dict(p, title=tr(p.get("title", p["param"])), help=tr(p["help"]) if p.get("help") else "")
            for p in ctx.tts.schema()]


def models(ctx):
    """Модели в памяти (вкладка «Статистика»): синтез и ONNX-фронтенд — на сервере синтеза, htdemucs — в stream-tts.
    Модель синтеза выгружается остановкой встроенного сервера (у готового сервера в тестах — никак). Объём памяти не
    показывается: cosyvoice-server его не отдаёт, а фронтенд и htdemucs живут в общих процессах, их долю не измерить."""
    rt = ctx.runtime.public() if ctx.runtime else None
    up = ctx.tts.ok is True
    out = [dict(id="tts", name=(rt and rt["model"]) or ctx.tts.info.get("model") or ctx.tts.title,
                what=tr("синтез речи"), where=(rt and rt["backend"]) or "", loaded=up,
                unload=bool(rt) and not rt["unloaded"] and rt["state"] == "running",
                load=bool(rt and rt["unloaded"]))]
    if ctx.tts.frontend_ready:
        out.append(dict(id="frontend", name="speech_tokenizer + campplus", what=tr("создание голосов из записи"),
                        where=tr("сервер синтеза"), loaded=up and ctx.tts.frontend_loaded,
                        unload=up and ctx.tts.frontend_loaded, load=False))
    if separate.available():
        sep = ctx.editor.separator
        out.append(dict(id="music", name="htdemucs int8", what=tr("очистка образца от музыки"),
                        where=tr("stream-tts, процессор"), loaded=sep.session is not None,
                        unload=sep.session is not None and not sep.lock.locked(), load=False))
    return out


async def api_state(request):
    return web.json_response(state(request.app["ctx"]))


async def admin_ws(request):
    ctx = request.app["ctx"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    ctx.admins += 1
    try:
        rev = ctx.voices.rev
        await ws.send_json(state(ctx))
        reader = asyncio.create_task(ws.receive())
        while not ws.closed:
            ctx.engine.dirty.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ctx.engine.dirty.wait(), 2)
            await asyncio.sleep(0.15)  # пачка изменений одним сообщением
            if reader.done():
                break
            part = state(ctx, full=False)
            if ctx.voices.rev != rev:  # набор установлен или удалён, готов пример: без этого нужна перезагрузка страницы
                rev = ctx.voices.rev
                part["voices"] = ctx.voices.public()
            await ws.send_json(part)
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
        raise web.HTTPServiceUnavailable(text=tr("синтез: {e}", e=e))
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
    params = {p["key"] for p in ctx.tts.schema()}
    for k, v in list(data.items()):
        if k in params:   # настройка модели: значение из вариантов схемы, передаётся в каждом запросе
            try:
                s.data[k] = ctx.tts.check_param(k, v)
            except (TypeError, ValueError):
                return fail(tr("недопустимое значение {key}: {value}", key=k, value=v))
            continue
        if k not in SETTINGS_KEYS or k in ("enabled", "sources"):
            continue
        if k in ("queue_limit", "max_chars"):
            v = max(1, int(v))
        elif k == "token_ratio":
            v = max(0.0, float(v))
        elif k == "chunk_tokens":
            v = min(1000, max(10, int(v)))
        elif k == "reply" and v not in ("off", "chat"):
            continue
        elif k == "update_check" and v not in updates.INTERVALS:
            continue
        elif k == "hidden_hints":   # id подсказок из разметки (data-hint)
            if not isinstance(v, list):
                continue
            v = sorted({x for x in v if isinstance(x, str) and HINT_ID.fullmatch(x)})[:100]
        elif k == "test_phrases":
            v = [" ".join(str(p).split())[:300] for p in v if str(p).strip()][:editor.MAX_PHRASES]
            if not v:
                return fail(tr("нужна хотя бы одна фраза"))
        elif k == "lang":
            if v in i18n.LANGS and v != s["lang"] and i18n.set_lang(s, v):
                data["demo_phrase"] = s["demo_phrase"]  # фраза примеров была по умолчанию и сменилась с языком
            continue
        s.data[k] = v
    s.save()
    if "demo_phrase" in data:
        ctx.voices.wake.set()
    return ok()


async def post_source(request, ctx, data):
    """Способ заказа озвучки (sources в настройках): включён ли, кто может, список ников, у команды — имя и кулдауны."""
    name = data.get("source")
    src = ctx.settings["sources"].get(name)
    if src is None:
        return fail(tr("нет такого способа"))
    for k, v in data.items():
        if k == "on":
            src["on"] = bool(v)
        elif k == "access" and v in ACCESS_LEVELS:
            src["access"] = v
        elif k == "allow":
            items = v if isinstance(v, list) else re.split(r"[\s,;]+", str(v))
            src["allow"] = sorted({n.strip().lstrip("@").lower() for n in items if n.strip().lstrip("@")})
        elif name == "command" and k == "name":
            cmd = "!" + str(v).strip().lstrip("!").lower()
            if not re.fullmatch(r"![\w-]{1,25}", cmd):
                return fail(tr("команда — «!» и одно слово, например !tts"))
            if cmd in SERVICE_COMMANDS:
                return fail(tr("{cmd} — служебная команда, выберите другое имя", cmd=cmd))
            src["name"] = cmd
        elif name == "command" and k in ("user_cd", "global_cd"):
            src[k] = min(3600, max(0, int(v)))
    ctx.settings.save()
    if name == "reward" and "on" in data:
        await ctx.twitch.set_paused(ctx.twitch.want_paused())
    ctx.engine.dirty.set()
    return ok(source=src)


async def post_voice(request, ctx, data):
    # name — один голос, names — несколько (кнопки «Включить/Выключить все» по результатам поиска)
    many = "names" in data
    names = [str(n) for n in data["names"]] if many else [data["name"]]
    if any(n not in ctx.voices.names() for n in names):
        return fail(tr("нет такого голоса"))
    for n in names:
        v = ctx.settings.voice(n)
        if "enabled" in data:
            v["enabled"] = bool(data["enabled"])
        if "gain_db" in data and not many:
            v["gain_db"] = max(-20.0, min(20.0, float(data["gain_db"])))
    ctx.settings.save()
    ctx.voices.wake.set()
    return ok()


async def post_demos_rebuild(request, ctx, data):
    ctx.voices.forget()
    ctx.voices.wake.set()
    return ok()


async def post_client_id(request, ctx, data):
    try:
        await ctx.twitch.set_client_id(str(data.get("client_id", "")))
    except TwitchError as e:
        return fail(e.message)
    return ok()


async def post_twitch_mode(request, ctx, data):
    mode = data.get("mode")
    if mode not in ("app", "chat"):
        return fail(tr("неизвестный режим"))
    try:
        await ctx.twitch.set_mode(mode, data.get("channel"))
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
    return ok() if await ctx.engine.remove(data.get("id")) else fail(tr("нет в очереди"))


async def post_test(request, ctx, data):
    """Тестовая активация из интерфейса: проходит весь путь, кроме Twitch."""
    login = (data.get("login") or "tester").strip().lower()
    if data.get("voice"):
        if data["voice"] not in ctx.voices.enabled():
            return fail(tr("голос выключен или не найден"))
        ctx.db.set_voice(f"test:{login}", login, data["voice"])
    rid = f"test-{secrets.token_hex(4)}"
    await ctx.engine.redemption(rid, "", f"test:{login}", login, data.get("login") or login, data.get("text", ""),
                                test=True)
    return ok(id=rid)


async def post_ban(request, ctx, data):
    login = str(data.get("login", "")).lstrip("@").strip()
    if not login:
        return fail(tr("ник?"))
    ctx.db.ban(login, bool(data.get("on", True)))
    return ok()


async def post_profanity(request, ctx, data):
    with open(ctx.profanity.path, "w", encoding="utf-8") as f:
        f.write(str(data.get("text", "")))
    ctx.profanity.reload()
    sample = str(data.get("sample", ""))
    return ok(sample=ctx.profanity.clean(sample, beep=tr("[писк]")) if sample else "")


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
    backend, launcher = str(data.get("backend", "auto")), ctx.runtime.launcher
    devices = launcher.public()["devices"]
    if backend not in devices:
        return fail(tr("ожидалось {values}", values=", ".join(devices)))
    ctx.settings.update(**{launcher.DEVICE_KEY: backend})
    await ctx.runtime.restart()
    return ok()


# --- модель синтеза (engines/): одна единовременно, смена — перезапуск встроенного сервера ---
async def post_engine(request, ctx, data):
    engine = str(data.get("engine", ""))
    if engine not in ctx.tts.available:
        return fail(tr("модель {engine} недоступна", engine=engine))
    if ctx.bench and ctx.bench.task and not ctx.bench.task.done():
        return fail(tr("идёт проверка скорости"))

    async def switch():
        try:
            await ctx.tts.use(engine, ctx.voices.refresh)
        except Exception as e:
            log.warning("смена модели: %s", e)
        ctx.voices.changed()
        ctx.engine.dirty.set()
    asyncio.create_task(switch())
    return ok()


async def post_accent(request, ctx, data):
    """«Как прочитает»: текст после фильтров озвучки, нормализованный и с ударениями (POST /accent сервера)."""
    if not ctx.tts.capabilities.get("accent"):
        return fail(tr("модель {engine} не показывает ударения", engine=ctx.tts.title))
    text = ctx.engine.clean(str(data.get("text", "")))
    if not text:
        return fail(tr("нечего озвучивать: после фильтров текста не осталось"))
    # мимо очереди: espeech-server размечает текст в потоке запроса, параллельно синтезу
    norm, acc = [], []
    for part in text.split(filters.BEEP):
        if filters.has_words(part):
            r = await ctx.tts.accent(part.strip())
            norm.append(r["normalized"])
            acc.append(r["accented"])
    beep = " " + tr("[писк]") + " "
    return ok(text=text.replace(filters.BEEP, beep.strip()), normalized=beep.join(norm), accented=beep.join(acc))


# --- словарь ударений пользователя (capabilities.user_accents, хранит сервер модели: espeech-server) ---
def _user_accents(ctx):
    if not ctx.tts.capabilities.get("user_accents"):
        raise ValueError(tr("у модели {engine} нет словаря ударений", engine=ctx.tts.title))


async def api_accents(request):
    ctx = request.app["ctx"]
    try:
        _user_accents(ctx)
        return ok(words=await ctx.tts.user_accents())
    except ValueError as e:
        return fail(str(e))
    except Exception as e:
        return fail(tr("сервер синтеза: {e}", e=str(e) or type(e).__name__))


async def post_accents(request, ctx, data):
    """Добавить или заменить слово: {accented: «з+амок»}."""
    try:
        _user_accents(ctx)
    except ValueError as e:
        return fail(str(e))
    try:
        r = await ctx.tts.put_user_accent(str(data.get("accented", "")))
        words = await ctx.tts.user_accents()
    except ValueError:   # текст ошибки сервера не переводится
        return fail(tr("одно слово русскими буквами, ударение — «+» перед гласной"))
    except Exception as e:
        return fail(tr("сервер синтеза: {e}", e=str(e) or type(e).__name__))
    log.info("словарь ударений: %s", r["accented"])
    return ok(word=r["word"], accented=r["accented"], words=words)


async def post_accents_delete(request, ctx, data):
    try:
        _user_accents(ctx)
    except ValueError as e:
        return fail(str(e))
    word = str(data.get("word", ""))
    try:
        if not await ctx.tts.delete_user_accent(word):
            return fail(tr("нет такого слова"))
        words = await ctx.tts.user_accents()
    except Exception as e:
        return fail(tr("сервер синтеза: {e}", e=str(e) or type(e).__name__))
    log.info("словарь ударений: убрано %s", word)
    return ok(words=words)


async def post_bench(request, ctx, data):
    _install(ctx)
    ctx.bench.start(bool(data.get("all")))
    return ok()


async def post_voiceset_delete(request, ctx, data):
    await _install(ctx).delete_set(str(data["name"]), data.get("model") or None)
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


# --- новый голос из записи (editor.py) и необязательные пакеты ---
async def api_draft(request):
    return web.json_response(request.app["ctx"].editor.public())


async def api_draft_source(request):
    try:
        return web.FileResponse(request.app["ctx"].editor.source_path(request.match_info["id"]))
    except ValueError:
        raise web.HTTPNotFound()


async def api_draft_sample(request):
    try:
        data = await request.app["ctx"].editor.sample_wav(request.query.get("variant", ""))
    except ValueError as e:
        raise web.HTTPNotFound(text=str(e))
    return web.Response(body=data, content_type="audio/wav")


async def api_draft_synth(request):
    q = request.query
    try:
        path = await request.app["ctx"].editor.synth(q.get("variant", ""), q.get("voice", ""), int(q.get("phrase", -1)))
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    except Exception as e:
        raise web.HTTPServiceUnavailable(text=tr("синтез: {e}", e=e))
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


async def api_draft_upload(request):
    """Запись для черновика: файл сырым телом, имя — в X-Filename (URL-кодировано)."""
    ctx = request.app["ctx"]
    filename = urllib.parse.unquote(request.headers.get("X-Filename", "audio"))
    fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(filename)[1][:10])
    try:
        size = 0
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                size += len(chunk)
                if size > MAX_UPLOAD:
                    return fail(tr("файл больше 512 МБ"))
                f.write(chunk)
        result = await ctx.editor.add_source(tmp, filename)
    except ValueError as e:
        return fail(str(e))
    finally:
        os.remove(tmp)
    return ok(**result, draft=ctx.editor.public())


async def post_draft(request, ctx, data):
    try:
        return ok(draft=ctx.editor.update(data))
    except ValueError as e:
        return fail(str(e))


async def post_draft_accent(request, ctx, data):
    """Расшифровка образца с ударениями модели — без фильтров озвучки: это не сообщение чата."""
    try:
        return ok(**await ctx.editor.accent(data.get("text", "")))
    except ValueError as e:
        return fail(str(e))
    except Exception as e:
        return fail(tr("сервер синтеза: {e}", e=str(e) or type(e).__name__))


async def post_draft_source_delete(request, ctx, data):
    ctx.editor.delete_source(str(data["id"]))
    return ok(draft=ctx.editor.public())


async def post_draft_save(request, ctx, data):
    try:
        result = await ctx.editor.save(str(data["variant"]), str(data["name"]).strip(), str(data["set"]))
    except ValueError as e:
        return fail(str(e))
    except Exception as e:
        return fail(tr("сервер синтеза: {e}", e=str(e) or type(e).__name__))
    ctx.voices.changed()
    return ok(**result)


async def post_draft_discard(request, ctx, data):
    await ctx.editor.discard()
    return ok()


async def post_package_download(request, ctx, data):
    _install(ctx).start_package(str(data["id"]))
    return ok()


async def post_package_delete(request, ctx, data):
    await _install(ctx).delete_package(str(data["id"]))
    return ok()


async def post_cuda_download(request, ctx, data):
    _install(ctx).start_cuda()
    return ok()


async def post_espeech_download(request, ctx, data):
    _install(ctx).start_espeech()
    return ok()


async def post_espeech_delete(request, ctx, data):
    await _install(ctx).delete_espeech()
    return ok()


# --- модели в памяти (вкладка «Статистика») ---
async def post_model_unload(request, ctx, data):
    what = str(data["id"])
    if what == "tts":
        if not ctx.runtime:   # готовый сервер в тестах
            return fail("no built-in server")
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


async def post_ui_edit(request, ctx, data):
    """Режим правки строк (в разработке, STREAM_TTS_UI_EDITS): text=null — вернуть исходную строку."""
    text = data.get("text")
    i18n.save_edit(str(data["key"]), None if text is None else str(text), str(data.get("note", "")),
                   str(data.get("where", "")))
    return ok(edits=i18n.edits)


def _wrap(fn):
    async def handler(request):
        try:
            data = await request.json() if request.can_read_body else {}
        except ValueError:
            return fail(tr("неверный JSON"))
        ctx = request.app["ctx"]
        try:
            resp = await fn(request, ctx, data or {})
        except (KeyError, ValueError, TypeError) as e:
            return fail(tr("неверный запрос: {e}", e=e))
        ctx.engine.dirty.set()
        return resp
    return handler


POSTS = {path: _wrap(fn) for path, fn in {
    "/api/settings": post_settings,
    "/api/source": post_source,
    "/api/voice": post_voice,
    "/api/demos/rebuild": post_demos_rebuild,
    "/api/twitch/client_id": post_client_id,
    "/api/twitch/mode": post_twitch_mode,
    "/api/twitch/login": post_login,
    "/api/twitch/logout": post_logout,
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
    "/api/engine": post_engine,
    "/api/accent": post_accent,
    "/api/accents": post_accents,
    "/api/accents/delete": post_accents_delete,
    "/api/bench": post_bench,
    "/api/voicesets/delete": post_voiceset_delete,
    "/api/draft": post_draft,
    "/api/draft/accent": post_draft_accent,
    "/api/draft/source/delete": post_draft_source_delete,
    "/api/draft/save": post_draft_save,
    "/api/draft/discard": post_draft_discard,
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
