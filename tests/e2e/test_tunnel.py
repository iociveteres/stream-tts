"""Страница голосов в интернете (tunnel.py, web/public.py, !ttsvoices) на поддельном cloudflared (fake_cloudflared.py):
кнопка включает и выключает туннель, через туннель видна только страница голосов, упавший cloudflared
перезапускается с новым адресом, включённый туннель поднимается после перезапуска программы, зритель получает адрес
командой. Загрузка cloudflared — с локального HTTP-сервера, с проверкой sha256."""
import asyncio, hashlib, json, os, signal, socket, sys, urllib.error, urllib.request

import pytest
from aiohttp import web

from harness import STREAM_TTS, Api, wait_for

sys.path.insert(0, STREAM_TTS)
from streamtts import downloads, tunnel  # noqa: E402

PINS = downloads.DOWNLOADS["cloudflared"]


def runs(server):
    """Запуски поддельного cloudflared: [{pid, args, url, hostname}]."""
    try:
        with open(os.path.join(server.tunnel_dir, "runs.jsonl"), encoding="utf-8") as f:
            return [json.loads(line) for line in f]
    except FileNotFoundError:
        return []


def get(url):
    """→ (код, заголовки, тело) без исключений на 4xx."""
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def metrics_up(run):
    port = int(run["args"][run["args"].index("--metrics") + 1].rsplit(":", 1)[1])
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def tunnel_state(api):
    return api.state()["tunnel"]


def wait_on(api, other_than=""):
    wait_for(lambda: (t := tunnel_state(api))["state"] == "on" and t["url"] != other_than, 15, "адреса туннеля")
    return tunnel_state(api)


@pytest.fixture
def tun(tts):
    """Процесс tts; после теста туннель выключен, файл отказа убран."""
    yield tts
    tts.api.ok("/api/tunnel", dict(on=False))
    fail = os.path.join(tts.server.tunnel_dir, "fail")
    if os.path.exists(fail):
        os.remove(fail)


def test_tunnel_serves_only_voices_page(tun):
    """Кнопка: адрес *.trycloudflare.com из метрик cloudflared; туннель смотрит на отдельный порт, где есть только
    страница голосов и примеры, с запретом индексации; «Закрыть» останавливает cloudflared."""
    api = tun.api
    assert tunnel_state(api) == dict(on=False, state="off", url="", error="", download=None, have=True,
                                     size=tunnel.PIN["size"])
    api.ok("/api/tunnel", dict(on=True))
    t = wait_on(api)
    run = runs(tun.server)[-1]
    assert t["url"] == f"https://{run['hostname']}/"
    assert run["args"][:2] == ["tunnel", "--no-autoupdate"]
    base = run["url"]
    assert base.startswith("http://127.0.0.1:") and not base.endswith(f":{tun.server.port}")

    code, headers, body = get(base + "/")
    page = body.decode()
    assert code == 200 and headers["X-Robots-Tag"] == "noindex, nofollow"
    voices = [v["name"] for v in api.state()["voices"] if v["enabled"]]
    assert voices and all(f"!ttssf {v}" in page for v in voices)
    code, headers, body = get(f"{base}/demos/{voices[0]}.mp3")
    assert code == 200 and headers["Content-Type"] == "audio/mpeg" and body
    for path in ("/api/state", "/overlay", "/login", "/voices/", "/voices.zip", "/static/admin.js",
                 "/demos/nobody.mp3", "/demos/../secret"):
        code, headers, _ = get(base + path)
        assert code == 404, path
        assert headers["X-Robots-Tag"] == "noindex, nofollow"

    api.ok("/api/tunnel", dict(on=False))
    assert tunnel_state(api)["state"] == "off" and not tunnel_state(api)["url"]
    wait_for(lambda: not metrics_up(run), 5, "остановки cloudflared")


def test_tunnel_restarts_with_new_address(tun):
    """Упавший cloudflared запускается снова, у туннеля новый адрес; отказ Cloudflare — ошибкой в админке и повтором."""
    api = tun.api
    api.ok("/api/tunnel", dict(on=True))
    first = wait_on(api)
    os.kill(runs(tun.server)[-1]["pid"], signal.SIGTERM)
    second = wait_on(api, other_than=first["url"])
    assert len(runs(tun.server)) >= 2 and second["url"] == f"https://{runs(tun.server)[-1]['hostname']}/"

    open(os.path.join(tun.server.tunnel_dir, "fail"), "w").close()
    os.kill(runs(tun.server)[-1]["pid"], signal.SIGTERM)
    wait_for(lambda: "1015" in tunnel_state(api)["error"], 10, "ошибки туннеля")
    assert tunnel_state(api)["state"] == "error" and not tunnel_state(api)["url"]
    os.remove(os.path.join(tun.server.tunnel_dir, "fail"))
    wait_on(api)


def test_tunnel_back_after_restart(spawn):
    """Включённый туннель поднимается при следующем запуске программы, выключенный — нет. Убитая программа не
    оставляет cloudflared (Job Object на Windows, PR_SET_PDEATHSIG на Linux)."""
    server, cosy = spawn()
    Api(server.base).ok("/api/tunnel", dict(on=True))
    wait_on(Api(server.base))
    server.stop()
    wait_for(lambda: not metrics_up(runs(server)[-1]), 5, "cloudflared вместе с программой")
    again, _ = spawn(workdir=server.dir, cosy=cosy)
    api = Api(again.base)
    t = wait_on(api)
    assert t["on"] and t["url"] == f"https://{runs(again)[-1]['hostname']}/"
    api.ok("/api/tunnel", dict(on=False))
    again.stop()
    third, _ = spawn(workdir=server.dir, cosy=cosy)
    assert tunnel_state(Api(third.base))["state"] == "off"


def test_ttsvoices_replies_with_address(twitch):
    """!ttsvoices — ответом на сообщение текущий адрес; туннель выключен — молча; повтор сразу — молча (LINK_EVERY)."""
    voice = twitch.api.state()["voices"][0]["name"]

    def barrier(n):
        """Всё отправленное в чат раньше обработано: заметка о выборе голоса — в журнале."""
        twitch.say("Viewer", f"!ttssf {voice}")
        wait_for(lambda: sum("выбрал голос" in j["text"] for j in twitch.api.state()["engine"]["journal"]) >= n,
                 5, "обработки чата")

    twitch.say("Viewer", "!ttsvoices")   # туннель выключен
    barrier(1)
    twitch.api.ok("/api/tunnel", dict(on=True))
    try:
        url = wait_on(twitch.api)["url"]
        mid = twitch.say("Viewer", "!ttsvoices")
        wait_for(lambda: twitch.fake.chat, 5, "ответа в чат")
        twitch.say("Other", "!ttsvoices")
        barrier(2)
        assert twitch.fake.chat == [dict(broadcaster_id="1000", sender_id="1000", reply_parent_message_id=mid,
                                         message=f"голоса и примеры звучания: {url}")]
    finally:
        twitch.api.ok("/api/tunnel", dict(on=False))


# --- загрузка cloudflared ---
def test_pins():
    """Закреплены оба файла одной версии из релизов cloudflare/cloudflared."""
    assert set(PINS) == {"version", "windows", "linux"}
    for system in ("windows", "linux"):
        p = PINS[system]
        assert p["url"].startswith(f"https://github.com/cloudflare/cloudflared/releases/download/{PINS['version']}/")
        assert p["size"] > 10 << 20 and len(p["sha256"]) == 64


class Settings(dict):
    def update(self, **kw):
        super().update(kw)


def test_download(tmp_path, monkeypatch):
    """Первое включение без cloudflared качает его с докачкой и проверкой sha256; неверная сумма — ошибка без файла."""
    data = os.urandom(300_000)
    sha = hashlib.sha256(data).hexdigest()

    async def serve(request):
        return web.Response(body=data)

    async def run():
        app = web.Application()
        app.router.add_get("/cf", serve)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        url = f"http://127.0.0.1:{runner.addresses[0][1]}/cf"
        try:
            t = tunnel.Tunnel(Settings(), lambda: None)
            monkeypatch.setattr(tunnel, "PIN", dict(url=url, size=len(data), sha256="0" * 64))
            with pytest.raises(RuntimeError, match="контрольная сумма"):
                await t._download()
            assert not tunnel.binary()
            monkeypatch.setattr(tunnel, "PIN", dict(url=url, size=len(data), sha256=sha))
            path = await t._download()
            assert open(path, "rb").read() == data and tunnel.binary() == path
            assert t.dl["done"] == len(data)
        finally:
            await runner.cleanup()

    monkeypatch.delenv("STREAM_TTS_CLOUDFLARED", raising=False)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(tunnel, "DIR", str(tmp_path / "cloudflared"))
    asyncio.run(run())
