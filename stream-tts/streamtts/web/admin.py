"""Действия админки: настройки, способы заказа, голоса, очередь, баны, мат, Twitch; статистика и прослушивание
голоса; режим правки строк."""
import re, secrets

from aiohttp import web

from .. import editor, i18n, stats, updates
from ..commands import SERVICE_COMMANDS
from ..i18n import tr
from ..twitch import TwitchError, explain
from .common import fail, ok


SETTINGS_KEYS = ("enabled", "sources", "reply", "filter_links", "filter_profanity", "read_nick",
                 "queue_limit", "streaming", "max_chars", "token_ratio", "chunk_tokens", "demo_phrase", "lang",
                 "test_phrases", "update_check", "hidden_hints")


ACCESS_LEVELS = ("all", "subs", "vip", "mods")


HINT_ID = re.compile(r"[a-z0-9-]{1,40}")


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


async def post_tunnel(request, ctx, data):
    """Страница голосов в интернете (tunnel.py): on — включить (cloudflared скачается при первом включении)."""
    await ctx.tunnel.set(bool(data["on"]))
    return ok()


async def post_ui_edit(request, ctx, data):
    """Режим правки строк (в разработке, STREAM_TTS_UI_EDITS): text=null — вернуть исходную строку."""
    text = data.get("text")
    i18n.save_edit(str(data["key"]), None if text is None else str(text), str(data.get("note", "")),
                   str(data.get("where", "")))
    return ok(edits=i18n.edits)


GETS = {
    "/api/stats": api_stats,
    "/api/preview": api_preview,
    "/api/profanity": api_profanity_get,
}
POSTS = {
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
    "/api/tunnel": post_tunnel,
}
