"""Обучение LoRA голоса ESpeech (engines/espeech/train.py), /api/train*: набор реплик — здесь, в данных stream-tts,
обучение — на espeech-server."""
import logging, os, urllib.parse

from aiohttp import web

from ..engines.espeech import train as lora
from ..i18n import tr
from .common import MAX_UPLOAD, fail, ok, upload

log = logging.getLogger("web")


def _train_fail(e):
    if isinstance(e, lora.Refused):
        return web.json_response(dict(ok=False, error=str(e), code=e.code), status=400)
    if isinstance(e, ValueError):
        return fail(str(e))
    log.warning("обучение: %s: %s", type(e).__name__, e)
    return fail(tr("сервер синтеза: {e}", e=str(e) or type(e).__name__))


async def api_train(request):
    """Набор реплик голоса и ход обучения на сервере (train: null и error — сервер не ответил)."""
    ctx = request.app["ctx"]
    try:
        out = ctx.train.public(request.query.get("voice", ""))
    except ValueError as e:
        return fail(str(e))
    try:
        out["train"], out["error"] = await ctx.train.status(), ""
    except Exception as e:
        out["train"], out["error"] = None, tr("сервер синтеза: {e}", e=str(e) or type(e).__name__)
    return ok(**out)


async def api_train_status(request):
    try:
        return ok(train=await request.app["ctx"].train.status())
    except Exception as e:
        return _train_fail(e)


async def api_train_item(request):
    q = request.query
    try:
        return web.FileResponse(request.app["ctx"].train.item_path(q.get("voice", ""), q.get("id", "")))
    except ValueError:
        raise web.HTTPNotFound()


async def api_train_source(request):
    q = request.query
    try:
        return web.FileResponse(request.app["ctx"].train.source_path(q.get("voice", ""), q.get("id", "")))
    except ValueError:
        raise web.HTTPNotFound()


async def api_train_synth(request):
    q = request.query
    try:
        path = await request.app["ctx"].train.synth(q.get("voice", ""), q.get("adapter", ""), int(q.get("phrase", -1)))
    except ValueError as e:
        raise web.HTTPBadRequest(text=str(e))
    except Exception as e:
        raise web.HTTPServiceUnavailable(text=tr("синтез: {e}", e=e))
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


async def api_train_upload(request):
    """Реплика (X-Kind: item) или запись для нарезки (source): файл сырым телом, голос и имя файла — в X-Voice и
    X-Filename (URL-кодированы). Текст пары «имя.txt» админка присылает следом (/api/train/items)."""
    ctx = request.app["ctx"]
    voice = urllib.parse.unquote(request.headers.get("X-Voice", ""))
    filename = urllib.parse.unquote(request.headers.get("X-Filename", "audio"))
    try:
        async with upload(request, os.path.splitext(filename)[1][:10], MAX_UPLOAD, tr("файл больше 512 МБ")) as tmp:
            if request.headers.get("X-Kind") == "source":
                result = await ctx.train.add_source(voice, tmp, filename)
            else:
                result = dict(item=await ctx.train.add_item(voice, tmp, filename))
    except ValueError as e:
        return fail(str(e))
    return ok(**result, set=ctx.train.public(voice))


async def post_train_items(request, ctx, data):
    """Тексты реплик: {voice, items: [{id, text}]}."""
    try:
        ctx.train.update(str(data.get("voice", "")), list(data.get("items") or []))
    except (ValueError, TypeError, KeyError) as e:
        return fail(str(e))
    return ok()


async def post_train_items_delete(request, ctx, data):
    voice = str(data.get("voice", ""))
    try:
        ctx.train.delete_items(voice, list(data.get("ids") or []))
        return ok(set=ctx.train.public(voice))
    except ValueError as e:
        return fail(str(e))


async def post_train_cut(request, ctx, data):
    """Реплика из выделения на волне записи: {voice, src, start, end, text?}."""
    voice = str(data.get("voice", ""))
    try:
        item = ctx.train.cut(voice, str(data.get("src", "")), float(data["start"]), float(data["end"]),
                             str(data.get("text", "")))
        return ok(item=item, set=ctx.train.public(voice))
    except ValueError as e:
        return fail(str(e))


async def post_train_source_delete(request, ctx, data):
    voice = str(data.get("voice", ""))
    try:
        ctx.train.delete_source(voice, str(data.get("id", "")))
        return ok(set=ctx.train.public(voice))
    except ValueError as e:
        return fail(str(e))


async def post_train_clear(request, ctx, data):
    try:
        ctx.train.clear(str(data.get("voice", "")))
    except ValueError as e:
        return fail(str(e))
    return ok()


async def post_train_start(request, ctx, data):
    """{voice, steps? (пусто — по объёму), checkpointing, discard? — сначала выбросить несохранённые результаты}."""
    try:
        t = await ctx.train.start(str(data.get("voice", "")), data.get("steps"), bool(data.get("checkpointing")),
                                  bool(data.get("discard")))
    except Exception as e:
        return _train_fail(e)
    return ok(train=t)


async def post_train_stop(request, ctx, data):
    try:
        return ok(train=await ctx.train.stop())
    except Exception as e:
        return _train_fail(e)


async def post_train_save(request, ctx, data):
    try:
        r = await ctx.train.save(str(data.get("snapshot", "")))
    except Exception as e:
        return _train_fail(e)
    ctx.voices.changed()
    return ok(**r)


async def post_train_discard(request, ctx, data):
    try:
        return ok(train=await ctx.train.discard())
    except Exception as e:
        return _train_fail(e)


GETS = {
    "/api/train": api_train,
    "/api/train/status": api_train_status,
    "/api/train/item": api_train_item,
    "/api/train/source": api_train_source,
    "/api/train/synth": api_train_synth,
}
POSTS = {
    "/api/train/items": post_train_items,
    "/api/train/items/delete": post_train_items_delete,
    "/api/train/cut": post_train_cut,
    "/api/train/source/delete": post_train_source_delete,
    "/api/train/clear": post_train_clear,
    "/api/train/start": post_train_start,
    "/api/train/stop": post_train_stop,
    "/api/train/save": post_train_save,
    "/api/train/discard": post_train_discard,
}
RAW = {
    "/api/train/upload": api_train_upload,
}
