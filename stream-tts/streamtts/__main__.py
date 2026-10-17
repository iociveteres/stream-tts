"""Точка входа: python -m streamtts"""
import asyncio, contextlib, logging, os, shutil, sys

from aiohttp import web

from . import config, filters, install, runtime, stats, store, web as webapp
from .cosy import Cosy
from .engine import Engine
from .twitch import Twitch
from .voices import Voices

log = logging.getLogger("stream-tts")


class Ctx:
    admins = 0


async def cosy_monitor(ctx):
    """Доступность cosyvoice и список голосов."""
    last_list, last_start = 0, 0
    while True:
        was = ctx.cosy.ok
        await ctx.cosy.status()
        if not ctx.cosy.ok and ctx.runtime and ctx.runtime.state != "running":
            ctx.cosy.error = ctx.runtime.error or "cosyvoice-server запускается"
        # встроенный сервер перезапущен с новыми голосами (между проверками он мог не успеть пропасть)
        started = ctx.runtime.starts if ctx.runtime else 0
        if ctx.cosy.ok and (not was or started != last_start or asyncio.get_running_loop().time() - last_list > 60):
            last_start = started
            try:
                await ctx.voices.refresh()
                last_list = asyncio.get_running_loop().time()
                ctx.voices.wake.set()
            except Exception as e:
                log.warning("список голосов: %s", e)
        if was is not ctx.cosy.ok:
            log.info("cosyvoice %s", "доступен" if ctx.cosy.ok else f"недоступен: {ctx.cosy.error}")
            ctx.engine.dirty.set()
        await asyncio.sleep(5)


async def gpu_monitor(ctx):
    while True:
        if ctx.admins:
            await ctx.gpu.poll()
        await asyncio.sleep(2)


async def housekeeping(ctx):
    while True:
        ctx.db.purge()
        await asyncio.sleep(3600)


async def on_startup(app):
    ctx = app["ctx"]
    if ctx.runtime:
        await ctx.runtime.start()
    await ctx.cosy.start()
    await ctx.twitch.start()
    ctx.tasks = [asyncio.create_task(c) for c in (
        ctx.engine.synth_worker(), ctx.engine.player(), ctx.voices.demo_worker(),
        cosy_monitor(ctx), gpu_monitor(ctx), housekeeping(ctx))]


async def on_cleanup(app):
    ctx = app["ctx"]
    await ctx.twitch.stop()
    for t in ctx.tasks:
        t.cancel()
    await asyncio.gather(*ctx.tasks, return_exceptions=True)
    await ctx.cosy.close()
    if ctx.install:
        ctx.install.cancel()
    if ctx.runtime:
        await ctx.runtime.stop()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    if config.BIND not in config.LOCAL_BINDS and not config.PASSWORD:
        log.error("STREAM_TTS_BIND=%s открывает админку в сеть: задайте STREAM_TTS_PASSWORD в .env", config.BIND)
        sys.exit(1)
    os.makedirs(config.DATA, exist_ok=True)
    prof = os.path.join(config.DATA, "profanity.txt")
    if not os.path.exists(prof):
        shutil.copy(filters.default_profanity_file(), prof)

    ctx = Ctx()
    ctx.settings = store.Settings(os.path.join(config.DATA, "settings.json"))
    ctx.db = store.DB(os.path.join(config.DATA, "stream-tts.sqlite"))
    ctx.profanity = filters.Profanity(prof)
    # Режим: external (COSYVOICE_URL задан или встроенного сервера нет) или managed (runtime.py)
    url, ctx.runtime = config.COSYVOICE_URL, None
    if not url:
        server = runtime.find_server()
        if server:
            ctx.runtime = runtime.Managed(server, ctx.settings)
            url = ctx.runtime.url
        else:
            url = "http://cosyvoice:8080"
    ctx.cosy = Cosy(url, config.COSYVOICE_API_KEY)
    ctx.voices = Voices(ctx.settings, ctx.cosy)
    ctx.engine = Engine(ctx.settings, ctx.db, ctx.cosy, ctx.voices, ctx.profanity)
    ctx.voices.engine = ctx.engine
    ctx.twitch = Twitch(ctx.settings, ctx.engine)
    ctx.engine.twitch = ctx.twitch
    ctx.gpu = stats.GPU()
    # Первый запуск через админку (модель, устройство, наборы голосов) — только для встроенного сервера
    ctx.install = install.Installer(ctx.settings, ctx.runtime, ctx.engine.dirty.set) if ctx.runtime else None

    app = webapp.make_app(ctx)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    log.info("интерфейс: http://%s:%d  оверлей: /overlay  cosyvoice: %s%s",
             config.BIND if config.BIND != "0.0.0.0" else "<адрес>", config.PORT, url,
             f" (встроенный: {ctx.runtime.server})" if ctx.runtime else "")
    web.run_app(app, host=config.LISTEN_HOST, port=config.LISTEN_PORT, print=None, shutdown_timeout=5)


if __name__ == "__main__":
    main()
