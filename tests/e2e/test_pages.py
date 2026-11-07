"""Страница голосов на GitHub Pages (pages.py, карточка «Страница голосов для зрителей», !ttsvoices) на поддельном
GitHub (fake_github.py): вход по коду, свой репозиторий tts-voices, публикация одним коммитом без истории, шаг
включения Pages и адрес, обновление после изменений голосов (только изменившиеся файлы), вход после перезапуска,
чужой репозиторий с тем же именем не трогается, отозванный доступ и отказ во входе."""
import asyncio, os, subprocess, sys, types, urllib.request

import pytest

from fake_github import CLIENT_ID, FakeGitHub
from harness import STREAM_TTS, Api, wait_for

sys.path.insert(0, STREAM_TTS)
from streamtts import commands, pages as pages_mod  # noqa: E402

REPO = pages_mod.REPO


@pytest.fixture(scope="module")
def gh(loop):
    fake = loop.run(FakeGitHub().start())
    yield fake
    loop.run(fake.close())


@pytest.fixture
def run(spawn, gh):
    """run(**spawn) -> (StreamTTS, FakeCosy, Api) с поддельным GitHub и короткими паузами публикации."""
    gh.reset()

    def make(**kw):
        env = dict(STREAM_TTS_TEST_GITHUB=gh.url, STREAM_TTS_GITHUB_CLIENT_ID=CLIENT_ID, STREAM_TTS_PAGES_DEBOUNCE="0.3",
                   STREAM_TTS_PAGES_CHECK="0.2", STREAM_TTS_PAGES_RETRY="0.5", STREAM_TTS_PAGES_SETTLE="10")
        server, cosy = spawn(env=env, **kw)
        return server, cosy, Api(server.base)
    return make


def st(api):
    return api.state()["pages"]


def enabled(api):
    return sorted(v["name"] for v in api.state()["voices"] if v["enabled"])


def login(api, gh):
    api.ok("/api/pages/login")
    p = wait_for(lambda: (x := st(api))["state"] == "device" and x, 5, "кода входа")
    assert p["device"]["code"] == "ABCD-1234" and p["device"]["uri"] == "https://github.com/login/device"
    gh.authorize()
    wait_for(lambda: st(api)["login"] == "Tester", 5, "входа в GitHub")


def published(api, gh, voices):
    """Дождаться, когда на ветке — страница ровно с этими голосами и их примерами, и публиковать нечего."""
    want = {"index.html", ".nojekyll", pages_mod.MARKER} | {f"demos/{v}.mp3" for v in voices}

    def done():
        repo = gh.repos.get(REPO)
        p = st(api)
        return repo and set(repo.files()) == want and not p["pending"] and p["state"] == "idle" and repo.files()
    wait_for(lambda: not api.state()["demos"]["todo"], 20, "примеров голосов")
    return wait_for(done, 15, "публикации на GitHub")


def get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.read()


def test_publish_enable_pages_and_restart(run, gh):
    """Вход по коду → репозиторий создан, страница с примерами всех включённых голосов — одним коммитом без родителя;
    до включения Pages адреса нет, есть ссылка на настройки; включили — адрес <ник>.github.io/tts-voices/. После
    перезапуска программы вход и адрес на месте, а неизменившаяся страница не публикуется заново."""
    server, cosy, api = run()
    p = st(api)
    assert p["client"] and p["state"] == "off" and not p["login"] and not p["url"]
    login(api, gh)
    voices = enabled(api)
    files = published(api, gh, voices)
    page = files["index.html"].decode()
    assert all(f"!ttssf {v}" in page for v in voices) and 'name="robots" content="noindex' in page
    for v in voices:
        assert files[f"demos/{v}.mp3"] == get(f"{server.base}/voices/demos/{v}.mp3")
    repo = gh.repos[REPO]
    assert repo.objects[repo.head][1]["parents"] == [] and gh.commits == 1
    p = st(api)
    assert p["repo"] == f"Tester/{REPO}" and p["checked"] and not p["pages"] and not p["url"]
    assert p["settings_url"] == f"https://github.com/Tester/{REPO}/settings/pages" and p["branch"] == "main"

    gh.enable_pages(REPO)
    p = wait_for(lambda: (x := st(api))["pages"] and x, 5, "включения Pages")
    assert p["url"] == f"https://tester.github.io/{REPO}/"

    server.stop()
    again, _, api = run(workdir=server.dir, cosy=cosy)
    p = wait_for(lambda: (x := st(api))["url"] and x, 10, "адреса после перезапуска")
    assert p["login"] == "Tester" and p["url"] == f"https://tester.github.io/{REPO}/"
    published(api, gh, voices)
    assert gh.commits == 1


def test_republish_only_changes(run, gh):
    """Выключили голос — страница публикуется заново сама: без его карточки и mp3, загружен только новый index.html.
    «Опубликовать сейчас» без изменений коммита не делает."""
    server, cosy, api = run()
    login(api, gh)
    voices = enabled(api)
    published(api, gh, voices)
    posts = len(gh.blob_posts)
    gone = voices[0]
    api.ok("/api/voice", dict(name=gone, enabled=False))
    files = published(api, gh, voices[1:])
    assert f"!ttssf {gone}" not in files["index.html"].decode()
    assert gh.blob_posts[posts:] == [pages_mod.git_sha(files["index.html"])] and gh.commits == 2
    api.ok("/api/pages/publish")
    published(api, gh, voices[1:])
    assert gh.commits == 2


def test_foreign_repo_untouched(run, gh):
    """Репозиторий tts-voices, созданный не программой, — ошибка в карточке, файлы целы; удалили его на GitHub —
    «Опубликовать сейчас» создаёт свой."""
    mine = {"index.html": b"my site", "app.js": b"alert(1)"}
    gh.add_repo(REPO, mine)
    server, cosy, api = run()
    login(api, gh)
    p = wait_for(lambda: (x := st(api))["state"] == "error" and x, 5, "ошибки чужого репозитория")
    assert "создала не программа" in p["error"] and gh.repos[REPO].files() == mine and not gh.blob_posts
    del gh.repos[REPO]
    api.ok("/api/pages/publish")
    published(api, gh, enabled(api))


def test_revoked_and_denied(run, gh):
    """Доступ отозван на GitHub — выход с объяснением, токен удалён; отказ на странице входа — ошибка без входа."""
    server, cosy, api = run()
    r = api.post("/api/pages/publish")
    assert r.status == 400
    login(api, gh)
    published(api, gh, enabled(api))
    assert os.path.exists(os.path.join(server.data, "github.json"))
    gh.revoke()
    api.ok("/api/pages/publish")
    p = wait_for(lambda: (x := st(api))["state"] == "off" and x, 5, "выхода после отзыва доступа")
    assert "отозвал доступ" in p["error"] and not p["login"]
    assert not os.path.exists(os.path.join(server.data, "github.json"))

    api.ok("/api/pages/login")
    wait_for(lambda: st(api)["state"] == "device", 5, "кода входа")
    gh.authorize(ok=False)
    p = wait_for(lambda: (x := st(api))["state"] == "off" and x["error"] and x, 5, "отказа во входе")
    assert p["error"] == "Вход отменён на GitHub." and not p["login"]


def test_ttsvoices_prefers_pages():
    """!ttsvoices: адрес GitHub Pages главнее туннеля; без Pages — адрес туннеля."""
    told = []

    class Twitch:
        state = "ok"

        async def tell(self, who, text, mid):
            told.append(text)

    async def ask(pages_url, tunnel_url):
        engine = types.SimpleNamespace(pages=types.SimpleNamespace(url=pages_url),
                                       tunnel=types.SimpleNamespace(url=tunnel_url), twitch=Twitch(), link_told=0.0)
        commands.voices_link(engine, dict(chatter_user_login="viewer"))
        await asyncio.sleep(0.01)

    asyncio.run(ask("https://tester.github.io/tts-voices/", "https://x.trycloudflare.com/"))
    asyncio.run(ask("", "https://x.trycloudflare.com/"))
    assert [t.rsplit(" ", 1)[1] for t in told] == ["https://tester.github.io/tts-voices/", "https://x.trycloudflare.com/"]


def test_git_sha_matches_git():
    """sha блоба считается как у git: по нему публикация видит файлы, которые уже лежат на GitHub."""
    for data in (b"", b"hello\n", os.urandom(5000)):
        out = subprocess.run(["git", "hash-object", "--stdin"], input=data, capture_output=True, check=True)
        assert pages_mod.git_sha(data) == out.stdout.decode().strip()
