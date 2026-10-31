"""Точка входа: python -m streamtts"""
import asyncio, contextlib, logging, os, shutil, sys

from aiohttp import web

from . import bench, config, editor, engines, filters, i18n, install, paths, stats, store, updates, web as webapp
from .i18n import tr
from .engine import Engine
from .twitch import Twitch
from .voices import Voices

log = logging.getLogger("stream-tts")

# Переменные STREAM_TTS_COSY_CHECK и STREAM_TTS_VOICES_CHECK — для e2e-тестов (tests/e2e): с между проверками
# сервера синтеза и между перечитываниями списка голосов
COSY_CHECK = float(paths.env("STREAM_TTS_COSY_CHECK", "5"))
VOICES_CHECK = float(paths.env("STREAM_TTS_VOICES_CHECK", "60"))


class Ctx:
    admins = 0


async def tts_monitor(ctx):
    """Доступность сервера синтеза и список голосов."""
    last_list, last_start = 0, None
    while True:
        was = ctx.tts.ok
        await ctx.tts.status()
        if not ctx.tts.ok and ctx.runtime and ctx.runtime.state != "running":
            ctx.tts.error = ctx.runtime.error or tr("{server} запускается", server=ctx.runtime.launcher.NAME)
        # встроенный сервер перезапущен с новыми голосами (между проверками он мог не успеть пропасть) или сменилась
        # модель: у неё свои голоса
        started = (ctx.runtime.starts if ctx.runtime else 0, ctx.tts.switches)
        stale = asyncio.get_running_loop().time() - last_list > VOICES_CHECK
        if ctx.tts.ok and (not was or started != last_start or stale):
            last_start = started
            try:
                await ctx.voices.refresh()
                last_list = asyncio.get_running_loop().time()
                ctx.voices.wake.set()
            except Exception as e:
                log.warning("список голосов: %s", e)
        if was is not ctx.tts.ok:
            log.info("%s %s", ctx.tts.id, "доступен" if ctx.tts.ok else f"недоступен: {ctx.tts.error}")
            ctx.engine.dirty.set()
        await asyncio.sleep(COSY_CHECK)


async def load_monitor(ctx):
    while True:
        if ctx.admins:
            await ctx.load.poll()
        await asyncio.sleep(2)


async def housekeeping(ctx):
    while True:
        ctx.db.purge()
        await asyncio.sleep(3600)


async def on_startup(app):
    ctx = app["ctx"]
    if ctx.runtime:
        await ctx.runtime.start()
    await ctx.tts.start()
    await ctx.twitch.start()
    ctx.tasks = [asyncio.create_task(c) for c in (
        ctx.engine.synth_worker(), ctx.engine.player(), ctx.voices.demo_worker(),
        tts_monitor(ctx), load_monitor(ctx), housekeeping(ctx), ctx.updates.worker())]


async def on_cleanup(app):
    ctx = app["ctx"]
    await ctx.twitch.stop()
    for t in ctx.tasks:
        t.cancel()
    await asyncio.gather(*ctx.tasks, return_exceptions=True)
    await ctx.tts.close()
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
    i18n.init(ctx.settings)
    i18n.load_edits()
    ctx.db = store.DB(os.path.join(config.DATA, "stream-tts.sqlite"))
    ctx.profanity = filters.Profanity(prof)
    # Сервер выбранной модели stream-tts запускает сам (runtime.py, engines/<id>/launch.py); готовые серверы — только
    # в тестах (config.TEST_SERVERS)
    servers, launchers = {}, {}
    for k, url in config.TEST_SERVERS.items():
        if k in engines.ENGINES:
            servers[k] = url
        else:
            log.warning("STREAM_TTS_TEST_SERVERS: неизвестная модель %s (есть: %s)", k, ", ".join(engines.ENGINES))
    if not servers:
        launchers = {k: E.Launcher(p, ctx.settings) for k, E in engines.ENGINES.items() if (p := E.find_server())}
        if not launchers:
            log.error("не найден сервер синтеза (%s)", ", ".join(E.Launcher.NAME for E in engines.ENGINES.values()))
            sys.exit(1)
    ctx.tts = engines.Tts(ctx.settings, servers, launchers)
    ctx.runtime = ctx.tts.runtime
    ctx.voices = Voices(ctx.settings, ctx.tts)
    ctx.engine = Engine(ctx.settings, ctx.db, ctx.tts, ctx.voices, ctx.profanity)
    ctx.voices.engine = ctx.engine
    ctx.twitch = Twitch(ctx.settings, ctx.engine)
    ctx.engine.twitch = ctx.twitch
    ctx.load = stats.Load()
    # Первый запуск через админку (модель, устройство, наборы голосов); в тестах с готовым сервером их нет
    ctx.install = install.Installer(ctx.settings, ctx.runtime, ctx.engine.dirty.set,
                                    launchers.get("cosyvoice"), launchers.get("espeech")) if ctx.runtime else None
    ctx.editor = editor.Editor(ctx.settings, ctx.tts, ctx.voices, ctx.runtime)
    ctx.bench = bench.Bench(ctx.settings, ctx.tts, ctx.runtime, ctx.voices, ctx.engine) if ctx.runtime else None
    ctx.updates = updates.Updates(ctx.settings, ctx.engine.dirty.set)

    app = webapp.make_app(ctx)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    log.info("интерфейс: http://%s:%d  оверлей: /overlay  синтез: %s %s%s",
             config.BIND if config.BIND != "0.0.0.0" else "<адрес>", config.PORT, ctx.tts.title, ctx.tts.url,
             f" (встроенный: {ctx.runtime.server})" if ctx.runtime else "")
    web.run_app(app, host=config.LISTEN_HOST, port=config.LISTEN_PORT, print=None, shutdown_timeout=5)


if __name__ == "__main__":
    main()
