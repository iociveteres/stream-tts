"""Запуск и доступ: сервис поднимается с поддельным cosyvoice, пароль закрывает админку и API, но не оверлей и страницу
голосов, POST в API только JSON со своего адреса, настройки (и скрытые подсказки) переживают перезапуск, остановка по
SIGTERM не ждёт открытых сокетов."""
import asyncio, os, re, subprocess, time

import aiohttp
import pytest

from harness import Api, Overlay, wait_for


def test_startup_voices_and_demos(tts):
    """Голоса берутся из GET /speaker, примеры синтезируются сами, страница голосов и mp3 отдаются."""
    s = tts.api.state()
    assert s["cosy"]["ok"] and s["cosy"]["url"] == tts.cosy.url
    assert s["config"]["password"] is False
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
    assert s["config"]["password"] is True
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


def test_public_bind_needs_password(spawn):
    """Адрес не localhost без пароля: сервис не стартует и объясняет почему."""
    server, _ = spawn(env=dict(STREAM_TTS_BIND="0.0.0.0"), start=False)
    server.spawn()
    try:
        code = server.proc.wait(30)
    except subprocess.TimeoutExpired:
        raise AssertionError("stream-tts запустился без пароля на 0.0.0.0")
    assert code == 1
    assert "STREAM_TTS_PASSWORD" in server.tail()


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
