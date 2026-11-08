"""Админка в настоящем браузере: headless Edge или Chrome через DevTools Protocol (CDP), ~10 с.

stream-tts на английском с двумя поддельными серверами синтеза (CosyVoice с ONNX-фронтендом, ESpeech с обучением
LoRA). Тест открывает админку, проходит все вкладки у обеих моделей, затем док-панель OBS (/dock), и падает:
- на исключениях JS и console.error (в том числе обращение к функции из ещё не загруженного скрипта при старте);
- на ошибках загрузки ресурсов (скрипт из admin.html, которого нет);
- на строках разметки без перевода — [...I18N_MISSING] из static/i18n.js (раньше — ручной просмотр консоли).

Браузер — msedge/chrome/chromium из PATH или стандартных папок; нет браузера — тест пропускается. CDP — по
WebSocket через aiohttp, без новых зависимостей. Профиль браузера — во временной папке."""
import asyncio, json, os, shutil, subprocess, time

import aiohttp
import pytest

from conftest import FAST_ENV
from fake_cosyvoice import FakeCosy
from harness import Api, StreamTTS, wait_for

BROWSERS = ("msedge", "chrome", "chromium", "chromium-browser", "google-chrome", "microsoft-edge")
BROWSER_PATHS = [os.path.join(os.environ.get(v, ""), *p) for v in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")
                 for p in (("Microsoft", "Edge", "Application", "msedge.exe"),
                           ("Google", "Chrome", "Application", "chrome.exe"))]


def find_browser():
    for name in BROWSERS:
        if path := shutil.which(name):
            return path
    return next((p for p in BROWSER_PATHS if os.path.isfile(p)), None)


class Page:
    """Вкладка браузера по CDP: команды и события (ошибки JS, консоль, журнал загрузки)."""

    def __init__(self, ws):
        self.ws, self.n, self.waiting, self.errors = ws, 0, {}, []
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        async for msg in self.ws:
            m = json.loads(msg.data)
            if "id" in m:
                self.waiting.pop(m["id"]).set_result(m)
            elif m.get("method") == "Runtime.exceptionThrown":
                d = m["params"]["exceptionDetails"]
                self.errors.append(f"исключение: {d.get('exception', {}).get('description') or d.get('text')}")
            elif m.get("method") == "Runtime.consoleAPICalled" and m["params"]["type"] in ("error", "assert"):
                args = m["params"]["args"]
                self.errors.append("console.error: " + " ".join(str(a.get("value", a.get("description"))) for a in args))
            elif m.get("method") == "Log.entryAdded" and m["params"]["entry"]["level"] == "error":
                e = m["params"]["entry"]
                if e.get("url", "").endswith("/favicon.ico"):   # браузер просит сам, у админки иконки нет
                    continue
                self.errors.append(f"журнал: {e['text']} {e.get('url', '')}")

    async def call(self, method, **params):
        self.n += 1
        fut = self.waiting[self.n] = asyncio.get_running_loop().create_future()
        await self.ws.send_json(dict(id=self.n, method=method, params=params))
        m = await asyncio.wait_for(fut, 15)
        assert "error" not in m, (method, m["error"])
        return m["result"]

    async def js(self, expr):
        r = await self.call("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        assert "exceptionDetails" not in r, (expr, r["exceptionDetails"])
        return r["result"].get("value")

    async def until(self, expr, what, timeout=10):
        deadline = time.monotonic() + timeout
        while not await self.js(expr):
            assert time.monotonic() < deadline, f"не дождались: {what}"
            await asyncio.sleep(0.1)


async def visit_tabs(page, base):
    """Админка с нуля, затем каждая видимая вкладка → id вкладок."""
    await page.call("Page.navigate", url=base + "/")
    await page.until("typeof S !== 'undefined' && S !== null && document.readyState === 'complete'", "загрузки админки")
    tabs = await page.js("[...document.querySelectorAll('#tabs button')].filter(b => !b.hidden).map(b => b.dataset.tab)")
    for tab in tabs:
        await page.js(f"document.querySelector('#tabs [data-tab=\"{tab}\"]').click()")
        await page.until(f"document.querySelector('#tab-{tab}').classList.contains('on')", f"вкладки {tab}")
        await asyncio.sleep(0.5)   # отрисовка по состоянию и запросы вкладки
    return tabs


async def smoke(cdp, base, switch):
    async with aiohttp.ClientSession() as http:
        async with http.get(cdp + "/json/list") as r:
            target = next(t for t in await r.json() if t["type"] == "page")
        async with http.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0) as ws:
            page = Page(ws)
            for domain in ("Runtime", "Log", "Page"):
                await page.call(f"{domain}.enable")
            seen = {}
            for engine in ("cosyvoice", "espeech"):
                await switch(engine)
                seen[engine] = await visit_tabs(page, base)
            missing = await page.js("[...I18N_MISSING]")
            lang = await page.js("document.documentElement.lang")
            # подсказка у поля с translate="no" не переводилась, хотя перевод в en.json был
            hint = await page.js("document.querySelector('#ed-save [name=name]').placeholder")
            # док-панель OBS (/dock): своя страница, свой скрипт; строки без перевода копятся в том же I18N_MISSING
            await page.call("Page.navigate", url=base + "/dock")
            await page.until("document.readyState === 'complete' && document.querySelector('#enabled').textContent",
                             "отрисовки док-панели")
            missing += await page.js("[...I18N_MISSING]")
            page.reader.cancel()
            return seen, missing, lang, hint, page.errors


@pytest.fixture(scope="module")
def browser(tmp_path_factory):
    exe = find_browser()
    if not exe:
        pytest.skip("нет Edge или Chrome")
    profile = str(tmp_path_factory.mktemp("browser"))
    # Под root (контейнер) Chromium без --no-sandbox не стартует, на раннере Ubuntu 24.04 песочнице мешает AppArmor;
    # страница — только локальная админка
    sandbox = ["--no-sandbox"] if os.environ.get("CI") or getattr(os, "geteuid", lambda: 1)() == 0 else []
    proc = subprocess.Popen([exe, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={profile}",
                             "--no-first-run", "--no-default-browser-check", "--disable-extensions",
                             "--disable-gpu", "--mute-audio", *sandbox, "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = os.path.join(profile, "DevToolsActivePort")

    def port():
        # На Windows браузер держит файл открытым, пока пишет: чтение в этот момент — PermissionError (падало в CI
        # на windows-latest). Порт — первая строка; перевод строки после него — знак, что она дописана.
        try:
            with open(port_file) as f:
                text = f.read()
        except OSError:
            return None
        return "\n" in text and text.split("\n", 1)[0].strip()

    yield f"http://127.0.0.1:{wait_for(port, 20, 'запуска браузера')}"
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_admin_tabs_english(browser, loop, tmp_path):
    cosy = loop.run(FakeCosy(frontend=True).start())
    esp = loop.run(FakeCosy(engine="espeech", train=True).start())
    server = StreamTTS(str(tmp_path), None, dict(FAST_ENV, STREAM_TTS_LANG="en",
                                                 STREAM_TTS_TEST_SERVERS=f"cosyvoice={cosy.url},espeech={esp.url}"))
    try:
        server.start()
        api = Api(server.base)

        async def switch(engine):
            def ready():
                c = api.state()["cosy"]
                return c["engine"] == engine and c["ok"] and not c["switching"]
            if not ready():
                await asyncio.to_thread(api.ok, "/api/engine", dict(engine=engine))
            await asyncio.to_thread(wait_for, ready, 15, f"смены модели на {engine}")

        seen, missing, lang, hint, errors = asyncio.run(smoke(browser, server.base, switch))
    finally:
        server.stop()
        loop.run(cosy.close())
        loop.run(esp.close())
    assert lang == "en"
    assert hint == "e.g. game-hero"
    for engine, tabs in seen.items():
        assert {"queue", "twitch", "voices", "settings", "stats"} <= set(tabs), (engine, tabs)
    assert "server" in seen["espeech"], seen   # настройки модели ESpeech — на вкладке «Сервер»
    assert not errors, "\n".join(errors)
    assert not missing, f"строки без перевода ({len(missing)}):\n" + "\n".join(missing)
