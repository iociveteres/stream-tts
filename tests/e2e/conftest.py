"""Фикстуры e2e-тестов stream-tts.

- tts — общий на сессию процесс stream-tts с поддельным cosyvoice, приведённый перед тестом в исходное состояние
  (настройки по умолчанию, очередь пуста, примеры голосов готовы). После теста проверяется, что к cosyvoice ни разу
  не шли два синтеза одновременно.
- chat — то же в режиме «Только чтение чата»: stream-tts читает канал testchan поддельного IRC (fake_irc.py).
- spawn — отдельный процесс со своими данными и своим поддельным cosyvoice: для тестов, которым нужно другое
  окружение (пароль, адрес) или перезапуск.
- editor — процесс на модуль с поддельным cosyvoice, у которого есть ONNX-фронтенд (голоса из записи), черновик
  перед тестом удаляется.
- Маркер real — проверка с настоящей моделью (test_real.py): по умолчанию пропускается, запуск — pytest -m real.
  Замеры (report) выводятся в конце прогона."""
import os, sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fake_cosyvoice import FakeCosy  # noqa: E402
from fake_irc import FakeIrc  # noqa: E402
from harness import Api, Loop, Overlay, StreamTTS, wait_for  # noqa: E402

# Настройки, которые тесты меняют (умолчания — store.DEFAULTS)
DEFAULTS = dict(queue_limit=20, streaming=True, filter_links=True, filter_profanity=True, read_nick=False,
                max_chars=500, chunk_tokens=150)
SOURCES = dict(reward=dict(on=True, access="all", allow=[]),
               command=dict(on=False, name="!tts", access="subs", allow=[], user_cd=60, global_cd=0),
               chat=dict(on=False, access="all", allow=[]))
# Таймауты чата для тестов: «канал не найден» через 2 с, свой PING после 1 с тишины, ответ на него — 1 с
IRC_ENV = dict(STREAM_TTS_IRC_JOIN_WAIT="2", STREAM_TTS_IRC_IDLE="1", STREAM_TTS_IRC_PONG_WAIT="1")
# Проверка cosyvoice и список голосов — раз в секунду, а не раз в 5 и 60 с: новый голос на сервере тест ждёт секунды
FAST_ENV = dict(STREAM_TTS_COSY_CHECK="1", STREAM_TTS_VOICES_CHECK="1")
REPORT = []


def pytest_configure(config):
    config.addinivalue_line("markers", "real: с настоящей моделью и cosyvoice-server (pytest -m real)")


def pytest_collection_modifyitems(config, items):
    if "real" in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason="с настоящей моделью: pytest tests/e2e -m real")
    for item in items:
        if "real" in item.keywords:
            item.add_marker(skip)


def pytest_terminal_summary(terminalreporter):
    if REPORT:
        terminalreporter.section("замеры с настоящей моделью")
        for line in REPORT:
            terminalreporter.write_line(line)


@pytest.fixture
def report(request):
    """report(строка): замер в итог прогона (без жёстких порогов)."""
    return lambda line: REPORT.append(f"{request.node.name}: {line}")


@pytest.fixture(scope="session")
def loop():
    lp = Loop()
    yield lp
    lp.close()


def _fake(loop):
    return loop.run(FakeCosy().start())


class Ctx:
    def __init__(self, loop, server, cosy):
        self.loop, self.server, self.cosy = loop, server, cosy
        self.api = Api(server.base)
        self.overlays = []

    def overlay(self, ready=True):
        ov = Overlay(self.loop, self.server.base)
        self.overlays.append(ov)
        return ov.connect(ready)

    def event(self, rid, timeout=10):
        return self.server.event(rid, timeout)

    def idle(self, timeout=20):
        """Очередь пуста, примеры голосов досинтезированы, к cosyvoice никто не обращается."""
        def done():
            s = self.api.state()
            return (s["cosy"]["ok"] and not s["engine"]["queue"] and not s["cosy"]["busy"]
                    and s["demos"]["todo"] == 0 and not s["demos"]["current"]
                    and all(v["demo"] for v in s["voices"] if v["enabled"]))
        wait_for(done, timeout, "простоя stream-tts")

    def close(self):
        for ov in self.overlays:
            ov.close()
        self.overlays = []


@pytest.fixture(scope="session")
def irc(loop):
    fake = loop.run(FakeIrc().start())
    yield fake
    loop.run(fake.close())


@pytest.fixture(scope="session")
def shared(loop, irc, tmp_path_factory):
    cosy = _fake(loop)
    server = StreamTTS(str(tmp_path_factory.mktemp("stream-tts")), cosy.url,
                       dict(IRC_ENV, **FAST_ENV, STREAM_TTS_IRC_URL=irc.url)).start()
    ctx = Ctx(loop, server, cosy)
    ctx.idle(30)
    yield ctx
    server.stop()
    loop.run(cosy.close())


@pytest.fixture
def tts(shared):
    ctx, api = shared, shared.api
    ctx.cosy.reset()
    ctx.api.ok("/api/settings", DEFAULTS)
    api.ok("/api/enabled", dict(on=True))
    api.ok("/api/clear")
    state = api.state()
    for login in state["bans"]:
        api.ok("/api/ban", dict(login=login, on=False))
    api.ok("/api/voice", dict(names=[v["name"] for v in state["voices"]], enabled=True))
    for v in state["voices"]:
        if v["gain_db"]:
            api.ok("/api/voice", dict(name=v["name"], gain_db=0))
    for name, src in SOURCES.items():
        api.ok("/api/source", dict(src, source=name))
    ctx.idle()
    ctx.cosy.reset()   # запросы примеров голосов из прошлых тестов тесту не нужны
    yield ctx
    ctx.close()
    assert ctx.cosy.max_active <= 1, "к cosyvoice шли параллельные синтезы"


class Chat:
    """Чат канала testchan: сообщения зрителей и модерация от имени Twitch, синхронно из потока теста."""

    def __init__(self, ctx, irc):
        self.ctx, self.irc, self.run = ctx, irc, ctx.loop.run

    def say(self, login, text, badges=(), **kw):
        """Сообщение в чат; возвращает его ID (ID фразы в очереди — command-<ID> или chat-<ID>)."""
        return self.run(self.irc.say(login, text, badges, **kw))

    def clear_msg(self, msg_id, login):
        self.run(self.irc.clear_msg(msg_id, login))

    def clear_chat(self, login=None):
        self.run(self.irc.clear_chat(login))

    def reconnect(self):
        self.run(self.irc.reconnect())

    def sync(self):
        """Всё отправленное раньше stream-tts уже обработал."""
        self.run(self.irc.sync())

    def state(self):
        return self.ctx.api.state()["twitch"]["chat"]

    def connected(self, timeout=10):
        wait_for(lambda: self.state()["state"] == "ok" and any(ch == "testchan" for _, ch in self.irc.clients),
                 timeout, "подключение к чату testchan")


@pytest.fixture
def chat(tts, irc):
    irc.reset()
    tts.api.ok("/api/twitch/mode", dict(mode="chat", channel="testchan"))
    c = Chat(tts, irc)
    c.connected()
    yield c
    tts.api.ok("/api/twitch/mode", dict(mode="app"))


@pytest.fixture
def spawn(loop, tmp_path):
    """spawn(env=..., password=..., start=True, frontend=False) -> (StreamTTS, FakeCosy). Один и тот же каталог
    данных у повторного запуска: spawn(workdir=прошлый.dir); тот же поддельный сервер: spawn(cosy=прошлый)."""
    made = []

    def make(env=None, password=None, start=True, workdir=None, frontend=False, cosy=None):
        cosy = cosy or loop.run(FakeCosy(frontend).start())
        wd = workdir or str(tmp_path / f"s{len(made)}")
        os.makedirs(wd, exist_ok=True)
        server = StreamTTS(wd, cosy.url, dict(FAST_ENV, **(env or {})))
        made.append((server, cosy))
        if start:
            server.start(password)
        return server, cosy

    yield make
    for server, cosy in made:
        server.stop()
    for cosy in {id(c): c for _, c in made}.values():
        loop.run(cosy.close())


@pytest.fixture(scope="module")
def editor_shared(loop, tmp_path_factory):
    cosy = loop.run(FakeCosy(frontend=True).start())
    server = StreamTTS(str(tmp_path_factory.mktemp("editor")), cosy.url, FAST_ENV).start()
    ctx = Ctx(loop, server, cosy)
    ctx.idle(30)
    yield ctx
    server.stop()
    loop.run(cosy.close())


@pytest.fixture
def editor(editor_shared):
    """Процесс с фронтендом на сервере синтеза, черновик пуст, фронтенд выгружен."""
    ctx = editor_shared
    ctx.api.ok("/api/draft/discard")
    ctx.api.ok("/api/models/unload", dict(id="frontend"))
    ctx.api.ok("/api/enabled", dict(on=True))
    ctx.idle()
    ctx.cosy.reset()
    yield ctx
    ctx.close()
    assert ctx.cosy.max_active <= 1, "к cosyvoice шли параллельные запросы"
