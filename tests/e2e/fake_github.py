"""Поддельный GitHub (github.com/login, api.github.com) для e2e-тестов публикации страницы голосов (pages.py). Адрес —
в STREAM_TTS_TEST_GITHUB (config.TEST_GITHUB, только для тестов).

Отвечает на то, что шлёт streamtts/pages.py:
- /login: Device Flow (device/code, oauth/access_token: authorization_pending до authorize(), deny() — access_denied).
  Неизвестный Client ID — ошибка, как у GitHub.
- /api: GET /user; репозиторий (GET, POST /user/repos, has_pages — enable_pages()); PUT contents (первый коммит пустого
  репозитория); Git Data: ref, commits, trees (recursive), blobs, создание дерева и коммита, PATCH ref (force).
  Пустой репозиторий на git-запросы — 409, как у GitHub. Отозванный токен — 401.

Тест читает состояние репозиториев (files, blob_posts, commits) и задаёт его (add_repo, enable_pages, revoke).
Методы-корутины — в цикле событий теста (Loop.run), остальное читается из потока теста."""
import base64, hashlib, itertools, json

from aiohttp import web

CLIENT_ID = "test-client"
USER = "Tester"


def sha1(data):
    return hashlib.sha1(data).hexdigest()


def blob_sha(data):
    return sha1(b"blob %d\0" % len(data) + data)


class Repo:
    def __init__(self, name, branch="main"):
        self.name, self.branch = name, branch
        self.has_pages = False
        self.objects = {}     # sha -> ("blob", bytes) | ("tree", [entries]) | ("commit", dict)
        self.head = None      # sha коммита ветки; None — пустой репозиторий

    def put_blob(self, data):
        sha = blob_sha(data)
        self.objects[sha] = ("blob", data)
        return sha

    def put_tree(self, entries):
        sha = sha1(json.dumps(entries, sort_keys=True).encode())
        self.objects[sha] = ("tree", entries)
        return sha

    def put_commit(self, tree, parents, message):
        sha = sha1(json.dumps([tree, parents, message, len(self.objects)]).encode())
        self.objects[sha] = ("commit", dict(tree=tree, parents=parents, message=message))
        return sha

    def commit_files(self, files, message="init"):
        """Тест: коммит {путь: bytes} поверх пустой истории."""
        tree = self.put_tree([dict(path=p, mode="100644", type="blob", sha=self.put_blob(d)) for p, d in files.items()])
        self.head = self.put_commit(tree, [], message)

    def files(self):
        """{путь: bytes} на ветке."""
        if not self.head:
            return {}
        tree = self.objects[self.objects[self.head][1]["tree"]][1]
        return {e["path"]: self.objects[e["sha"]][1] for e in tree}

    def json(self, base):
        return dict(name=self.name, full_name=f"{USER}/{self.name}", private=False, default_branch=self.branch,
                    has_pages=self.has_pages, html_url=f"{base}/{USER}/{self.name}")


class FakeGitHub:
    def __init__(self):
        self.runner = None
        self.url = ""
        self.reset()

    def reset(self):
        self.ids = itertools.count(1)
        self.devices = {}     # device_code -> "pending" | "ok" | "denied"
        self.tokens = {}      # access_token -> действует ли
        self.repos = {}       # имя -> Repo
        self.blob_posts = []  # sha загруженных блобов
        self.commits = 0      # коммитов через Git Data API

    # --- управление из теста ---
    def authorize(self, ok=True):
        for code, st in self.devices.items():
            if st == "pending":
                self.devices[code] = "ok" if ok else "denied"

    def add_repo(self, name, files=None):
        repo = self.repos[name] = Repo(name)
        if files:
            repo.commit_files(files)
        return repo

    def enable_pages(self, name):
        self.repos[name].has_pages = True

    def revoke(self):
        for t in self.tokens:
            self.tokens[t] = False

    # --- сервер ---
    async def start(self):
        app = web.Application(client_max_size=64 << 20)
        r = app.router
        r.add_post("/login/device/code", self.device_code)
        r.add_post("/login/oauth/access_token", self.access_token)
        r.add_get("/api/user", self.user)
        r.add_get("/api/repos/{owner}/{repo}", self.get_repo)
        r.add_post("/api/user/repos", self.create_repo)
        r.add_put("/api/repos/{owner}/{repo}/contents/{path:.+}", self.put_contents)
        r.add_get("/api/repos/{owner}/{repo}/git/ref/heads/{branch}", self.get_ref)
        r.add_patch("/api/repos/{owner}/{repo}/git/refs/heads/{branch}", self.patch_ref)
        r.add_get("/api/repos/{owner}/{repo}/git/commits/{sha}", self.get_commit)
        r.add_post("/api/repos/{owner}/{repo}/git/commits", self.post_commit)
        r.add_get("/api/repos/{owner}/{repo}/git/trees/{sha}", self.get_tree)
        r.add_post("/api/repos/{owner}/{repo}/git/trees", self.post_tree)
        r.add_post("/api/repos/{owner}/{repo}/git/blobs", self.post_blob)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{self.runner.addresses[0][1]}"
        return self

    async def close(self):
        if self.runner:
            await self.runner.cleanup()

    # --- вход ---
    async def device_code(self, request):
        form = await request.post()
        if form.get("client_id") != CLIENT_ID:
            return web.json_response(dict(error="Not Found"), status=404)
        code = f"dev-{next(self.ids)}"
        self.devices[code] = "pending"
        return web.json_response(dict(device_code=code, user_code="ABCD-1234", expires_in=900, interval=0.1,
                                      verification_uri="https://github.com/login/device"))

    async def access_token(self, request):
        form = await request.post()
        st = self.devices.get(form.get("device_code"))
        if form.get("client_id") != CLIENT_ID:
            return web.json_response(dict(error="incorrect_client_credentials"))
        if st is None:
            return web.json_response(dict(error="incorrect_device_code"))
        if st == "pending":
            return web.json_response(dict(error="authorization_pending"))
        del self.devices[form["device_code"]]
        if st == "denied":
            return web.json_response(dict(error="access_denied"))
        token = f"gho_{next(self.ids)}"
        self.tokens[token] = True
        return web.json_response(dict(access_token=token, token_type="bearer", scope="public_repo"))

    # --- API ---
    def _auth(self, request):
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not self.tokens.get(token):
            raise web.HTTPUnauthorized(text=json.dumps(dict(message="Bad credentials")),
                                       content_type="application/json")

    def _repo(self, request, git=False):
        self._auth(request)
        m = request.match_info
        repo = self.repos.get(m["repo"]) if m["owner"] == USER else None
        if not repo:
            raise web.HTTPNotFound(text=json.dumps(dict(message="Not Found")), content_type="application/json")
        if git and not repo.head:
            raise web.HTTPConflict(text=json.dumps(dict(message="Git Repository is empty.")),
                                   content_type="application/json")
        return repo

    async def user(self, request):
        self._auth(request)
        return web.json_response(dict(login=USER, id=1))

    async def get_repo(self, request):
        return web.json_response(self._repo(request).json(self.url))

    async def create_repo(self, request):
        self._auth(request)
        body = await request.json()
        if body["name"] in self.repos:
            return web.json_response(dict(message="Repository creation failed."), status=422)
        repo = self.add_repo(body["name"])
        if body.get("auto_init"):
            repo.commit_files({"README.md": b"# " + body["name"].encode()})
        return web.json_response(repo.json(self.url), status=201)

    async def put_contents(self, request):
        repo = self._repo(request)
        body = await request.json()
        files = repo.files()
        files[request.match_info["path"]] = base64.b64decode(body["content"])
        old = repo.head
        repo.commit_files(files, body["message"])
        if old:
            repo.objects[repo.head][1]["parents"] = [old]
        return web.json_response(dict(commit=dict(sha=repo.head)), status=201)

    async def get_ref(self, request):
        repo = self._repo(request, git=True)
        if request.match_info["branch"] != repo.branch:
            raise web.HTTPNotFound(text=json.dumps(dict(message="Not Found")), content_type="application/json")
        return web.json_response(dict(ref=f"refs/heads/{repo.branch}", object=dict(sha=repo.head, type="commit")))

    async def patch_ref(self, request):
        repo = self._repo(request, git=True)
        body = await request.json()
        commit = repo.objects.get(body["sha"])
        if not commit or commit[0] != "commit":
            return web.json_response(dict(message="Object does not exist"), status=422)
        if repo.head not in commit[1]["parents"] and not body.get("force"):
            return web.json_response(dict(message="Update is not a fast forward"), status=422)
        repo.head = body["sha"]
        return web.json_response(dict(ref=f"refs/heads/{repo.branch}", object=dict(sha=repo.head)))

    def _object(self, repo, sha, kind):
        obj = repo.objects.get(sha)
        if not obj or obj[0] != kind:
            raise web.HTTPNotFound(text=json.dumps(dict(message="Not Found")), content_type="application/json")
        return obj[1]

    async def get_commit(self, request):
        repo = self._repo(request, git=True)
        c = self._object(repo, request.match_info["sha"], "commit")
        return web.json_response(dict(sha=request.match_info["sha"], tree=dict(sha=c["tree"]),
                                      parents=[dict(sha=p) for p in c["parents"]], message=c["message"]))

    async def post_commit(self, request):
        repo = self._repo(request, git=True)
        body = await request.json()
        self._object(repo, body["tree"], "tree")
        self.commits += 1
        return web.json_response(dict(sha=repo.put_commit(body["tree"], body.get("parents", []), body["message"])),
                                 status=201)

    async def get_tree(self, request):
        repo = self._repo(request, git=True)
        entries = self._object(repo, request.match_info["sha"], "tree")
        return web.json_response(dict(sha=request.match_info["sha"], tree=entries, truncated=False))

    async def post_tree(self, request):
        repo = self._repo(request, git=True)
        body = await request.json()
        for e in body["tree"]:
            self._object(repo, e["sha"], "blob")
        return web.json_response(dict(sha=repo.put_tree(body["tree"])), status=201)

    async def post_blob(self, request):
        repo = self._repo(request, git=True)
        body = await request.json()
        sha = repo.put_blob(base64.b64decode(body["content"]))
        self.blob_posts.append(sha)
        return web.json_response(dict(sha=sha), status=201)
