"""Фикстуры e2e-тестов stream-tts.

- tts — общий на сессию процесс stream-tts с поддельным cosyvoice, приведённый перед тестом в исходное состояние
  (настройки по умолчанию, очередь пуста, примеры голосов готовы). После теста проверяется, что к cosyvoice ни разу
  не шли два синтеза одновременно.
- spawn — отдельный процесс со своими данными и своим поддельным cosyvoice: для тестов, которым нужно другое
  окружение (пароль, адрес) или перезапуск."""
import os, sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from fake_cosyvoice import FakeCosy  # noqa: E402
from harness import Api, Loop, Overlay, StreamTTS, wait_for  # noqa: E402

# Настройки, которые тесты меняют (умолчания — store.DEFAULTS)
DEFAULTS = dict(queue_limit=20, streaming=True, filter_links=True, filter_profanity=True, read_nick=False,
                subtitles=False, max_chars=500, chunk_tokens=150)


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
                    and s["demos"]["todo"] == 0 and not s["demos"]["current"])
        wait_for(done, timeout, "простоя stream-tts")

    def close(self):
        for ov in self.overlays:
            ov.close()
        self.overlays = []


@pytest.fixture(scope="session")
def shared(loop, tmp_path_factory):
    cosy = _fake(loop)
    server = StreamTTS(str(tmp_path_factory.mktemp("stream-tts")), cosy.url).start()
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
    ctx.idle()
    ctx.cosy.reset()   # запросы примеров голосов из прошлых тестов тесту не нужны
    yield ctx
    ctx.close()
    assert ctx.cosy.max_active <= 1, "к cosyvoice шли параллельные синтезы"


@pytest.fixture
def spawn(loop, tmp_path):
    """spawn(env=..., password=..., start=True) -> (StreamTTS, FakeCosy). Один и тот же каталог данных у
    повторного запуска: spawn(workdir=прошлый.dir)."""
    made = []

    def make(env=None, password=None, start=True, workdir=None):
        cosy = _fake(loop)
        wd = workdir or str(tmp_path / f"s{len(made)}")
        os.makedirs(wd, exist_ok=True)
        server = StreamTTS(wd, cosy.url, env)
        made.append((server, cosy))
        if start:
            server.start(password)
        return server, cosy

    yield make
    for server, cosy in made:
        server.stop()
        loop.run(cosy.close())
