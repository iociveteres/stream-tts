"""Запуск и доступ: сервис поднимается с поддельным cosyvoice, пароль (из переменной или заданный в админке) закрывает
админку и API, но не оверлей и страницу голосов; без пароля пускается только локальная сеть (адрес, прокси, Host:
DNS rebinding, публичное имя или IP); POST в API только JSON со своего адреса, сокеты — без чужого Origin, настройки
(и скрытые подсказки) переживают перезапуск, остановка по SIGTERM не ждёт открытых сокетов."""
import asyncio, os, re, time, urllib.parse

import aiohttp
import pytest

from harness import Api, Overlay, wait_for


def test_startup_voices_and_demos(tts):
    """Голоса берутся из GET /speaker, примеры синтезируются сами, страница голосов и mp3 отдаются."""
    s = tts.api.state()
    assert s["cosy"]["ok"] and s["cosy"]["url"] == tts.cosy.url
    assert s["config"]["password"] is None
    assert [v["name"] for v in s["voices"]] == sorted(tts.cosy.voices)
    assert all(v["enabled"] and v["demo"] for v in s["voices"])
    page = tts.api.get("/voices/")
    assert page.status == 200
    for name in tts.cosy.voices:
        assert f"!ttssf {name}" in page.text
        mp3 = tts.api.get(f"/voices/demos/{name}.mp3")
        assert mp3.status == 200 and len(mp3.body) > 1000
    assert tts.api.get("/").status == 200
    assert tts.api.get("/overlay").status == 200


def test_cosy_down_and_back(tts):
    """Пропажа cosyvoice видна в состоянии, после возвращения сервис снова готов (проверка раз в 5 с)."""
    tts.cosy.down = True
    wait_for(lambda: tts.api.state()["cosy"]["ok"] is False, 12, "cosy.ok = false")
    assert tts.api.state()["cosy"]["error"]
    tts.cosy.down = False
    wait_for(lambda: tts.api.state()["cosy"]["ok"], 12, "cosy.ok = true")


def test_password(spawn):
    server, _ = spawn(env=dict(STREAM_TTS_PASSWORD="secret"), password="secret")
    api = Api(server.base)
    # без входа: API — 401, админка — на страницу входа, открытые страницы доступны
    assert api.get("/api/state").status == 401
    r = api.get("/")
    assert r.status == 302 and r.headers["Location"] == "/login"
    for path in ("/login", "/overlay", "/voices/", "/static/overlay.html"):
        assert api.get(path).status == 200, path
    assert api.post("/api/enabled", dict(on=False)).status == 401
    # неверный пароль
    r = api.login("wrong")
    assert r.status == 302 and r.headers["Location"] == "/login?bad=1"
    assert api.get("/api/state").status == 401
    # верный: cookie, дальше API доступно
    r = api.login("secret")
    assert r.status == 302 and r.headers["Location"] == "/"
    s = api.state()
    assert s["config"]["password"] == "env" and s["config"]["refused"] is None
    # с паролем пускается и не локальная сеть (прокси, туннель), но только после входа
    assert api.get("/api/state", headers={"X-Forwarded-For": "93.184.216.34"}).status == 200
    assert Api(server.base).get("/api/state", headers={"X-Forwarded-For": "93.184.216.34"}).status == 401
    # док-панель OBS: вход возвращает на неё, а не в админку; чужой адрес возврата — в админку
    other = Api(server.base)
    where = lambda r: urllib.parse.unquote(r.headers["Location"])
    assert where(other.get("/dock")) == "/login?next=/dock"
    assert where(other.request("POST", "/login", data=b"password=wrong&next=%2Fdock",
                               headers={"Content-Type": "application/x-www-form-urlencoded"})) == "/login?bad=1&next=/dock"
    for nxt, want in (("/dock", "/dock"), ("//evil.example", "/"), ("https://evil.example", "/")):
        r = other.request("POST", "/login", data=urllib.parse.urlencode(dict(password="secret", next=nxt)).encode(),
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert where(r) == want, nxt
    # выход стирает cookie
    assert api.get("/logout").status == 302
    assert api.get("/api/state").status == 401


def test_post_guard(tts):
    """POST в API: чужой Origin — 403, не JSON — 415; свой Origin проходит."""
    r = tts.api.post("/api/enabled", dict(on=False), headers={"Origin": "http://evil.example"})
    assert r.status == 403
    r = tts.api.request("POST", "/api/enabled", data=b'{"on": false}', headers={"Content-Type": "text/plain"})
    assert r.status == 415
    assert tts.api.state()["engine"]["enabled"] is True
    r = tts.api.post("/api/enabled", dict(on=True), headers={"Origin": tts.server.base})
    assert r.status == 200


def test_local_network_only_without_password(tts):
    """Без пароля — только локальная сеть (access.local_request). В Host: чужое имя — 403 (DNS rebinding: сайт, чьё
    имя ведёт на адрес в сети, получил бы админку), публичное имя или IP — 403 (DDNS, проброс порта: в Docker Desktop
    адрес клиента всегда частный, и интернет видно только по Host); IP локальной сети и локальные имена проходят.
    Запрос через прокси или туннель (X-Forwarded-For и др.) — 403. Последний отказ админка показывает."""
    port = tts.server.port
    for host in ("evil.example", f"evil.example:{port}", "127.0.0.1.nip.io", f"myhome.duckdns.org:{port}",
                 f"93.184.216.34:{port}", f"[2606:4700::1]:{port}"):
        assert tts.api.get("/api/state", headers={"Host": host}).status == 403, host
        assert tts.api.get("/overlay", headers={"Host": host}).status == 403, host
    for host in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", f"192.168.1.10:{port}",
                 f"10.0.0.5:{port}", f"homeserver:{port}", f"nas.local:{port}", f"server.lan:{port}",
                 f"box.home.arpa:{port}", f"100.101.102.103:{port}"):
        assert tts.api.get("/api/state", headers={"Host": host}).status == 200, host
    for header in ("X-Forwarded-For", "Forwarded", "X-Real-IP", "CF-Connecting-IP"):
        r = tts.api.get("/api/state", headers={header: "93.184.216.34"})
        assert r.status == 403 and "пароль" in r.text, header
    refused = tts.api.state()["config"]["refused"]
    assert refused["addr"] == "93.184.216.34" and refused["host"] == f"127.0.0.1:{port}"   # клиент, а не прокси
    assert time.time() - refused["ts"] < 60


def test_websocket_origin(tts, loop):
    """Сокеты админки и оверлея с чужой страницы — 403: WebSocket браузер открывает без CORS, и чужая страница читала
    бы очередь и настройки или перехватывала звук оверлея. Свой Origin и клиент без Origin (OBS, тесты) проходят."""
    async def status(path, origin):
        async with aiohttp.ClientSession() as s:
            try:
                async with s.ws_connect(tts.server.base.replace("http", "ws", 1) + path, origin=origin) as ws:
                    await ws.close()
                    return 101
            except aiohttp.WSServerHandshakeError as e:
                return e.status
    for path in ("/api/ws", "/overlay/ws"):
        assert loop.run(status(path, "http://evil.example")) == 403, path
        assert loop.run(status(path, tts.server.base)) == 101, path


def test_password_not_ascii(spawn):
    """Пароль по-русски: compare_digest со строками не-ASCII бросал TypeError, и вход отвечал 500."""
    server, _ = spawn(env=dict(STREAM_TTS_PASSWORD="секрет"), password="секрет")
    api = Api(server.base)
    assert api.login("неверно").headers["Location"] == "/login?bad=1"
    assert api.login("секрет").headers["Location"] == "/"
    assert api.state()["config"]["password"] == "env"
    # с паролем Host не проверяется, чужую страницу останавливает cookie
    assert Api(server.base).get("/api/state", headers={"Host": "evil.example"}).status == 401


def test_home_server_by_name(spawn, loop):
    """Домашний сервер в Docker (BIND=0.0.0.0, внутри контейнера слушает свой адрес) без пароля и настроек: стартует,
    с ПК в сети его открывают по имени — админка, POST и оверлей OBS (сокет с Origin того же имени) работают,
    проверка Host против DNS rebinding их не задевает."""
    server, _ = spawn(env=dict(STREAM_TTS_BIND="0.0.0.0", STREAM_TTS_LISTEN_HOST="127.0.0.1"))
    host = f"homeserver:{server.port}"
    api = Api(server.base)
    s = api.get("/api/state", headers={"Host": host})
    assert s.status == 200 and s.json()["config"]["password"] is None and s.json()["config"]["bind"] == "0.0.0.0"
    assert api.request("POST", "/api/enabled", json_body=dict(on=True),
                       headers={"Host": host, "Origin": f"http://{host}"}).status == 200
    assert Api(server.base).get("/overlay", headers={"Host": host}).status == 200

    async def overlay():
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(server.base.replace("http", "ws", 1) + "/overlay/ws", origin=f"http://{host}",
                                    headers={"Host": host}) as ws:
                return (await ws.receive_json(timeout=5))["type"]
    assert loop.run(overlay()) == "active"


INTERNET = {"X-Forwarded-For": "93.184.216.34"}   # запрос как из интернета: через прокси адрес клиента неизвестен


def cookie(api):
    return next(c.value for c in api.jar if c.name == "stream_tts")


def test_password_from_ui(spawn):
    """Пароль из админки (POST /api/password): задать без текущего — эта вкладка остаётся внутри (cookie из ответа),
    другие — на вход, из интернета — после входа; сменить — с текущим паролем, старые входы выходят; снять — только
    из локальной сети, и снова пускается без входа. Короткий пароль и неверный текущий — отказ."""
    server, _ = spawn()
    me, other = Api(server.base), Api(server.base)
    assert other.state()["config"]["password"] is None
    r = me.post("/api/password", dict(new="1234567"))
    assert r.status == 400 and "8" in r.json()["error"]
    assert me.ok("/api/password", dict(new="первый пароль"))["password"] == "file"
    assert me.state()["config"]["password"] == "file"
    assert other.get("/api/state").status == 401
    assert other.get("/").headers["Location"] == "/login"
    assert other.get("/overlay").status == 200
    assert other.login("первый пароль").headers["Location"] == "/"
    assert other.get("/api/state", headers=INTERNET).status == 200
    # смена: без текущего и с неверным — 403, с верным — новый токен у меня, старый у other не действует
    assert me.post("/api/password", dict(new="второй пароль")).status == 403
    assert me.post("/api/password", dict(current="не тот", new="второй пароль")).status == 403
    old = cookie(me)
    me.ok("/api/password", dict(current="первый пароль", new="второй пароль"))
    assert cookie(me) != old and me.get("/api/state").status == 200
    assert other.get("/api/state").status == 401
    assert other.login("первый пароль").headers["Location"] == "/login?bad=1"
    assert other.login("второй пароль").headers["Location"] == "/"
    # снять из интернета нельзя: следующий запрос оттуда без пароля — 403, вход пропал бы
    r = other.request("POST", "/api/password", json_body=dict(current="второй пароль", new=""), headers=INTERNET)
    assert r.status == 409 and r.json()["error"]
    assert me.state()["config"]["password"] == "file"
    me.ok("/api/password", dict(current="второй пароль", new=""))
    assert Api(server.base).state()["config"]["password"] is None
    assert Api(server.base).get("/api/state", headers=INTERNET).status == 403


def test_password_file_hashed(spawn):
    """Пароль из админки — scrypt-хэш с солью в <данные>/admin-password, не открытым текстом; переживает перезапуск.
    Удалить файл и перезапустить — восстановление забытого пароля."""
    server, _ = spawn()
    Api(server.base).ok("/api/password", dict(new="пароль-на-диске"))
    path = os.path.join(server.data, "admin-password")
    with open(path, encoding="utf-8") as f:
        line = f.read().strip()
    assert line.startswith("scrypt$") and "пароль" not in line and len(line.split("$")) == 6
    with open(os.path.join(server.data, "settings.json"), encoding="utf-8") as f:
        assert "scrypt" not in f.read()
    server.stop()

    again, _ = spawn(workdir=server.dir, password="пароль-на-диске")
    api = Api(again.base)
    assert api.get("/api/state").status == 401
    assert api.login("пароль-на-диске").headers["Location"] == "/"
    assert api.state()["config"]["password"] == "file"
    again.stop()

    os.remove(path)
    third, _ = spawn(workdir=server.dir)
    assert Api(third.base).state()["config"]["password"] is None


def test_env_password_locks_ui(spawn):
    """STREAM_TTS_PASSWORD главнее пароля из админки: менять его там нельзя (409), файл не читается."""
    server, _ = spawn(start=False)
    os.makedirs(server.data, exist_ok=True)
    with open(os.path.join(server.data, "admin-password"), "w", encoding="utf-8") as f:
        f.write("scrypt$16384$8$1$AAAA$AAAA\n")
    server.extra["STREAM_TTS_PASSWORD"] = "из-переменной"
    server.start("из-переменной")
    api = Api(server.base)
    api.login("из-переменной")
    assert api.state()["config"]["password"] == "env"
    r = api.post("/api/password", dict(current="из-переменной", new="другой пароль"))
    assert r.status == 409 and "STREAM_TTS_PASSWORD" in r.json()["error"]
    assert Api(server.base).login("из-переменной").headers["Location"] == "/"


def test_password_change_closes_sockets(spawn, loop):
    """Смена пароля закрывает сокеты админки кодом 4001 (admin.js: чужая вкладка перезагружается и уходит на вход),
    сокет оверлея не трогает."""
    server, _ = spawn()
    api = Api(server.base)
    ws_base = server.base.replace("http", "ws", 1)

    async def run():
        async with aiohttp.ClientSession() as s:
            admin = await s.ws_connect(ws_base + "/api/ws")
            await admin.receive_json(timeout=5)
            overlay = await s.ws_connect(ws_base + "/overlay/ws")
            await overlay.receive_json(timeout=5)
            await asyncio.to_thread(api.ok, "/api/password", dict(new="закрыть сокеты"))
            while (msg := await admin.receive(timeout=5)).type == aiohttp.WSMsgType.TEXT:
                pass
            assert msg.type == aiohttp.WSMsgType.CLOSE and msg.data == 4001, msg
            assert not overlay.closed
            # новый сокет со старой cookie (её нет) — 401, с новой — пускается
            try:
                await s.ws_connect(ws_base + "/api/ws")
                raise AssertionError("сокет админки открылся без входа")
            except aiohttp.WSServerHandshakeError as e:
                assert e.status == 401
            again = await s.ws_connect(ws_base + "/api/ws", headers={"Cookie": f"stream_tts={cookie(api)}"})
            assert (await again.receive_json(timeout=5))["config"]["password"] == "file"
            await again.close()
            await overlay.close()
    loop.run(run())


def test_settings_survive_restart(spawn):
    server, _ = spawn()
    api = Api(server.base)
    api.ok("/api/settings", dict(queue_limit=7, read_nick=True))
    api.ok("/api/voice", dict(name="bravo", enabled=False))
    api.ok("/api/ban", dict(login="troll"))
    server.stop()

    again, _ = spawn(workdir=server.dir)
    s = Api(again.base).state()
    assert s["settings"]["queue_limit"] == 7 and s["settings"]["read_nick"] is True
    assert {v["name"]: v["enabled"] for v in s["voices"]}["bravo"] is False
    assert s["bans"] == ["troll"]


def test_hidden_hints(spawn):
    """Скрываемые подсказки админки (data-hint): скрытая крестиком подсказка — id в настройках, а не в браузере,
    поэтому остаётся скрытой после перезагрузки страницы и перезапуска сервиса; «Показать подсказки» — пустой
    список. Чужие значения (не id из разметки) не сохраняются."""
    server, _ = spawn()
    api = Api(server.base)
    ids = re.findall(r'data-hint="([\w-]+)"', api.get("/").text)
    assert {"ed-transcript", "ed-accents", "ed-save"} <= set(ids)
    assert api.state()["settings"]["hidden_hints"] == []
    api.ok("/api/settings", dict(hidden_hints=["ed-transcript", "ed-transcript", "<b>x</b>", 5, "ed-save"]))
    assert api.state()["settings"]["hidden_hints"] == ["ed-save", "ed-transcript"]
    server.stop()

    again, _ = spawn(workdir=server.dir)
    api = Api(again.base)
    assert api.state()["settings"]["hidden_hints"] == ["ed-save", "ed-transcript"]
    api.ok("/api/settings", dict(hidden_hints=[]))
    assert api.state()["settings"]["hidden_hints"] == []


@pytest.mark.skipif(os.name == "nt", reason="на Windows terminate — TerminateProcess, корректной остановки нет")
def test_sigterm_with_open_sockets(spawn, loop):
    """SIGTERM (docker stop) при открытых админке и оверлее: сокеты закрываются при остановке (web.close_sockets), и
    процесс выходит сразу. Без этого aiohttp ждал их shutdown_timeout дважды — 10 с, и docker stop добивал SIGKILL."""
    server, _ = spawn()
    ov = Overlay(loop, server.base).connect()

    async def admin():
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(server.base.replace("http", "ws", 1) + "/api/ws")
        await ws.receive_json()

        async def read():   # клиент отвечает на закрытие, только пока читает
            async for _ in ws:
                pass
        return session, ws, asyncio.create_task(read())
    session, ws, reader = loop.run(admin())

    t0 = time.monotonic()
    server.proc.terminate()
    server.proc.wait(15)
    took = time.monotonic() - t0
    ov.close()

    async def close():
        reader.cancel()
        await ws.close()
        await session.close()
    loop.run(close())
    assert took < 3, f"остановка {took:.1f} с\n{server.tail()}"
