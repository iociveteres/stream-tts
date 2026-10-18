"""Проверка скорости синтеза на этом компьютере (вкладка «Сервер», режим managed).

Текущее устройство — одна фраза целиком (скорость относительно реального времени) и она же стримингом (через
сколько секунд первый звук, с текущим chunk_tokens). «Все устройства» — то же по очереди на CUDA, Vulkan и CPU:
встроенный сервер перезапускается на каждом, в конце возвращается выбранное устройство. Перед замером короткая
фраза-разогрев: на Vulkan первая фраза после запуска готовит шейдеры до минуты.

Пока идёт проверка, доступ к cosyvoice занят (Lane, приоритет PREVIEW): озвучка ждёт в очереди, а не теряется.
Результаты — settings["bench"], последний замер на пару устройство + модель."""
import asyncio, contextlib, logging, os, time

from .config import SR
from .cosy import PREVIEW
from .i18n import tr

log = logging.getLogger("bench")

PHRASE = "Проверка скорости синтеза: эта фраза на русском звучит примерно шесть секунд."
WARMUP = "Разогрев."
READY_TIMEOUT = 300   # как у runtime._ready: загрузка модели на CPU бывает долгой


class Bench:
    def __init__(self, settings, cosy, runtime, voices, engine):
        self.s, self.cosy, self.runtime, self.voices, self.engine = settings, cosy, runtime, voices, engine
        self.state = dict(state="idle", step="", error="")
        self.task = None

    def public(self):
        return dict(self.state, results=self.s.get("bench", []))

    def devices(self):
        """Устройства для «Все устройства»: найденные видеокарты и процессор."""
        found = [n.lower() for n, _ in self.runtime.backends or []]
        return [k for k in ("cuda", "vulkan") if any(n.startswith(k) for n in found)] + ["cpu"]

    def start(self, every):
        if self.task and not self.task.done():
            raise ValueError(tr("проверка уже идёт"))
        if self.engine.busy():
            raise ValueError(tr("идёт озвучка: проверьте, когда очередь пуста"))
        if not self.cosy.ok:
            raise ValueError(tr("cosyvoice недоступен"))
        if every and self.runtime.public()["locked"]["backend"]:
            raise ValueError(tr("устройство задано переменной COSYVOICE_BACKEND"))
        if not self._voice():
            raise ValueError(tr("нет голосов"))
        self.task = asyncio.create_task(self._run(self.devices() if every else [None]))

    def _voice(self):
        enabled = sorted(self.voices.enabled())
        return next((v for v in ("irina", "wc3-footman") if v in enabled), enabled[0] if enabled else None)

    def _step(self, text):
        self.state.update(state="running", step=text, error="")
        self.engine.dirty.set()

    async def _run(self, devices):
        switched = False
        # Прежнее устройство возвращается, пока доступ ещё занят: иначе озвучка попала бы на перезапуск
        async with self.cosy.lane.hold(PREVIEW, tr("проверка скорости")):
            try:
                for dev in devices:
                    if dev:
                        self._step(tr("{dev}: перезапуск сервера", dev=dev.upper()))
                        self.runtime.override = dev   # settings не трогаются: результат сохраняется в них
                        switched = True
                        await self._restart()
                    await self._measure()
                self.state.update(state="done", step="")
            except Exception as e:
                log.warning("проверка скорости: %s", e)
                self.state.update(state="error", step="", error=str(e) or type(e).__name__)
            finally:
                if switched:
                    self.runtime.override = ""
                    self.state["step"] = tr("возврат прежнего устройства")
                    self.engine.dirty.set()
                    with contextlib.suppress(Exception):
                        await self._restart()
                    self.state["step"] = ""
                self.engine.dirty.set()

    async def _restart(self):
        starts = self.runtime.starts
        await self.runtime.restart()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READY_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(1)
            if self.runtime.starts > starts and self.runtime.state == "running" and await self.cosy.status():
                return
            if self.runtime.state == "crashed":
                raise RuntimeError(self.runtime.error)
        raise RuntimeError(tr("сервер не запустился за 5 минут"))

    async def _measure(self):
        voice, name = self._voice(), self.runtime.backend
        ratio, chunk = self.s["token_ratio"], int(self.s["chunk_tokens"])
        self._step(tr("{dev}: разогрев", dev=name))
        await self.cosy.synth(voice, WARMUP, ratio, seed=1)
        self._step(tr("{dev}: фраза целиком", dev=name))
        t0 = time.monotonic()
        x = await self.cosy.synth(voice, PHRASE, ratio, seed=1)
        whole, audio = time.monotonic() - t0, len(x) / SR
        self._step(tr("{dev}: стриминг", dev=name))
        first = None
        t0 = time.monotonic()
        # Нужен только первый кусок: дальше синтез останавливается, как при пропуске фразы в engine
        async with contextlib.aclosing(self.cosy.stream(voice, PHRASE, ratio, chunk)) as chunks:
            async for _ in chunks:
                first = time.monotonic() - t0
                await self.cosy.stop()
                break
        row = dict(backend=name, model=os.path.basename(self.runtime.model), rtf=round(whole / audio, 3),
                   first=round(first, 2) if first is not None else None, chunk=chunk, audio=round(audio, 1),
                   ts=time.time())
        log.info("скорость %s: %.2f× реального времени, первый звук %s с", name, row["rtf"], row["first"])
        results = [r for r in self.s.get("bench", []) if (r["backend"], r["model"]) != (row["backend"], row["model"])]
        self.s.update(bench=results + [row])
