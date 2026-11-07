"""Редактор голоса — новый голос из записи (streamtts/editor.py): черновик, записи, варианты, синтез и сохранение,
/api/draft*."""
import os, urllib.parse

from aiohttp import web

from ..i18n import tr
from .common import MAX_UPLOAD, fail, ok, upload


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
    try:
        async with upload(request, os.path.splitext(filename)[1][:10], MAX_UPLOAD, tr("файл больше 512 МБ")) as tmp:
            result = await ctx.editor.add_source(tmp, filename)
    except ValueError as e:
        return fail(str(e))
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
    try:
        ctx.editor.delete_source(str(data["id"]))
    except ValueError as e:
        return fail(str(e))
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


GETS = {
    "/api/draft": api_draft,
    "/api/draft/source/{id}.wav": api_draft_source,
    "/api/draft/sample": api_draft_sample,
    "/api/draft/synth": api_draft_synth,
}
POSTS = {
    "/api/draft": post_draft,
    "/api/draft/accent": post_draft_accent,
    "/api/draft/source/delete": post_draft_source_delete,
    "/api/draft/save": post_draft_save,
    "/api/draft/discard": post_draft_discard,
}
RAW = {
    "/api/draft/upload": api_draft_upload,
}
