"""Встроенный сервер синтеза: stream-tts сам запускает сервер движка и следит за ним.

Здесь — общее для всех движков: процесс, порт, перезапуски, вывод в лог, остановка на время смены файлов (paused),
выгрузка модели. Что запускать и с какими аргументами, решает запуск движка (engines/<id>/launch.py, Launcher):
args(host, port) собирает командную строку или бросает Unavailable (нет модели и т. п.), env() — окружение
процесса, public() — его поля для админки.

Порт: Launcher.port (переменная движка, у CosyVoice — COSYVOICE_PORT) или любой свободный; адрес, на котором сервер
слушает, — Launcher.host (по умолчанию 127.0.0.1; образ Docker задаёт 0.0.0.0, чтобы порт сервера был виден снаружи
контейнера), а сам stream-tts всегда ходит к нему по 127.0.0.1. Упавший сервер перезапускается (пауза растёт до
минуты, если он падает сразу после старта). Сервер завершается вместе с stream-tts: на Windows через Job Object
(даже если stream-tts убит), на Linux через PR_SET_PDEATHSIG. Выгрузка модели синтеза (вкладка «Статистика») —
остановка сервера: так освобождается и контекст видеокарты (и память torch, которую он сам не отдаёт); загрузка —
новый запуск. Вывод сервера — в лог под именем движка (логгер cosyvoice: по elapsed_ms в нём меряют скорость)."""
import asyncio, collections, contextlib, logging, os, socket, subprocess, sys

import aiohttp

from . import paths
from .i18n import tr

log = logging.getLogger("runtime")


class Unavailable(Exception):
    """Сервер пока нельзя запустить (нет модели, нет AVX2): причина показывается в админке."""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Job:
    """Job Object Windows с KILL_ON_JOB_CLOSE: процессы в нём завершаются, когда закрывается stream-tts."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ct, k = ctypes, ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.CloseHandle.argtypes = [w.HANDLE]

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.k, self.job = k, k.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.job or not k.SetInformationJobObject(self.job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "Job Object")

    def add(self, pid):
        h = self.k.OpenProcess(0x0101, False, pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
        try:
            if not h or not self.k.AssignProcessToJobObject(self.job, h):
                raise OSError(self.ct.get_last_error(), "AssignProcessToJobObject")
        finally:
            if h:
                self.k.CloseHandle(h)


def _die_with_parent():
    """Linux: сервер получает SIGTERM, когда завершается stream-tts (preexec_fn)."""
    import ctypes, signal
    ctypes.CDLL(None).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


def spawn_kwargs():
    """Для create_subprocess_exec: без окна консоли на Windows, смерть вместе с stream-tts на Linux."""
    if os.name == "nt":
        return dict(creationflags=subprocess.CREATE_NO_WINDOW)
    return dict(preexec_fn=_die_with_parent) if sys.platform.startswith("linux") else {}


class Managed:
    def __init__(self, launcher):
        self.launcher = launcher
        self.port = launcher.port or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.state = "stopped"     # waiting (нет модели и т. п.) | starting | running | crashed | stopped
        self.error = ""
        self.restarts = 0
        self.starts = 0            # сколько раз сервер становился готов: stream-tts перечитывает голоса
        self.tail = collections.deque(maxlen=40)  # последние строки вывода сервера: причина падения
        self.proc = None
        self.task = None
        self._restarting = False
        self._hold = 0             # paused(): сервер остановлен, пока меняются файлы голосов
        self.unloaded = False      # модель выгружена кнопкой: сервер не запускается до load()
        self._wake = None          # asyncio.Event: перезапуск без ожидания
        self._job = None
        if os.name == "nt":
            try:
                self._job = _Job()
            except OSError as e:
                log.warning("Job Object не создан, сервер может пережить stream-tts: %s", e)

    @property
    def server(self):
        return self.launcher.server

    def public(self):
        return dict(state=self.state, error=self.error, restarts=self.restarts, server=self.server,
                    unloaded=self.unloaded, pid=self.proc.pid if self.proc and self.proc.returncode is None else None,
                    **self.launcher.public())

    # --- процесс ---
    async def start(self):
        self._wake = asyncio.Event()
        self.task = asyncio.create_task(self._supervise())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self._kill()
        self.state = "stopped"

    async def restart(self):
        """Перезапуск с новыми моделью, устройством и голосами."""
        self._restarting = True
        await self._kill()
        self._wake.set()

    async def use(self, launcher):
        """Другой движок (смена модели): тот же порт, перезапуск с его аргументами."""
        self.launcher = launcher
        self.restarts, self.unloaded, self.error = 0, False, ""
        await self.restart()

    async def unload(self):
        """Модель выгружена: сервер остановлен, пока не вызван load()."""
        self.unloaded = True
        await self.restart()

    def load(self):
        self.unloaded = False
        self._wake.set()

    @contextlib.asynccontextmanager
    async def paused(self):
        """Сервер остановлен на время блока: на Windows он держит открытыми файлы голосов."""
        self._hold += 1
        self._restarting = True
        try:
            await self._kill()
            yield
        finally:
            self._hold -= 1
            self._wake.set()

    async def _sleep(self, seconds):
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), seconds)
        self._wake.clear()

    async def _kill(self):
        p = self.proc
        if not p or p.returncode is not None:
            return
        p.terminate()  # Windows: TerminateProcess; сохранять серверу нечего
        try:
            await asyncio.wait_for(p.wait(), 10)
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()

    async def _pump(self, stream):
        srv_log = logging.getLogger(self.launcher.ID)
        while line := await stream.readline():
            text = line.decode(errors="replace").rstrip()
            if text:
                self.tail.append(text)
                srv_log.info("%s", text)

    async def _ready(self, session):
        """Ждёт ответа /status, пока процесс жив."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 300
        while self.proc.returncode is None and loop.time() < deadline:
            try:
                async with session.get(f"{self.url}/status", timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status == 200:
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(1)
        else:
            return False
        self.state = "running"
        self.starts += 1
        return True

    async def _run(self, args):
        name = self.launcher.NAME
        os.makedirs(paths.VOICES, exist_ok=True)
        log.info("запуск: %s%s", subprocess.list2cmdline(self.launcher.shown(args)), self.launcher.started())
        self.tail.clear()
        self.state, self.error = "starting", ""
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=paths.VOICES, env=self.launcher.env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **spawn_kwargs())
        if self._job:
            try:
                self._job.add(self.proc.pid)
            except OSError as e:
                log.warning("сервер не добавлен в Job Object: %s", e)
        if self._hold or self.unloaded:  # paused() или unload() начались, пока процесс запускался
            await self._kill()
        pump = asyncio.create_task(self._pump(self.proc.stdout))
        try:
            async with aiohttp.ClientSession() as session:
                if await self._ready(session):
                    log.info("%s готов: %s, %s", name, self.url, self.launcher.ready())
                elif self.proc.returncode is None:
                    log.error("%s не ответил за 5 минут, перезапускаю", name)
                    await self._kill()
            await self.proc.wait()
        finally:
            await self._kill()
            await asyncio.gather(pump, return_exceptions=True)

    async def _supervise(self):
        loop, delay = asyncio.get_running_loop(), 2
        while True:
            name = self.launcher.NAME
            if self._hold:
                self.state, self.error = "stopped", tr("сервер остановлен: меняются голоса")
                await self._sleep(1)
                continue
            if self.unloaded:
                self.state, self.error = "stopped", tr("модель выгружена: загрузите её на вкладке «Статистика»")
                await self._sleep(5)
                continue
            try:
                args = await self.launcher.args(self.launcher.host, self.port)
            except Unavailable as e:
                if self.error != str(e):
                    log.warning("%s не запущен: %s", name, e)
                self.state, self.error = "waiting", str(e)
                await self._sleep(5)
                continue
            if self._hold or self.unloaded:   # paused() или unload() начались, пока собирались аргументы
                continue
            self._restarting = False
            started = loop.time()
            await self._run(args)
            if self._restarting:
                self._restarting = False
                continue
            code = self.proc.returncode
            code = code - 2 ** 32 if code >= 2 ** 31 else code  # Windows отдаёт код без знака
            last = next((t for t in reversed(self.tail) if not self.launcher.noise(t)), "")
            self.state, self.error = "crashed", tr("{server} завершился (код {code})", server=name, code=code) + \
                (": " + last if last else "")
            self.restarts += 1
            delay = 2 if loop.time() - started > 60 else min(delay * 2, 60)
            log.warning("%s; перезапуск через %d с", self.error, delay)
            await self._sleep(delay)
