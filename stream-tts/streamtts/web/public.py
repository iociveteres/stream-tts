"""Страница голосов и примеры: в основном приложении (/voices/) и на отдельном порту для туннеля (tunnel.py).

Отдельный порт слушает только 127.0.0.1, и на нём нет ничего, кроме страницы и mp3 примеров: туннель открывает его в
интернет, а админка, вход, оверлей и API туда не попадают."""
from aiohttp import web

# Страница уходит в интернет: не индексировать, не передавать адрес дальше по ссылкам
HEADERS = {"X-Robots-Tag": "noindex, nofollow", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}


async def voices_page(request):
    return web.Response(text=request.app["ctx"].voices.page(), content_type="text/html",
                        headers={"Cache-Control": "no-cache"})


async def demo(request):
    v = request.app["ctx"].voices
    name = request.match_info["name"]
    if name not in v.names() or not v.has_demo(name):
        raise web.HTTPNotFound()
    return web.FileResponse(v.demo_path(name), headers={"Cache-Control": "no-cache"})


@web.middleware
async def _headers(request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as e:
        e.headers.update(HEADERS)
        raise
    resp.headers.update(HEADERS)
    return resp


async def start(ctx):
    """Порт для туннеля: случайный свободный на 127.0.0.1. → (runner, порт); runner.cleanup() — остановка."""
    app = web.Application(middlewares=[_headers])
    app["ctx"] = ctx
    app.router.add_get("/", voices_page)
    app.router.add_get("/demos/{name}.mp3", demo)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, runner.addresses[0][1]
