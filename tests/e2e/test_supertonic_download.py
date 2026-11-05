"""Загрузка Supertonic 3 по кнопке (engines/supertonic/download.py, install.py, POST /api/supertonic/*) без сети и без
настоящей модели: stream-tts отдельным процессом со встроенным поддельным supertonic-server (пакет-заглушка), файлы —
с локального HTTP-сервера по своему закреплению (STREAM_TTS_TEST_SUPERTONIC). Проверяются докачка после обрыва,
неверный sha256, готовые голоса после загрузки (страница голосов, примеры), запуск ждущего сервера, удаление модели с
голосами; закрепление настоящей модели в downloads.json; запуск сервера программой для Windows (StreamTTS.exe
--supertonic) и в образе Docker — find_server и командная строка, ключ --supertonic — через StreamTTS.py."""
import asyncio, hashlib, json, os, random, subprocess, sys

import pytest
from aiohttp import web

from conftest import FAST_ENV, Ctx
from fake_cosyvoice import fake_python
from harness import ROOT, STREAM_TTS, StreamTTS, wait_for

sys.path.insert(0, STREAM_TTS)
from streamtts import paths  # noqa: E402
from streamtts.engines.supertonic import download, launch  # noqa: E402

STYLES = [f"{g}{i}" for g in "MF" for i in range(1, 6)]
NAMES = sorted(download.VOICES.values())


class Files:
    """HTTP-сервер файлов модели с Range. cut[путь] — оборвать первый ответ на файл после стольких байт, bad — пути,
    которые отдаются испорченными; requests — (путь, Range)."""

    def __init__(self, files):
        self.files, self.cut, self.bad, self.requests = files, {}, set(), []

    async def start(self):
        app = web.Application()
        app.router.add_get("/{path:.+}", self.get)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self

    async def get(self, request):
        path, rng = request.match_info["path"], request.headers.get("Range")
        self.requests.append((path, rng))
        data = self.files[path]
        if path in self.bad:
            data = bytes(b ^ 0xFF for b in data)
        a = int(rng.removeprefix("bytes=").split("-")[0]) if rng else 0
        body = data[a:]
        resp = web.StreamResponse(status=206 if rng else 200, headers={"Content-Length": str(len(body))})
        if rng:
            resp.headers["Content-Range"] = f"bytes {a}-{len(data) - 1}/{len(data)}"
        await resp.prepare(request)
        cut = self.cut.pop(path, 0)
        if cut and len(body) > cut:
            await resp.write(body[:cut])
            request.transport.close()   # обрыв посреди файла
            return resp
        await resp.write(body)
        return resp

    async def close(self):
        await self.runner.cleanup()


def model_files():
    """Файлы модели в раскладке Supertone/supertonic-3: «веса» — случайные байты, стили — JSON как у модели."""
    rnd = random.Random(3)
    files = {f"onnx/{n}.onnx": rnd.randbytes(k) for n, k in (("duration_predictor", 20_000), ("text_encoder", 50_000),
                                                             ("vector_estimator", 400_000), ("vocoder", 120_000))}
    files["onnx/tts.json"] = b'{"ae": {"sample_rate": 44100}}'
    files["onnx/unicode_indexer.json"] = b"[0, 1, 2]"
    for s in STYLES:
        style = {k: {"dims": [1, 2, 2], "data": [[[rnd.random(), 0.1], [0.2, 0.3]]]} for k in ("style_ttl", "style_dp")}
        files[f"voice_styles/{s}.json"] = json.dumps(dict(style, name=s)).encode()
    files["LICENSE"] = b"BigScience OpenRAIL-M (test)"
    return files


@pytest.fixture(scope="module")
def st(loop, tmp_path_factory):
    """stream-tts с моделью Supertonic 3 без файлов модели и закреплением загрузки на локальный сервер."""
    wd = tmp_path_factory.mktemp("supertonic-download")
    files = model_files()
    srv = loop.run(Files(files).start())
    pin = wd / "pin.json"
    pin.write_text(json.dumps(dict(url=srv.url + "/", license=srv.url + "/LICENSE", files=[
        dict(path=p, size=len(d), sha256=hashlib.sha256(d).hexdigest()) for p, d in files.items()])), encoding="utf-8")
    python = fake_python(str(wd / "fake"), "supertonic", "supertonic")
    (wd / "data").mkdir()
    (wd / "data" / "settings.json").write_text('{"engine": "supertonic"}', encoding="utf-8")
    # голос пользователя в папке набора: удаление модели его не трогает
    (wd / "voices" / "supertonic").mkdir(parents=True)
    (wd / "voices" / "supertonic" / "mine.supertonic").write_bytes(files["voice_styles/M1.json"])
    server = StreamTTS(str(wd), None, dict(FAST_ENV, SUPERTONIC_SERVER=python,
                                           STREAM_TTS_TEST_SUPERTONIC=str(pin))).start(synth=False)
    ctx = Ctx(loop, server, None)
    ctx.srv, ctx.files, ctx.wd = srv, files, wd
    yield ctx
    ctx.close()
    server.stop()
    loop.run(srv.close())


def setup(api):
    return api.state()["setup"]


def finished(api, what):
    return wait_for(lambda: (lambda d: d["state"] != "running" and d)(setup(api)["download"]), 30, what)


def test_download_resume_bad_sha_voices_delete(st):
    """Без модели сервер ждёт с причиной, карточка предлагает загрузку и лицензию. Загрузка: обрыв посреди файла —
    докачка с места обрыва; неверный sha256 — ошибка, файл не остаётся, «Скачать» продолжает с него (скачанное не
    качается заново). После загрузки — 10 готовых голосов набора supertonic, сервер запускается сам, на странице
    голосов — группа «Supertonic 3» с примерами. Пропал голос — «Поставить голоса» возвращает его без загрузки.
    Удаление — модель и готовые голоса, голос пользователя остаётся, сервер снова ждёт модель."""
    api, srv, files, wd = st.api, st.srv, st.files, st.wd
    models, voices = wd / "models" / "supertonic", wd / "voices" / "supertonic"
    c = wait_for(lambda: (lambda c: c["runtime"]["state"] == "waiting" and c)(api.state()["cosy"]), 30,
                 f"ожидания модели\n{st.server.tail()}")
    assert c["engine"] == "supertonic"
    assert c["runtime"]["error"] == "нет модели Supertonic 3: скачайте её на вкладке «Сервер»"
    s = setup(api)["supertonic"]
    total = sum(len(d) for d in files.values())
    assert (s["offer"], s["model"], s["installed"], s["voices"], s["voices_total"], s["size"]) == \
        (True, False, False, 0, 10, total)
    assert s["license"] == srv.url + "/LICENSE" and s["names"] == NAMES and s["model_dir"] == str(models)

    srv.cut["onnx/vector_estimator.onnx"] = 150_000
    srv.bad.add("onnx/vocoder.onnx")
    api.ok("/api/supertonic/download")
    d = finished(api, "загрузки с испорченным файлом")
    assert d["state"] == "error" and d["package"] == "supertonic"
    assert d["error"] == "onnx/vocoder.onnx: контрольная сумма не совпала, файл удалён — скачайте ещё раз", d
    ve = [r for p, r in srv.requests if p == "onnx/vector_estimator.onnx"]
    assert ve == [None, "bytes=150000-"], "после обрыва — докачка с места обрыва"
    assert (models / "onnx" / "vector_estimator.onnx").read_bytes() == files["onnx/vector_estimator.onnx"]
    assert not (models / "onnx" / "vocoder.onnx").exists() and not (models / "onnx" / "vocoder.onnx.part").exists()
    assert not setup(api)["supertonic"]["model"] and api.state()["cosy"]["runtime"]["state"] == "waiting"

    srv.bad.clear()
    srv.requests.clear()
    api.ok("/api/supertonic/download")
    d = finished(api, "загрузки")
    assert d["state"] == "done", d
    got = [p for p, _ in srv.requests]
    assert got == list(files)[3:], "скачанные файлы качались заново"
    assert all((models / p).read_bytes() == data for p, data in files.items())
    for style, name in download.VOICES.items():
        assert (voices / f"{name}.supertonic").read_bytes() == files[f"voice_styles/{style}.json"]
    manifest = json.loads((voices / "manifest.json").read_text(encoding="utf-8"))
    assert sorted(manifest) == NAMES and manifest["anton"]["title"] == "Supertonic 3 M1"
    s = setup(api)["supertonic"]
    assert (s["model"], s["installed"], s["voices"], s["size"]) == (True, True, 10, 0)
    r = api.post("/api/supertonic/download").json()
    assert not r["ok"] and r["error"].endswith("Supertonic 3 уже скачан"), r

    # ждущий сервер запустился сам, голоса — готовые и голос пользователя
    wait_for(lambda: (lambda c: c["ok"] and c["runtime"]["state"] == "running")(api.state()["cosy"]), 30,
             f"запуска сервера после загрузки\n{st.server.tail()}")
    wait_for(lambda: sorted(v["name"] for v in api.state()["voices"]) == sorted(NAMES + ["mine"]), 30,
             "голосов после загрузки")
    st.idle(60)   # примеры всех голосов
    page = api.get("/voices/").text
    # группа — по manifest.json набора: голос пользователя без записи в нём — в «Других»
    assert "<h2>Supertonic 3 <span>10</span></h2>" in page, "готовые голоса не в группе Supertonic 3"
    assert all(f'data-name="{n}"' in page for n in NAMES)
    assert api.get("/voices/demos/anna.mp3").status == 200

    # голос удалили — «Поставить голоса»: без загрузки, только недостающие
    (voices / "rita.supertonic").unlink()
    s = setup(api)["supertonic"]
    assert (s["model"], s["installed"], s["voices"]) == (True, False, 9)
    srv.requests.clear()
    api.ok("/api/supertonic/download")
    assert finished(api, "голосов")["state"] == "done" and not srv.requests
    assert (voices / "rita.supertonic").is_file() and setup(api)["supertonic"]["installed"]

    pid = api.state()["cosy"]["runtime"]["pid"]
    api.ok("/api/supertonic/delete")
    assert not models.exists()
    assert sorted(os.listdir(voices)) == ["mine.supertonic"], "голос пользователя удалён или готовые остались"
    c = wait_for(lambda: (lambda c: c["runtime"]["state"] == "waiting" and c)(api.state()["cosy"]), 30,
                 "ожидания модели после удаления")
    assert c["runtime"]["pid"] != pid and "нет модели Supertonic 3" in c["runtime"]["error"]
    s = setup(api)["supertonic"]
    assert (s["model"], s["installed"], s["voices"], s["size"]) == (False, False, 0, total)


def test_pinned_model():
    """downloads.json: Supertone/supertonic-3 закреплённой ревизии — файлы, без которых сервер не запустится, все 10
    стилей и LICENSE (условия OpenRAIL-M передаются с моделью), у каждого размер и sha256; готовые голоса — из этих
    стилей, имена — по правилу voices.NAME и без повторов."""
    with open(os.path.join(STREAM_TTS, "streamtts", "downloads.json"), encoding="utf-8") as f:
        s = json.load(f)["supertonic"]
    rev = s["revision"]
    assert len(rev) == 40 and s["url"] == f"https://huggingface.co/Supertone/supertonic-3/resolve/{rev}"
    assert s["license"] == f"https://huggingface.co/Supertone/supertonic-3/blob/{rev}/LICENSE"
    have = [f["path"] for f in s["files"]]
    assert set(have) == {*launch.MODEL_FILES, *(f"voice_styles/{x}.json" for x in STYLES), "LICENSE"}
    assert len(have) == len(set(have)) and all(f["size"] > 0 and len(f["sha256"]) == 64 for f in s["files"])
    assert sorted(download.VOICES) == sorted(STYLES) and len(set(download.VOICES.values())) == 10
    from streamtts.voices import NAME
    assert all(NAME.fullmatch(n) for n in download.VOICES.values())


def test_find_server_program_and_docker(tmp_path, monkeypatch):
    """Программа для Windows запускает supertonic-server сама: StreamTTS.exe --supertonic, без PYTHONPATH. В образе
    Docker — Python stream-tts с кодом сервера и espeech из рантайма (Dockerfile) в PYTHONPATH."""
    monkeypatch.delenv("SUPERTONIC_SERVER", raising=False)
    models, voices = tmp_path / "models", tmp_path / "voices"
    monkeypatch.setattr(paths, "MODELS", str(models))
    monkeypatch.setattr(paths, "VOICES", str(voices))
    for f in launch.MODEL_FILES:
        (models / "supertonic" / f).parent.mkdir(parents=True, exist_ok=True)
        (models / "supertonic" / f).write_text("{}")
    tail = ["--models", str(models / "supertonic"), "--voices", str(voices), "--host", "127.0.0.1", "--port", "5"]

    # собранная программа: исходников рядом нет
    exe = tmp_path / "StreamTTS" / "StreamTTS.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    exe = str(exe)
    monkeypatch.setattr(paths, "FROZEN", True)
    monkeypatch.setattr(paths, "FROM_SOURCE", False)
    monkeypatch.setattr(paths, "PROGRAM", exe)   # у Nuitka sys.executable — несуществующий python.exe рядом
    monkeypatch.setattr(sys, "executable", str(tmp_path / "StreamTTS" / "python.exe"))
    assert launch.find_server() == exe
    L = launch.Launcher(exe)
    assert L.frozen and not L.bundled
    assert asyncio.run(L.args("127.0.0.1", 5)) == [exe, "--supertonic", *tail]
    monkeypatch.setenv("PYTHONPATH", "x")
    assert "PYTHONPATH" not in L.env()

    # образ Docker: код в paths.RUNTIME (/opt/cosyvoice)
    py = tmp_path / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_bytes(b"")
    py = str(py)
    monkeypatch.setattr(paths, "FROZEN", False)
    monkeypatch.setattr(sys, "executable", py)
    rt = tmp_path / "runtime"
    monkeypatch.setattr(launch, "CODE", str(rt / "supertonic"))
    monkeypatch.setattr(launch, "ESPEECH_CODE", str(rt / "espeech"))
    assert launch.find_server() is None, "без кода сервера модели в выборе быть не должно"
    for f in ("supertonic/supertonic/server.py", "espeech/espeech/normalize.py"):
        (rt / f).parent.mkdir(parents=True, exist_ok=True)
        (rt / f).write_text("")
    assert launch.find_server() == py
    L = launch.Launcher(py)
    assert L.bundled and not L.frozen
    assert asyncio.run(L.args("127.0.0.1", 5)) == [py, "-m", "supertonic", *tail]
    assert L.env()["PYTHONPATH"] == os.pathsep.join((str(rt / "supertonic"), str(rt / "espeech")))


def test_program_entry_runs_server(tmp_path):
    """StreamTTS.py (главный модуль сборки) с --supertonic — supertonic-server проекта, а не stream-tts: без модели он
    выходит с кодом 2 и списком недостающих файлов, не трогая данные программы."""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((STREAM_TTS, ROOT)), STREAM_TTS_DATA=str(tmp_path / "data"))
    r = subprocess.run([sys.executable, os.path.join(ROOT, "packaging", "windows", "StreamTTS.py"), "--supertonic",
                        "--models", str(tmp_path / "none"), "--port", "1"], env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 2 and "supertonic-server: в папке модели" in r.stderr, r.stderr
    assert not (tmp_path / "data").exists()


def test_program_has_espeech_modules():
    """supertonic-server в программе для Windows: модули espeech/, которые он импортирует (с их импортами), — в
    ESPEECH_MODULES build-exe.py. Новый импорт из espeech без этого списка ломает только собранную программу."""
    import ast

    def espeech_imports(path, mod=None):
        out = set()
        for n in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
            if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("espeech."):
                out.add(n.module.split(".")[1])
            elif isinstance(n, ast.ImportFrom) and mod and n.level == 1 and n.module:   # внутри espeech: from .x
                out.add(n.module)
            elif isinstance(n, ast.Import):
                out |= {a.name.split(".")[1] for a in n.names if a.name.startswith("espeech.")}
        return out

    need, todo = set(), set()
    for f in os.listdir(os.path.join(ROOT, "supertonic")):
        if f.endswith(".py"):
            todo |= espeech_imports(os.path.join(ROOT, "supertonic", f))
    while todo:
        m = todo.pop()
        if m not in need:
            need.add(m)
            todo |= espeech_imports(os.path.join(ROOT, "espeech", m + ".py"), m)
    src = open(os.path.join(ROOT, "packaging", "windows", "build-exe.py"), encoding="utf-8").read()
    listed = set(ast.literal_eval(src.split("ESPEECH_MODULES = ", 1)[1].split("\n", 1)[0]))
    assert need <= listed, f"нет в ESPEECH_MODULES build-exe.py: {sorted(need - listed)}"
