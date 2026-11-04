"""Общее для модулей web/: ответы ok/fail, обёртка JSON-POST, приём файла сырым телом (upload) и проверка обучения
LoRA перед перезапуском встроенного сервера (_restart_guard)."""
import contextlib, os, tempfile

from aiohttp import web

from ..i18n import tr

MAX_UPLOAD = 512 << 20


@contextlib.asynccontextmanager
async def upload(request, suffix, limit, too_big):
    """Тело запроса — во временный файл (путь — в with), по выходе файл удаляется. Больше limit байт —
    ValueError(too_big): вызывающий отвечает fail(str(e)), как и на ошибки разбора файла."""
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        size = 0
        with os.fdopen(fd, "wb") as f:
            async for chunk in request.content.iter_chunked(1 << 20):
                size += len(chunk)
                if size > limit:
                    raise ValueError(too_big)
                f.write(chunk)
        yield tmp
    finally:
        os.remove(tmp)


def ok(**kw):
    return web.json_response(dict(ok=True, **kw))


def fail(msg, status=400):
    return web.json_response(dict(ok=False, error=msg), status=status)


async def _restart_guard(ctx, data):
    """Действие перезапускает встроенный сервер синтеза, а обучение LoRA и его несохранённые результаты живут в
    процессе сервера: во время обучения — отказ, с несохранёнными результатами — вопрос (confirm, повтор с force).
    → ответ-отказ или None."""
    if not ctx.runtime:
        return None
    r = await ctx.train.guard(bool(data.get("force")))
    if r is None:
        return None
    text, confirm = r
    return web.json_response(dict(ok=False, error=text, confirm=confirm), status=409)


def json_post(fn):
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
