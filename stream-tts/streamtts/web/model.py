"""Модель синтеза (engines/): смена модели, «как прочитает» и словарь ударений пользователя. Модель одна
единовременно, смена — перезапуск встроенного сервера."""
import asyncio, logging

from .. import filters
from ..i18n import tr
from .common import _restart_guard, fail, ok

log = logging.getLogger("web")


async def post_engine(request, ctx, data):
    engine = str(data.get("engine", ""))
    if engine not in ctx.tts.available:
        return fail(tr("модель {engine} недоступна", engine=engine))
    if ctx.bench and ctx.bench.task and not ctx.bench.task.done():
        return fail(tr("идёт проверка скорости"))
    if stop := await _restart_guard(ctx, data):
        return stop

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


# Словарь ударений пользователя (capabilities.user_accents): хранит сервер модели, espeech-server
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


GETS = {
    "/api/accents": api_accents,
}
POSTS = {
    "/api/engine": post_engine,
    "/api/accent": post_accent,
    "/api/accents": post_accents,
    "/api/accents/delete": post_accents_delete,
}
