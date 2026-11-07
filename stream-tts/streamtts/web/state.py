"""Состояние для админки: GET /api/state и сокет админки /api/ws (полное состояние, затем изменения пачками)."""
import asyncio, contextlib, time

from aiohttp import web

from .. import access, config, install, separate
from ..i18n import tr
from .admin import SETTINGS_KEYS


def state(ctx, full=True):
    s = ctx.settings
    out = dict(
        engine=ctx.engine.snapshot(),
        twitch=ctx.twitch.public(),
        # сервер синтеза выбранной модели (ключ cosy — с тех пор, когда модель была одна)
        cosy=dict(ok=ctx.tts.ok, error=ctx.tts.error, url=ctx.tts.url,
                  busy=ctx.tts.lane.holder[1] if ctx.tts.lane.holder else "", waiting=ctx.tts.lane.queued(),
                  runtime=ctx.runtime.public() if ctx.runtime else None,
                  engine=ctx.tts.id, picked=ctx.tts.picked, title=ctx.tts.title, engines=ctx.tts.engines(),
                  switching=ctx.tts.switching,
                  capabilities=ctx.tts.capabilities, params=model_params(ctx),
                  # обучение LoRA на espeech-server: state, voice, step, steps (из /status, раз в COSY_CHECK)
                  training=ctx.tts.info.get("training") if ctx.tts.id == "espeech" else None),
        load=ctx.load.public(history=full),
        models=models(ctx),
        demos=dict(ctx.voices.progress),
        setup=ctx.install.public() if ctx.install else None,
        # необязательные пакеты; у готового сервера в тестах файлов не видно — о фронтенде говорит GET /frontend/model
        packages=install.packages() if ctx.install else install.packages(dict(frontend=ctx.tts.frontend_ready)),
        bench=ctx.bench.public() if ctx.bench else None,
        update=ctx.updates.public(),
        tunnel=ctx.tunnel.public(),   # страница голосов в интернете (tunnel.py)
        ts=time.time(),
    )
    if full:
        out.update(
            settings={k: s[k] for k in SETTINGS_KEYS},
            voices=ctx.voices.public(),
            bans=ctx.db.bans(),
            # password: "env" | "file" | None; refused — последний отказ без пароля «не из локальной сети»
            config=dict(password=access.source(), refused=access.refused if not access.source() else None,
                        bind=config.BIND, port=config.PORT, min_password=access.MIN_LEN),
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


GETS = {
    "/api/state": api_state,
    "/api/ws": admin_ws,
}
