"""Движки синтеза: у каждого свой пакет engines/<id>/ — описание (ID, TITLE, VOICE_EXT, DEVICES, загрузки),
запуск встроенного сервера (Launcher, find_server) и клиент (Client, общий протокол — client.py). Модули лежат внутри
пакета streamtts: собранная программа (Nuitka) импортов извне не видит.

Модель одна единовременно (settings.engine): Tts — сервер синтеза выбранной модели для ядра stream-tts. Смена модели —
другой клиент и перезапуск встроенного сервера с запуском другого движка на том же порту (в тестах с готовыми
серверами, config.TEST_SERVERS, — сервер того движка). Очередь к серверу (lane) общая: смена ждёт текущий запрос и
держит очередь, пока новый сервер не ответит, — озвучка ждёт, а не падает на перезапуске."""
import asyncio, logging

from . import cosyvoice, espeech
from .client import LIVE, Lane
from .. import runtime
from ..i18n import tr

log = logging.getLogger("tts")

ENGINES = {cosyvoice.ID: cosyvoice, espeech.ID: espeech}
READY_TIMEOUT = 300   # загрузка модели на CPU бывает долгой


class Tts:
    """Клиент выбранного движка: атрибуты, которых здесь нет (synth, stream, stop, speakers, schema, accent…), — его."""

    def __init__(self, settings, servers, launchers):
        """launchers — запуск серверов найденных движков; servers — готовые серверы {движок: адрес}, только в тестах."""
        self.s, self.servers, self.launchers = settings, servers or {}, launchers or {}
        self.lane = Lane()
        have = self.launchers or self.servers
        self.available = [i for i in ENGINES if i in have]
        self.id = settings.get("engine") if settings.get("engine") in self.available else self.available[0]
        self.runtime = runtime.Managed(self.launchers[self.id]) if self.launchers else None
        self.client = self._client(self.id)
        self.switches = 0          # сколько раз сменилась модель: tts_monitor перечитывает голоса
        self.switching = ""        # на какую модель идёт смена

    def _client(self, engine):
        url = self.runtime.url if self.runtime else self.servers[engine]
        return ENGINES[engine].Client(url, self.s, lane=self.lane)

    def __getattr__(self, name):
        return getattr(self.__dict__["client"], name)

    # поля, которые пишут снаружи (web.py, __main__.py), — у клиента
    ok = property(lambda self: self.client.ok, lambda self, v: setattr(self.client, "ok", v))
    error = property(lambda self: self.client.error, lambda self, v: setattr(self.client, "error", v))

    @property
    def title(self):
        return ENGINES[self.id].TITLE

    @property
    def voice_ext(self):
        return ENGINES[self.id].VOICE_EXT

    def engines(self):
        """Модели для выбора в админке."""
        out = []
        for i, E in ENGINES.items():
            why = "" if i in self.available else tr("нет {name}", name=E.Launcher.NAME)
            out.append(dict(id=i, title=E.TITLE, available=i in self.available, why=why))
        return out

    async def start(self):
        await self.client.start()

    async def close(self):
        await self.client.close()

    async def status(self):
        """GET /status. У движка может уточниться то, что до запуска было догадкой: у ESpeech auto решает torch."""
        ok = await self.client.status()
        sync = getattr(self.runtime.launcher, "sync", None) if self.runtime else None
        if sync:
            sync(self.client.info)
        return ok

    async def use(self, engine, ready=None):
        """Сменить модель. Вызывать задачей: ждёт текущий запрос к серверу и готовности нового сервера; ready() —
        когда он ответил (голоса новой модели), до конца смены."""
        if engine not in self.available:
            raise ValueError(tr("модель {engine} недоступна", engine=engine))
        if engine == self.id or self.switching:
            return
        self.switching = engine
        try:
            self.s.update(engine=engine)
            async with self.lane.hold(LIVE, tr("смена модели")):
                old, new = self.client, self._client(engine)
                await new.start()
                self.client, self.id = new, engine
                self.switches += 1
                await old.close()
                log.info("модель синтеза: %s", ENGINES[engine].TITLE)
                if self.runtime:
                    await self.runtime.use(self.launchers[engine])
                if await self._wait_ready() and ready:
                    await ready()
        finally:
            self.switching = ""

    async def _wait_ready(self):
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        limit = READY_TIMEOUT if self.runtime else 10
        while loop.time() - t0 < limit:
            if await self.client.status():
                return True
            # сервер не запустить (нет модели) или он упал — ждать нечего, причина видна в админке
            if self.runtime and loop.time() - t0 > 5 and self.runtime.state in ("waiting", "crashed"):
                self.client.error = self.runtime.error   # причина, а не «соединение отклонено»
                return False
            await asyncio.sleep(1)
        return False
