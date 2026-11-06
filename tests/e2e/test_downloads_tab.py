"""Вкладка «Загрузки» (install.py, web/server.py, static/admin-downloads.js) без сети и без настоящих моделей.

- Очередь Installer: загрузки — заданиями-заглушками вместо скачивания (подменён Installer._job): вторая загрузка
  встаёт в очередь, «Убрать» убирает из неё, отмена и ошибка идущей запускают следующую, оборванная докачивается
  «Продолжить»; ESpeech, которому нужна CUDA, ставит её в очередь перед собой.
- Выбор модели: до первой загрузки модель не выбрана (settings.engine пуст, Tts.picked — ""), первая скачанная
  становится выбранной (у другого движка — сменой модели, pick), следующие загрузки выбор не меняют; прежние
  установки с уже скачанной моделью получают её в engine при запуске. Скачанный квант становится вариантом CosyVoice3,
  только если выбранного нет; удаление выбранного переводит на оставшийся, последнего — сервер ждёт модель.
- Процесс stream-tts с cosyvoice-server без модели и поддельным supertonic-server (файлы модели — с локального
  HTTP-сервера, как в test_supertonic_download.py), админка на английском в headless-браузере (как
  test_admin_smoke.py): первый запуск открывается на «Загрузках» с полосой «скачайте модель», «Сервер» не даёт выбрать
  нескачанную модель, «Скачать» с подтверждением лицензии ставит загрузку, после неё Supertonic 3 выбран и запущен;
  удаление — сервер снова ждёт модель. Ошибки JS и строки без перевода роняют тест."""
import asyncio, hashlib, json, os, sys

import pytest

from conftest import FAST_ENV
from fake_cosyvoice import fake_python
from harness import STREAM_TTS, Api, StreamTTS, wait_for
from test_admin_smoke import Page, browser  # noqa: F401 — фикстура браузера
from test_supertonic_download import Files, model_files

sys.path.insert(0, STREAM_TTS)
from streamtts import engines, install, paths  # noqa: E402
from streamtts.engines.cosyvoice import model_name  # noqa: E402


# --- очередь и удаление: Installer в этом процессе ---
class Settings(dict):
    def update(self, **kw):   # как store.Settings
        super().update(kw)


class Runtime:
    """То, что Installer берёт у runtime.Managed: запущенный движок, перезапуск и остановка на время блока."""

    def __init__(self, launcher):
        self.launcher, self.restarts, self.pauses = launcher, 0, 0

    async def restart(self):
        self.restarts += 1

    def paused(self):
        rt = self

        class Paused:
            async def __aenter__(self):
                rt.pauses += 1

            async def __aexit__(self, *exc):
                return False
        return Paused()


class Launcher:
    def __init__(self, d):
        self.dir, self.model, self.downloaded = str(d), "", False


class Jobs:
    """Задания-заглушки вместо загрузок: каждое ждёт, пока тест его не отпустит (ok) или уронит (fail)."""

    def __init__(self, inst):
        self.inst, self.gates, self.started = inst, {}, []
        inst._job = self.job

    async def job(self, did, session):
        self.started.append(did)
        gate = self.gates[did] = asyncio.get_running_loop().create_future()
        self.inst.dl.update(total=100, done=10)
        if await gate:
            raise RuntimeError(await gate)
        if did in install.QUANTS:
            open(os.path.join(paths.MODELS, model_name(did)), "wb").close()

    async def finish(self, did, error=None):
        await until(lambda: did in self.gates and not self.gates[did].done())
        self.gates[did].set_result(error)
        await until(lambda: self.inst.dl["id"] != did or self.inst.dl["state"] != "running")


async def until(cond, timeout=5):
    for _ in range(int(timeout / 0.01)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("не дождались")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "MODELS", str(tmp_path / "models"))
    os.makedirs(paths.MODELS)

    def make(engine="", runtime_engine="cosyvoice", picks=None):
        cosy, st = Launcher(tmp_path / "rt"), Launcher(tmp_path / "st")
        launchers = dict(cosyvoice=cosy, supertonic=st)
        rt = Runtime(launchers[runtime_engine])
        def pick(e):   # смена модели: запускается сервер другого движка
            picks.append(e)
            rt.launcher = launchers[e]
        inst = install.Installer(Settings(engine=engine, model=""), rt, lambda: None, launchers,
                                 pick if picks is not None else None)
        inst.cuda_state = lambda: dict(offer=False, installed=False, version="", size=0, unpacked=0, dir=cosy.dir)
        return inst, Jobs(inst), rt
    return make


def test_queue_cancel_error_resume(setup):
    """Две загрузки подряд — вторая в очереди, повтор — отказ, «Убрать» — из очереди; отмена идущей запускает
    следующую, ошибка — тоже; отменённая и оборванная — в failed и «Продолжить» качает их снова."""
    async def go():
        inst, jobs, rt = setup()
        inst.enqueue("Q8_0")
        inst.enqueue("Q6_K")
        inst.enqueue("Q5_K_M")
        assert inst.current() == "Q8_0" and inst.queue == ["Q6_K", "Q5_K_M"]
        assert inst.public()["queue"] == ["Q6_K", "Q5_K_M"] and inst.public()["download"]["id"] == "Q8_0"
        for did in ("Q8_0", "Q6_K"):
            with pytest.raises(ValueError, match="уже в очереди"):
                inst.enqueue(did)
        inst.unqueue("Q5_K_M")
        assert inst.queue == ["Q6_K"]
        with pytest.raises(ValueError):
            inst.unqueue("Q5_K_M")

        await until(lambda: "Q8_0" in jobs.gates)
        inst.cancel()   # отмена идущей — следующая сама
        await until(lambda: inst.current() == "Q6_K")
        assert inst.failed["Q8_0"]["state"] == "cancelled" and not inst.queue
        inst.enqueue("frontend")
        await jobs.finish("Q6_K", "обрыв")
        await until(lambda: inst.current() == "frontend")
        assert inst.failed["Q6_K"] == dict(inst.failed["Q6_K"], state="error", error="обрыв")
        await jobs.finish("frontend")
        assert inst.dl["state"] == "done" and not inst.current()

        inst.enqueue("Q8_0")   # «Продолжить»: failed уходит, пока идёт
        assert "Q8_0" not in inst.failed and inst.current() == "Q8_0"
        await jobs.finish("Q8_0")
        assert jobs.started == ["Q8_0", "Q6_K", "frontend", "Q8_0"]
        with pytest.raises(ValueError, match="Q8_0 уже скачан"):
            inst.enqueue("Q8_0")
        with pytest.raises(ValueError, match="нет такой загрузки"):
            inst.enqueue("nope")
        inst.close()
    asyncio.run(go())


def test_espeech_queues_cuda_first(setup):
    """ESpeech, которому нужна CUDA, ставит её в очередь перед собой, а не качает внутри своей загрузки."""
    async def go():
        inst, jobs, _ = setup()
        inst.espeech_state = lambda: dict(offer=True, installed=False, cuda=True, size=1, disk=1)
        inst._check = lambda did: None
        inst.enqueue("Q8_0")
        inst.enqueue("espeech")
        assert inst.queue == ["cuda", "espeech"]
        await jobs.finish("Q8_0")
        await until(lambda: inst.current() == "cuda")
        assert inst.queue == ["espeech"]
        inst.close()

        inst, jobs, _ = setup()
        inst.espeech_state = lambda: dict(offer=True, installed=False, cuda=True, size=1, disk=1)
        inst._check = lambda did: None
        inst.enqueue("espeech")   # ничего не идёт: CUDA сразу, ESpeech за ней
        assert inst.current() == "cuda" and inst.queue == ["espeech"]
        inst.close()
    asyncio.run(go())


def test_first_model_picked_and_quants(setup):
    """Первый скачанный квант — вариант CosyVoice3 и выбранная модель (сервер ждал — перезапуск); следующий квант выбор
    не меняет и сервер не трогает. Удаление выбранного — на оставшийся, при остановленном сервере; последнего —
    вариант пуст, сервер ждёт модель."""
    async def go():
        inst, jobs, rt = setup()
        inst.enqueue("Q6_K")
        await jobs.finish("Q6_K")
        await until(lambda: rt.restarts == 1)
        assert inst.s == dict(engine="cosyvoice", model=model_name("Q6_K"))
        inst.enqueue("Q8_0")
        await jobs.finish("Q8_0")
        await asyncio.sleep(0.05)
        assert inst.s["model"] == model_name("Q6_K") and rt.restarts == 1, "следующая загрузка сменила выбор"
        assert inst.public()["model"] == model_name("Q6_K")

        with pytest.raises(ValueError, match="нет такой модели"):
            await inst.delete("Q5_K_M")
        await inst.delete("Q6_K")
        assert inst.s["model"] == model_name("Q8_0") and rt.pauses == 1
        assert install.installed_models() == [model_name("Q8_0")]
        await inst.delete("Q8_0")
        assert inst.s["model"] == "" and rt.pauses == 2 and not install.installed_models()
        assert inst.s["engine"] == "cosyvoice", "выбор модели после удаления остаётся: сервер ждёт её"
        inst.close()
    asyncio.run(go())


def test_first_model_other_engine(setup):
    """Первая скачанная модель другого движка — выбрана сменой модели (pick), без перезапуска текущего сервера;
    вторая модель выбор не меняет."""
    async def go():
        picks = []
        inst, jobs, rt = setup(picks=picks)
        inst.supertonic_state = lambda: dict(offer=True, installed=False)
        inst.enqueue("supertonic")
        await jobs.finish("supertonic")
        await until(lambda: picks == ["supertonic"])
        assert inst.s["engine"] == "supertonic" and rt.restarts == 0
        inst.enqueue("Q8_0")
        await jobs.finish("Q8_0")
        await asyncio.sleep(0.05)
        assert inst.s["engine"] == "supertonic" and picks == ["supertonic"]
        assert inst.s["model"] == model_name("Q8_0") and rt.restarts == 0   # сервер CosyVoice не запущен
        inst.close()
    asyncio.run(go())


class Engine:
    def __init__(self, ready):
        self.ready, self.port = ready, 0

    def has_model(self):
        return self.ready


def test_tts_picked_until_first_model():
    """Tts: без скачанных моделей — первая найденная, но не выбрана (engine пуст); с моделью у второй — она и
    записывается в engine (прежние установки); нескачанная в engines() — ready=False, «не скачана»."""
    s = Settings(engine="")
    t = engines.Tts(s, {}, dict(cosyvoice=Engine(False), supertonic=Engine(False)))
    assert (t.id, t.picked, s["engine"]) == ("cosyvoice", "", "")
    e = {x["id"]: x for x in t.engines()}
    assert (e["cosyvoice"]["ready"], e["cosyvoice"]["why"]) == (False, "не скачана")
    assert (e["espeech"]["available"], e["espeech"]["ready"]) == (False, False)

    t = engines.Tts(s, {}, dict(cosyvoice=Engine(False), supertonic=Engine(True)))
    assert (t.id, t.picked, s["engine"]) == ("supertonic", "supertonic", "supertonic")
    s = Settings(engine="cosyvoice")   # выбранная модель удалена: выбор остаётся, сервер ждёт её
    t = engines.Tts(s, {}, dict(cosyvoice=Engine(False), supertonic=Engine(True)))
    assert (t.id, t.picked) == ("cosyvoice", "cosyvoice") and not t.ready("cosyvoice")
    t = engines.Tts(Settings(engine=""), {"cosyvoice": "http://127.0.0.1:1"}, {})   # готовые серверы тестов
    assert t.picked == "cosyvoice" and t.ready("cosyvoice")


# --- процесс stream-tts и вкладка в браузере ---
@pytest.fixture
def first_run(loop, tmp_path):
    """stream-tts на английском: cosyvoice-server без модели (не запускается — нет модели) и поддельный
    supertonic-server без файлов модели; модель ещё не выбрана."""
    files = model_files()
    srv = loop.run(Files(files).start())
    pin = tmp_path / "pin.json"
    pin.write_text(json.dumps(dict(url=srv.url + "/", license=srv.url + "/LICENSE", files=[
        dict(path=p, size=len(d), sha256=hashlib.sha256(d).hexdigest()) for p, d in files.items()])), encoding="utf-8")
    python = fake_python(str(tmp_path / "fake"), "supertonic", "supertonic")
    (tmp_path / "runtime").mkdir()
    (tmp_path / "runtime" / ("cosyvoice-server" + paths.EXE)).write_bytes(b"")
    server = StreamTTS(str(tmp_path), None, dict(FAST_ENV, STREAM_TTS_LANG="en", SUPERTONIC_SERVER=python,
                                                STREAM_TTS_TEST_SUPERTONIC=str(pin))).start(synth=False)
    yield server, Api(server.base)
    server.stop()
    loop.run(srv.close())


JS_CARDS = "[...document.querySelectorAll('#dl-models .card')].map(c => c.querySelector('h2').textContent)"
JS_ST = "[...document.querySelectorAll('#dl-models .card')].find(c => c.querySelector('h2').textContent === 'Supertonic 3')"


async def browse(cdp, base, api):
    import aiohttp
    async with aiohttp.ClientSession() as http:
        async with http.get(cdp + "/json/list") as r:
            target = next(t for t in await r.json() if t["type"] == "page")
        async with http.ws_connect(target["webSocketDebuggerUrl"], max_msg_size=0) as ws:
            page = Page(ws)
            for domain in ("Runtime", "Log", "Page"):
                await page.call(f"{domain}.enable")
            await page.call("Page.navigate", url=base + "/")
            await page.until("typeof S !== 'undefined' && S !== null && document.readyState === 'complete'",
                             "загрузки админки")
            # первый запуск: «Загрузки», полоса «скачайте модель», точка на ярлыке, карточки найденных моделей
            await page.until("document.querySelector('#tab-downloads').classList.contains('on')", "вкладки «Загрузки»")
            assert await page.js("location.hash") == "#downloads"
            assert await page.js("!document.querySelector('#dl-strip').hidden && "
                                 "document.querySelector('#dl-strip').classList.contains('first')")
            assert await page.js("!!document.querySelector('#dl-badge .dot.warn')")
            assert await page.js(JS_CARDS) == ["CosyVoice3", "Supertonic 3"]
            assert await page.js("document.querySelectorAll('#dl-models .card.picked').length") == 0
            assert await page.js("document.querySelectorAll('#dl-models li').length") == 4   # 3 кванта и Supertonic 3
            assert await page.js("[...document.querySelectorAll('#packages li b')].length") == 2
            # «Сервер»: нескачанные модели выбрать нельзя, строка со ссылкой на «Загрузки»
            await page.js("document.querySelector('#tabs [data-tab=\"server\"]').click()")
            await page.until("document.querySelector('#tab-server').classList.contains('on')", "вкладки «Сервер»")
            assert await page.js("[...document.querySelectorAll('#engine-form input')].every(i => i.disabled && !i.checked)")
            assert await page.js("!document.querySelector('#engine-hint').hidden && "
                                 "!!document.querySelector('#engine-hint a[href=\"#downloads\"]')")
            # «Скачать» у Supertonic 3 — с подтверждением лицензии
            await page.js("document.querySelector('#tabs [data-tab=\"downloads\"]').click()")
            await page.js(f"{JS_ST}.querySelector('button.primary').click()")
            await page.until("document.querySelector('#ask').open", "подтверждения лицензии")
            await page.js("document.querySelector('#ask-yes').click()")
            await asyncio.to_thread(wait_for, lambda: api.state()["cosy"]["picked"] == "supertonic", 30,
                                    "выбора первой скачанной модели")
            await page.until(f"{JS_ST} && {JS_ST}.classList.contains('picked')", "выбранной карточки", 30)
            await page.until("document.querySelector('#dl-strip').hidden", "полосы после загрузки")
            await page.until("!document.querySelector('#dl-badge .dot')", "ярлыка без точки", 30)
            assert await page.js("document.querySelector('#dl-sum dl.sum') !== null")
            missing = await page.js("[...I18N_MISSING]")
            page.reader.cancel()
            return missing, page.errors


def test_first_run_tab(first_run, browser):  # noqa: F811
    server, api = first_run
    c = api.state()["cosy"]
    assert c["picked"] == "" and c["engine"] == "cosyvoice"
    e = {x["id"]: x for x in c["engines"]}
    assert (e["cosyvoice"]["available"], e["cosyvoice"]["ready"], e["supertonic"]["ready"]) == (True, False, False)
    r = api.post("/api/engine", dict(engine="supertonic")).json()
    assert not r["ok"] and "Downloads" in r["error"], r
    st = api.state()["setup"]
    assert st["download"]["state"] == "idle" and st["queue"] == [] and st["failed"] == {}
    assert st["disk"]["free"] > 0 and st["supertonic"]["offer"] and not st["espeech"]["offer"]
    assert "cosyvoice-Q8_0" in st["samples"]   # примеры звучания — только те, что есть: без них кнопки нет

    missing, errors = asyncio.run(browse(browser, server.base, api))
    assert not errors, "\n".join(errors)
    assert not missing, f"строки без перевода ({len(missing)}):\n" + "\n".join(missing)

    c = wait_for(lambda: (lambda c: c["ok"] and c["runtime"]["state"] == "running" and c)(api.state()["cosy"]), 30,
                 f"запуска Supertonic 3 после загрузки\n{server.tail()}")
    assert c["engine"] == c["picked"] == "supertonic"
    assert json.load(open(os.path.join(server.data, "settings.json"), encoding="utf-8"))["engine"] == "supertonic"
    # удаление выбранной модели: выбор остаётся, сервер ждёт её
    api.ok("/api/download/delete", dict(id="supertonic"))
    c = wait_for(lambda: (lambda c: c["runtime"]["state"] == "waiting" and c)(api.state()["cosy"]), 30,
                 "ожидания модели после удаления")
    assert c["picked"] == "supertonic" and not {x["id"]: x for x in c["engines"]}["supertonic"]["ready"]
