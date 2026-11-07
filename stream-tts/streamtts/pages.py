"""Страница голосов на GitHub Pages: постоянный адрес <ник>.github.io/tts-voices/, работает и при выключенной программе.

Вход — Device Code Flow OAuth-приложения stream-tts (CLIENT_ID, секрет не нужен): код вводится на
github.com/login/device. Область — public_repo: включить Pages через API можно только с repo, а это полный доступ и
к приватным репозиториям (документация GitHub, 2026-11). Поэтому Pages стример включает сам, один раз, по ссылке из
админки на Settings → Pages; включение программа видит по has_pages репозитория.

Репозиторий REPO создаёт программа; свой узнаёт по файлу MARKER и чужой репозиторий с тем же именем не трогает.
Публикация — один коммит через Git Data API: блобы только изменившихся файлов, дерево целиком (выключенные голоса
пропадают), коммит без родителя и принудительная перезапись ветки, чтобы история не копила старые mp3. Публикует сама
после изменений страницы (голоса, примеры, модель, язык): ждёт, пока страница DEBOUNCE с не меняется и примеры
досинтезированы (но не дольше SETTLE с), — так сборок Pages меньше лимита GitHub (~10 в час).

Токен — <данные>/github.json; токены OAuth-приложений GitHub не истекают, отозванный — 401 и выход.
STREAM_TTS_GITHUB_CLIENT_ID — своё приложение; STREAM_TTS_TEST_GITHUB — поддельный GitHub тестов
(tests/e2e/fake_github.py)."""
import asyncio, base64, contextlib, hashlib, json, logging, os, time

import aiohttp

from . import __version__, config, paths, store
from .i18n import tr

log = logging.getLogger("pages")

# OAuth-приложение StreamTTS на GitHub (владелец — iociveteres, Device Flow включён в его настройках); Client ID
# публичный, секрет в Device Flow не нужен
CLIENT_ID = paths.env("STREAM_TTS_GITHUB_CLIENT_ID", "Ov23li7PeFOtjydSQDHm")
SCOPE = "public_repo"
LOGIN, API = "https://github.com/login", "https://api.github.com"
if config.TEST_GITHUB:
    LOGIN, API = f"{config.TEST_GITHUB}/login", f"{config.TEST_GITHUB}/api"
REPO = "tts-voices"
MARKER = "stream-tts.json"
# Файлы, с которыми GitHub создаёт репозиторий: такой репозиторий ещё ничей, его можно занять
FRESH = {"README.md", "LICENSE", ".gitignore"}
DEBOUNCE = float(paths.env("STREAM_TTS_PAGES_DEBOUNCE", "30"))   # с: страница не менялась столько — публикуем
SETTLE = float(paths.env("STREAM_TTS_PAGES_SETTLE", "600"))      # с: дольше примеры не ждём
CHECK = float(paths.env("STREAM_TTS_PAGES_CHECK", "15"))         # с: проверка изменений и включения Pages
RETRY = float(paths.env("STREAM_TTS_PAGES_RETRY", "60"))         # с: пауза после ошибки GitHub
# GitHub: не больше 80 запросов, создающих данные, в минуту — первая публикация 60+ голосов идёт с паузами
MANY_BLOBS, BLOB_PAUSE = 50, 0.8
TIMEOUT = aiohttp.ClientTimeout(total=120, sock_connect=30)
MARKER_BODY = json.dumps(dict(app="stream-tts", note="Voices page published by StreamTTS. Manual edits get overwritten."),
                         indent=1).encode() + b"\n"


class PagesError(Exception):
    pass


class Unauthorized(Exception):
    pass


def git_sha(data):
    """sha блоба git — как его посчитает GitHub: по нему видно, какие файлы уже лежат в репозитории."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class Pages:
    def __init__(self, voices, changed):
        """voices — страница (voices.site, progress примеров); changed() — разбудить админку."""
        self.voices, self.changed = voices, changed
        self.path = os.path.join(config.DATA, "github.json")
        self.tok = store.read_json(self.path, None)   # {access_token, login, client_id}
        self.state = "idle" if self.tok else "off"    # off | device | idle | working | error
        self.device, self.error = None, ""
        self.repo = None       # {branch, pages}: репозиторий проверен
        self.pushed = None     # {путь: sha} на ветке после последней публикации
        self.published, self.pending = 0.0, False
        self.now = False       # «Опубликовать сейчас»: без ожидания
        self.wake = asyncio.Event()
        self.http = self.task = self.device_task = None
        self._sha = {}         # путь файла -> (mtime_ns, размер, sha)

    @property
    def login(self):
        return (self.tok or {}).get("login", "")

    @property
    def url(self):
        """Адрес страницы, когда Pages включён; иначе ""."""
        return f"https://{self.login.lower()}.github.io/{REPO}/" if self.login and self.repo and self.repo["pages"] else ""

    def public(self):
        repo = f"https://github.com/{self.login}/{REPO}" if self.login else ""
        return dict(client=bool(CLIENT_ID), state=self.state, error=self.error, device=self.device, login=self.login,
                    repo=f"{self.login}/{REPO}" if self.login else "", repo_url=repo,
                    settings_url=repo + "/settings/pages" if repo else "", checked=self.repo is not None,
                    branch=(self.repo or {}).get("branch", "main"), pages=bool(self.repo and self.repo["pages"]),
                    url=self.url, published=self.published, pending=self.pending)

    # --- запуск ---
    async def start(self):
        self.http = aiohttp.ClientSession(timeout=TIMEOUT, headers={
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": f"StreamTTS/{__version__}"})
        self.task = asyncio.create_task(self._worker())
        self.wake.set()   # после перезапуска программы — сразу сверить репозиторий, не через CHECK

    async def stop(self):
        for t in (self.task, self.device_task):
            if t:
                t.cancel()
        await asyncio.gather(*(t for t in (self.task, self.device_task) if t), return_exceptions=True)
        if self.http:
            await self.http.close()

    # --- вход ---
    async def login_start(self):
        """Device Code Flow: код для github.com/login/device, дальше опрос в фоне."""
        if not CLIENT_ID:
            raise PagesError(tr("Вход через GitHub в этой сборке не настроен"))
        await self.logout()
        try:
            async with self.http.post(f"{LOGIN}/device/code", data=dict(client_id=CLIENT_ID, scope=SCOPE),
                                      headers={"Accept": "application/json"}) as r:
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            raise PagesError(tr("GitHub недоступен: {e}", e=e or type(e).__name__)) from None
        if r.status != 200 or "device_code" not in data:
            raise PagesError(tr("GitHub не начал вход: {msg}", msg=data.get("error_description") or data.get("error")
                                or r.status))
        self.device = dict(code=data["user_code"], uri=data["verification_uri"],
                           expires=time.time() + data.get("expires_in", 900))
        self.state, self.error = "device", ""
        self.device_task = asyncio.create_task(self._poll(data["device_code"], data.get("interval", 5)))
        self.changed()

    async def _poll(self, device_code, interval):
        try:
            while time.time() < self.device["expires"]:
                await asyncio.sleep(interval)
                with contextlib.suppress(aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    async with self.http.post(f"{LOGIN}/oauth/access_token", headers={"Accept": "application/json"},
                                              data=dict(client_id=CLIENT_ID, device_code=device_code,
                                                        grant_type="urn:ietf:params:oauth:grant-type:device_code")) as r:
                        data = await r.json(content_type=None)
                    if data.get("access_token"):
                        await self._logged_in(data["access_token"])
                        return
                    err = data.get("error", "")
                    if err == "authorization_pending":
                        continue
                    if err == "slow_down":
                        interval = data.get("interval", interval + 5)
                        continue
                    self.device, self.state = None, "off"
                    self.error = {
                        "access_denied": tr("Вход отменён на GitHub."),
                        "expired_token": tr("Код истёк, начните вход заново."),
                        "token_expired": tr("Код истёк, начните вход заново."),
                    }.get(err) or tr("Вход не удался: {msg}", msg=data.get("error_description") or err or r.status)
                    return
            self.device, self.state, self.error = None, "off", tr("Код истёк, начните вход заново.")
        finally:
            self.device_task = None
            self.changed()

    async def _logged_in(self, token):
        async with self.http.get(f"{API}/user", headers={"Authorization": f"Bearer {token}"}) as r:
            user = await r.json(content_type=None)
        if r.status != 200:
            raise aiohttp.ClientError(f"/user: {r.status}")
        self.tok = dict(access_token=token, login=user["login"], client_id=CLIENT_ID)
        store.write_json(self.path, self.tok)
        log.info("GitHub: вошли как %s", self.tok["login"])
        self.device, self.state, self.error = None, "idle", ""
        self.repo = self.pushed = None
        self.publish_now()

    async def logout(self):
        """Забыть токен. Репозиторий и страница остаются на GitHub."""
        if self.device_task:
            self.device_task.cancel()
            await asyncio.gather(self.device_task, return_exceptions=True)
        self._forget("")

    def _forget(self, error):
        if self.tok:
            log.info("GitHub: выход (%s)", error or "кнопкой")
        with contextlib.suppress(OSError):
            os.remove(self.path)
        self.tok = self.device = self.repo = self.pushed = None
        self.state, self.error, self.pending, self.published = "off", error, False, 0.0
        self.changed()

    def publish_now(self):
        self.now = True
        self.wake.set()

    # --- публикация ---
    async def _worker(self):
        changed_at = dirty_since = 0.0
        last = None
        while True:
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), CHECK)
            self.wake.clear()
            if not self.tok:
                last, self.now = None, False
                continue
            token = self.tok["access_token"]
            try:
                if self.repo is None:
                    await self._ensure_repo()
                elif not self.repo["pages"]:
                    await self._check_pages()
                files = await asyncio.to_thread(self._files)
                digest = {p: sha for p, (sha, _) in files.items()}
                now = time.monotonic()
                if digest != last:
                    last, changed_at = digest, now
                if digest == self.pushed:
                    dirty_since, self.now, self.pending = 0.0, False, False
                    self._set("idle")
                    continue
                dirty_since = dirty_since or now
                self.pending = True
                settled = not self.voices.progress.get("todo") or now - dirty_since >= SETTLE
                if self.now or (now - changed_at >= DEBOUNCE and settled):
                    self.now = False
                    await self._publish(files, digest)
                    dirty_since, self.pending = 0.0, False
                self._set("idle")
            except Unauthorized:
                if self.tok and self.tok["access_token"] == token:
                    self._forget(tr("GitHub отозвал доступ программы: подключите GitHub снова."))
            except (PagesError, aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                msg = str(e) if isinstance(e, PagesError) else tr("GitHub недоступен: {e}", e=e or type(e).__name__)
                log.warning("GitHub Pages: %s; повтор через %d с", msg, RETRY)
                self._set("error", msg)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._woken(), RETRY)

    async def _woken(self):
        """Ждать «Опубликовать сейчас» или выхода (кнопки будят воркер)."""
        await self.wake.wait()

    def _set(self, state, error=""):
        if (self.state, self.error) != (state, error):
            self.state, self.error = state, error
            self.changed()

    def _files(self):
        """{путь: (sha, bytes | путь к файлу)} — страница, примеры, метка репозитория; sha файлов — из кэша по mtime."""
        out = {MARKER: (git_sha(MARKER_BODY), MARKER_BODY), ".nojekyll": (git_sha(b""), b"")}
        for path, src in self.voices.site().items():
            if isinstance(src, bytes):
                out[path] = (git_sha(src), src)
                continue
            st = os.stat(src)
            key = (st.st_mtime_ns, st.st_size)
            cached = self._sha.get(src)
            if not cached or cached[:2] != key:
                with open(src, "rb") as f:
                    cached = (*key, git_sha(f.read()))
                self._sha[src] = cached
            out[path] = (cached[2], src)
        return out

    async def _api(self, method, path, body=None, allow=()):
        """→ (код, JSON). 401 — Unauthorized; лимит — пауза и повтор; другой код не 2xx и не из allow — PagesError."""
        for attempt in range(3):
            async with self.http.request(method, API + path, json=body,
                                         headers={"Authorization": f"Bearer {self.tok['access_token']}"}) as r:
                text = await r.text()
            try:
                data = json.loads(text) if text else {}
            except ValueError:
                data = {}
            if r.status == 401:
                raise Unauthorized()
            limited = r.status == 429 or (r.status == 403 and (r.headers.get("Retry-After")
                                                               or r.headers.get("X-RateLimit-Remaining") == "0"))
            if limited and attempt < 2:
                wait = int(r.headers.get("Retry-After") or 60)
                log.warning("GitHub: лимит запросов, пауза %d с", wait)
                await asyncio.sleep(min(wait, 120))
                continue
            if r.status < 300 or r.status in allow:
                return r.status, data
            msg = data.get("message") if isinstance(data, dict) else ""
            raise PagesError(tr("Ошибка GitHub ({code}): {msg}", code=r.status, msg=msg or method + " " + path))
        raise PagesError(tr("GitHub: превышен лимит запросов, повтор позже"))

    async def _ensure_repo(self):
        """Свой репозиторий: нет — создать; пустой — первый коммит с меткой; чужой — ошибка, ничего не трогаем."""
        name = f"/repos/{self.login}/{REPO}"
        status, repo = await self._api("GET", name, allow=(404,))
        created = status == 404
        if created:
            _, repo = await self._api("POST", "/user/repos", dict(
                name=REPO, description=tr("Голоса озвучки для зрителей — страницу выкладывает StreamTTS"),
                homepage=f"https://{self.login.lower()}.github.io/{REPO}/", has_issues=False, has_wiki=False,
                has_projects=False, auto_init=False))
            log.info("GitHub: создан репозиторий %s/%s", self.login, REPO)
        branch = repo.get("default_branch") or "main"
        tree = await self._tree(branch)
        if tree is None:   # пустой репозиторий: Git Data API в нём не работает, первый коммит — через contents
            await self._api("PUT", f"{name}/contents/{MARKER}", dict(
                message="StreamTTS", content=base64.b64encode(MARKER_BODY).decode(), branch=branch))
            tree = await self._tree(branch) or {}
        elif not created and MARKER not in tree and set(tree) - FRESH:
            raise PagesError(tr("На GitHub уже есть репозиторий {repo}, и его создала не программа. Переименуйте "
                                "или удалите его, затем нажмите «Опубликовать сейчас».", repo=f"{self.login}/{REPO}"))
        self.repo = dict(branch=branch, pages=bool(repo.get("has_pages")))
        self.pushed = tree
        self.changed()

    async def _check_pages(self):
        _, repo = await self._api("GET", f"/repos/{self.login}/{REPO}")
        if repo.get("has_pages"):
            log.info("GitHub Pages включён: %s", f"https://{self.login.lower()}.github.io/{REPO}/")
            self.repo["pages"] = True
            self.changed()

    async def _tree(self, branch):
        """{путь: sha} файлов ветки; None — веток нет (пустой репозиторий)."""
        name = f"/repos/{self.login}/{REPO}/git"
        status, ref = await self._api("GET", f"{name}/ref/heads/{branch}", allow=(404, 409))
        if status in (404, 409):
            return None
        _, commit = await self._api("GET", f"{name}/commits/{ref['object']['sha']}")
        _, tree = await self._api("GET", f"{name}/trees/{commit['tree']['sha']}?recursive=1")
        return {e["path"]: e["sha"] for e in tree.get("tree", []) if e.get("type") == "blob"}

    async def _publish(self, files, digest):
        name = f"/repos/{self.login}/{REPO}/git"
        have = set((self.pushed or {}).values())
        new = {sha: src for sha, src in files.values() if sha not in have}
        self._set("working")
        log.info("GitHub Pages: публикую, новых файлов %d", len(new))
        for i, (sha, src) in enumerate(new.items()):
            if i and len(new) > MANY_BLOBS:
                await asyncio.sleep(BLOB_PAUSE)
            if not isinstance(src, bytes):
                src = await asyncio.to_thread(_read, src)
            _, blob = await self._api("POST", f"{name}/blobs", dict(content=base64.b64encode(src).decode(),
                                                                     encoding="base64"))
            if blob.get("sha") != sha:   # файл поменялся между хэшем и загрузкой: следующий круг опубликует снова
                digest = dict(digest, _changed="")
        _, tree = await self._api("POST", f"{name}/trees", dict(tree=[
            dict(path=p, mode="100644", type="blob", sha=sha) for p, (sha, _) in sorted(files.items())]))
        voices = sum(p.startswith("demos/") for p in files)
        _, commit = await self._api("POST", f"{name}/commits", dict(
            message=f"StreamTTS: {voices} voice samples", tree=tree["sha"], parents=[]))
        await self._api("PATCH", f"{name}/refs/heads/{self.repo['branch']}", dict(sha=commit["sha"], force=True))
        self.pushed, self.published = digest, time.time()
        log.info("GitHub Pages: опубликовано (%d файлов)", len(files))
        self.changed()


def _read(path):
    with open(path, "rb") as f:
        return f.read()
