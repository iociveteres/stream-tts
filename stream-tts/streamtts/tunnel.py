"""Страница голосов в интернете без хостинга и аккаунта: быстрый туннель Cloudflare (cloudflared, адрес
*.trycloudflare.com) к отдельному порту, где есть только страница голосов и примеры (web/public.py).

Кнопка в админке (POST /api/tunnel) включает и выключает туннель; включённый поднимается и при следующем запуске
(settings.tunnel). Адрес новый при каждом запуске cloudflared, поэтому зрители получают текущий командой чата
!ttsvoices (commands.py). Без аккаунта Cloudflare не обещает доступности; упавший cloudflared перезапускается через
RETRY с (адрес сменится).

Почему только Cloudflare (замер 2026-11, тестовая страница с файлом 1 МБ): localhost.run — ~24 КБ/с и внешний IP
компьютера в заголовке x-backend-addr каждого ответа (его увидит любой зритель); Pinggy без аккаунта рвёт туннель
через 60 мин; LocalTunnel показывает зрителю заглушку с паролем; ngrok без аккаунта не работает. У Cloudflare —
~780 КБ/с, адрес за ~7 с, X-Robots-Tag: none от самого Cloudflare.

cloudflared качается при первом включении (версия, размер и sha256 закреплены в downloads.json) в <данные>/cloudflared/;
есть в PATH — берётся оттуда. STREAM_TTS_CLOUDFLARED — свой файл (тесты: tests/e2e/fake_cloudflared.py). Адрес — из
метрик cloudflared: GET /ready (соединение с Cloudflare есть) и GET /quicktunnel ({"hostname": …})."""
import asyncio, collections, contextlib, logging, os, shutil, socket, sys, time

import aiohttp

from . import fetch, paths
from .downloads import DOWNLOADS
from .i18n import tr
from .runtime import _Job, spawn_kwargs

log = logging.getLogger("tunnel")

VERSION = DOWNLOADS["cloudflared"]["version"]
PIN = DOWNLOADS["cloudflared"]["windows" if os.name == "nt" else "linux"]
DIR = os.path.join(paths.DATA, "cloudflared")
START_WAIT = float(paths.env("STREAM_TTS_TUNNEL_WAIT", "60"))   # с: адрес обычно есть через 5–10 с
RETRY = float(paths.env("STREAM_TTS_TUNNEL_RETRY", "10"))        # с: пауза перед перезапуском упавшего cloudflared
TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)


def binary():
    """Готовый cloudflared: свой (STREAM_TTS_CLOUDFLARED), скачанный кнопкой или из PATH; "" — надо скачать."""
    own = paths.env("STREAM_TTS_CLOUDFLARED")
    if own:
        return own
    local = os.path.join(DIR, "cloudflared" + paths.EXE)
    if os.path.isfile(local):
        return local
    return shutil.which("cloudflared") or ""


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Tunnel:
    def __init__(self, settings, changed):
        """changed() — разбудить админку. port — порт страницы голосов (web/public.py), задаётся при запуске."""
        self.s, self.changed = settings, changed
        self.port = 0
        self.state, self.url, self.error = "off", "", ""   # off | download | starting | on | error
        self.dl = dict(file="", done=0, total=0, speed=0.0)
        self.task = self.proc = None
        self.tail = collections.deque(maxlen=20)
        self._last_push = 0.0
        self._job = None   # Windows: cloudflared завершается вместе с stream-tts, даже убитым (как сервер синтеза)
        if os.name == "nt":
            try:
                self._job = _Job()
            except OSError as e:
                log.warning("Job Object не создан, cloudflared может пережить stream-tts: %s", e)

    def public(self):
        return dict(on=bool(self.s.get("tunnel")), state=self.state, url=self.url, error=self.error,
                    download=dict(self.dl) if self.state == "download" else None,
                    have=bool(binary()), size=PIN["size"])

    # --- включение ---
    async def set(self, on):
        self.s.update(tunnel=bool(on))
        if on:
            self.start()
        else:
            await self.stop()

    def start(self):
        if not self.task or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.state, self.url, self.error = "off", "", ""
        self._push(force=True)

    async def _run(self):
        while True:
            try:
                await self._once(binary() or await self._download())
                err = tr("cloudflared завершился: {line}", line=self._last_error())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                err = str(e) or type(e).__name__
            self.state, self.url, self.error = "error", "", err
            log.warning("туннель: %s; повтор через %d с", err, RETRY)
            self._push(force=True)
            await asyncio.sleep(RETRY)

    def _last_error(self):
        lines = [ln for ln in self.tail if " ERR " in ln] or list(self.tail) or ["?"]
        return lines[-1]

    # --- процесс cloudflared ---
    async def _once(self, exe):
        """Один запуск cloudflared: ждёт адрес, потом — пока процесс не завершится."""
        if not self.port:
            raise RuntimeError(tr("страница голосов для туннеля не запущена"))
        metrics = _free_port()
        args = [exe, "tunnel", "--no-autoupdate", "--metrics", f"127.0.0.1:{metrics}",
                "--url", f"http://127.0.0.1:{self.port}"]
        if exe.endswith(".py"):   # поддельный cloudflared тестов
            args.insert(0, sys.executable)
        self.state, self.error = "starting", ""
        self.tail.clear()
        self._push(force=True)
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, **spawn_kwargs())
        if self._job:
            try:
                self._job.add(self.proc.pid)
            except OSError as e:
                log.warning("cloudflared не добавлен в Job Object: %s", e)
        reader = asyncio.create_task(self._read(self.proc))
        try:
            try:
                host = await asyncio.wait_for(self._address(metrics), START_WAIT)
            except asyncio.TimeoutError:
                raise RuntimeError(tr("Cloudflare не выдал адрес за {n} с: {line}", n=int(START_WAIT),
                                      line=self._last_error())) from None
            self.state, self.url = "on", f"https://{host}/"
            log.info("страница голосов в интернете: %s", self.url)
            self._push(force=True)
            await self.proc.wait()
        finally:
            await self._kill()
            reader.cancel()

    async def _read(self, proc):
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if line:
                self.tail.append(line)

    async def _address(self, metrics):
        """Имя туннеля, когда cloudflared соединился с Cloudflare; процесс завершился — RuntimeError."""
        base = f"http://127.0.0.1:{metrics}"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as s:
            while True:
                if self.proc.returncode is not None:
                    await asyncio.sleep(0.2)   # дочитать вывод
                    raise RuntimeError(tr("cloudflared завершился: {line}", line=self._last_error()))
                with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    async with s.get(base + "/ready") as r:
                        ready = r.status == 200
                    if ready:
                        async with s.get(base + "/quicktunnel") as r:
                            host = (await r.json(content_type=None)).get("hostname")
                        if host:
                            return host
                await asyncio.sleep(0.5)

    async def _kill(self):
        p, self.proc = self.proc, None
        if not p or p.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            p.terminate()
        try:
            await asyncio.wait_for(p.wait(), 5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                p.kill()
            await p.wait()

    # --- загрузка ---
    async def _download(self):
        dst = os.path.join(DIR, "cloudflared" + paths.EXE)
        self.state = "download"
        self.dl = dict(file="cloudflared", done=fetch.part_size(dst), total=PIN["size"], speed=0.0)
        self._push(force=True)
        log.info("скачиваю cloudflared %s", VERSION)
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            await fetch.fetch(self, session, PIN["url"], "cloudflared", dst, PIN["size"], PIN["sha256"])
        if os.name != "nt":
            os.chmod(dst, 0o755)
        return dst

    def _push(self, force=False):
        now = time.monotonic()
        if force or now - self._last_push > 0.5:
            self._last_push = now
            self.changed()
