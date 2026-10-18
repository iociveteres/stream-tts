"""Проверка новой версии программы для Windows: latest.json из последнего релиза на GitHub, без своего сервера.

CI (.github/workflows/windows.yml, packaging/windows/release.py) кладёт в каждый релиз latest.json:
  {"version": "0.2.0", "date": "2026-10-20", "url": "<страница релиза>", "notes": "<что нового>",
   "files": {"setup": {"name", "url", "size", "sha256"}}}
Ссылка …/releases/latest/download/latest.json всегда ведёт на файл последнего релиза (не pre-release) и отдаётся
с CDN GitHub: лимит API (60 запросов в час на IP) на неё не действует.

Проверка — раз в день, неделю или месяц (settings["update_check"], "off" — не проверять) и по кнопке в
настройках. Найденная версия хранится в settings["update_latest"]: плашка в админке видна и после перезапуска без
сети. Скачивание и установка пока вручную, по ссылке на страницу релиза.

Проверяет только собранная программа: из исходников и в Docker обновляются через git. STREAM_TTS_UPDATE_URL задаёт
другой адрес latest.json (форк, проверка на локальном сервере) и включает проверку и там."""
import asyncio, json, logging, re, time

import aiohttp

from . import __version__, paths
from .i18n import tr

log = logging.getLogger("updates")

REPO = "iociveteres/stream-tts"
URL = paths.env("STREAM_TTS_UPDATE_URL", f"https://github.com/{REPO}/releases/latest/download/latest.json")
AVAILABLE = paths.FROZEN or bool(paths.env("STREAM_TTS_UPDATE_URL"))
INTERVALS = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400, "off": None}
STARTUP_DELAY = 30     # первая проверка — когда программа и cosyvoice-server уже запустились
TICK = 3600            # как часто смотреть, не пора ли: смена частоты в настройках действует с этим шагом
RETRY = 3 * 3600       # после ошибки (нет сети) — не чаще
MAX_SIZE = 1 << 20


def parse(version):
    """«0.2.0» или «v0.2» -> (0, 2, 0); pre-release и прочее — None."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)(?:\.(\d+))?", str(version).strip())
    return tuple(int(x or 0) for x in m.groups()) if m else None


class UpdateError(Exception):
    pass


class Updates:
    def __init__(self, settings, changed):
        self.s, self.changed = settings, changed  # changed() — разбудить админку
        self.lock = asyncio.Lock()
        self.checking = False
        self.error = ""
        self.tried = 0.0

    def public(self):
        latest = self.s.get("update_latest") or None
        newer = bool(latest) and parse(latest["version"]) > parse(__version__)
        return dict(available=AVAILABLE, version=__version__, check=self.s["update_check"], checking=self.checking,
                    checked=self.s.get("update_checked", 0), error=self.error, latest=latest, newer=newer,
                    dismissed=self.s.get("update_dismissed", ""))

    def due(self):
        interval = INTERVALS.get(self.s["update_check"])
        now = time.time()
        return (AVAILABLE and interval is not None and now - self.s.get("update_checked", 0) >= interval
                and now - self.tried >= RETRY)

    async def worker(self):
        await asyncio.sleep(STARTUP_DELAY)
        while True:
            if self.due():
                await self.check()
            await asyncio.sleep(TICK)

    async def check(self):
        """Проверить сейчас. Вторая проверка во время первой просто дождётся её."""
        if not AVAILABLE:
            raise ValueError(tr("обновления проверяет только программа для Windows"))
        if self.lock.locked():
            async with self.lock:
                return
        async with self.lock:
            self.checking, self.tried = True, time.time()
            self.changed()
            try:
                latest = await self._fetch()
                self.error = ""
                self.s.data.update(update_checked=time.time(), update_latest=latest)
                self.s.save()
                if parse(latest["version"]) > parse(__version__):
                    log.info("доступна версия %s (установлена %s): %s", latest["version"], __version__, latest["url"])
            except UpdateError as e:
                self.error = str(e)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self.error = tr("нет связи с GitHub: {e}", e=str(e) or type(e).__name__)
            if self.error:
                log.warning("проверка обновлений: %s", self.error)
            self.checking = False
            self.changed()

    async def _fetch(self):
        timeout = aiohttp.ClientTimeout(total=30, sock_connect=15)
        headers = {"User-Agent": f"StreamTTS/{__version__}"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(URL) as r:
                if r.status == 404:
                    raise UpdateError(tr("на GitHub пока нет релизов"))
                if r.status != 200:
                    raise UpdateError(tr("GitHub ответил HTTP {status}", status=r.status))
                raw = await r.content.read(MAX_SIZE)
        try:
            data = json.loads(raw)
        except ValueError:
            raise UpdateError(tr("latest.json повреждён"))
        return _validate(data)


def _validate(data):
    """Только нужные поля и только безопасные ссылки: url показывается в админке ссылкой."""
    if not isinstance(data, dict) or not parse(data.get("version", "")):
        raise UpdateError(tr("в latest.json нет версии"))
    https = lambda u: u if isinstance(u, str) and u.startswith("https://") else ""
    files = {}
    for k, f in (data.get("files") or {}).items() if isinstance(data.get("files"), dict) else ():
        if isinstance(f, dict) and https(f.get("url")):
            files[str(k)] = dict(name=str(f.get("name", "")), url=f["url"], size=int(f.get("size") or 0),
                                 sha256=str(f.get("sha256", "")))
    return dict(version=str(data["version"]).lstrip("v"), date=str(data.get("date", ""))[:32],
                url=https(data.get("url")) or f"https://github.com/{REPO}/releases/latest",
                notes=str(data.get("notes", ""))[:4000], files=files)
