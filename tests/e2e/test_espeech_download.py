"""Загрузка ESpeech (engines/espeech/download.py, partzip.py; программа для Windows и образ Docker) без сети и без
настоящих файлов: часть zip диапазонами с локального HTTP-сервера (выбор файлов, докачка после обрыва, подмена хвоста
или файла), распаковка колёс и Python из tar.gz, состояние рантайма, закрепление для обеих систем. Сам рантайм с torch
и сборку модели проверяет запуск программы и контейнера (docs/espeech.md, «Поставка»): они качают гигабайты."""
import asyncio, hashlib, io, json, os, random, sys, tarfile, zipfile

import aiohttp
import pytest
from aiohttp import web

from harness import STREAM_TTS

sys.path.insert(0, STREAM_TTS)
from streamtts import partzip, paths  # noqa: E402
from streamtts.engines.espeech import download, launch  # noqa: E402


class Files:
    """HTTP-сервер файлов с Range; cut — оборвать первый ответ после стольких байт."""

    def __init__(self, files):
        self.files, self.cut, self.served, self.requests = files, 0, 0, []

    async def start(self):
        app = web.Application()
        app.router.add_get("/{name}", self.get)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self

    async def get(self, request):
        data = self.files[request.match_info["name"]]
        rng = request.headers.get("Range")
        self.requests.append(rng)
        if not rng:
            return web.Response(body=data)
        a, b = rng.removeprefix("bytes=").split("-")
        a, b = int(a), int(b) if b else len(data) - 1
        body = data[a:b + 1]
        resp = web.StreamResponse(status=206, headers={"Content-Range": f"bytes {a}-{b}/{len(data)}",
                                                       "Content-Length": str(len(body))})
        await resp.prepare(request)
        if self.cut and len(body) > self.cut:
            await resp.write(body[:self.cut])
            self.served += self.cut
            self.cut = 0
            request.transport.close()   # обрыв посреди диапазона
            return resp
        await resp.write(body)
        self.served += len(body)
        return resp

    async def close(self):
        await self.runner.cleanup()


class Inst:
    """То, что Steps берёт у Installer: прогресс и _push."""

    def __init__(self):
        self.dl = dict(done=0, total=0, speed=0.0, file="")

    def _push(self, force=False):
        pass


def make_wheel(tmp_path):
    """Архив как колесо torch: Python-код (сжат), большие «DLL» (без сжатия), заголовки и .lib — их не берём."""
    rnd = random.Random(1)
    files = {}
    for i in range(30):
        files[f"torch/mod{i:02d}.py"] = f"x = {i}\n".encode() * rnd.randint(10, 2000)
    for name in ("torch_cpu.dll", "torch_cuda.dll", "cudnn64_9.dll", "cublas64_13.dll", "c10.dll"):
        files[f"torch/lib/{name}"] = rnd.randbytes(rnd.randint(300_000, 900_000))
    for i in range(40):
        files[f"torch/include/h{i}.h"] = rnd.randbytes(20_000)
    files["torch/lib/torch.lib"] = rnd.randbytes(500_000)
    files["torch-1.0.dist-info/RECORD"] = b"..."
    path = tmp_path / "torch.whl"
    with zipfile.ZipFile(path, "w") as z:
        for k, v in files.items():
            z.writestr(k, v, zipfile.ZIP_STORED if k.endswith(".dll") else zipfile.ZIP_DEFLATED)
    return path, files


EXCLUDE = ["torch/include/*", "torch/lib/*.lib", "torch/lib/cudnn*", "torch/lib/cublas*"]


def pin(path, url, **sel):
    """Описание части архива так, как его пишет pin-espeech.py."""
    with zipfile.ZipFile(path) as z:
        tail = z.start_dir
    data = path.read_bytes()
    spec = dict(url=url, size=len(data), tail=tail, tail_sha256=hashlib.sha256(data[tail:]).hexdigest(), **sel)
    spec["digest"] = partzip.digest_of(path, spec)
    return spec


def want(files):
    return {k: v for k, v in files.items() if not k.startswith("torch/include/") and not k.endswith(".lib")
            and "/cudnn" not in k and "/cublas" not in k}


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


def fetch_and_extract(loop, srv, spec, part, out):
    inst = Inst()

    async def go():
        async with aiohttp.ClientSession() as session:
            return await download.Steps(inst, session).partial(spec, str(part), "torch.whl")
    ranges = loop.run_until_complete(go())
    got = {}

    def sink(info, src):
        got[info.filename] = src.read()
    partzip.extract(str(part), spec, ranges, sink)
    return ranges, got, inst


@pytest.fixture
def wheel_server(loop, tmp_path):
    path, files = make_wheel(tmp_path)
    srv = loop.run_until_complete(Files({"torch.whl": path.read_bytes()}).start())
    yield srv, path, files
    loop.run_until_complete(srv.close())


def test_partial_zip(loop, tmp_path, wheel_server):
    """Только нужные файлы, диапазонами: заголовки, .lib и заменённые заглушками DLL не качаются; содержимое — как в
    архиве; размер .part — хвост и диапазоны."""
    srv, path, files = wheel_server
    spec = pin(path, srv.url + "/torch.whl", exclude=EXCLUDE)
    part = tmp_path / "torch.part"
    ranges, got, inst = fetch_and_extract(loop, srv, spec, part, tmp_path)
    assert got == want(files)
    assert part.stat().st_size == partzip.download_size(spec, ranges) == inst.dl["done"]
    assert srv.served == inst.dl["done"] < 0.6 * path.stat().st_size, "качалось лишнее"
    assert len(ranges) < len(want(files)), "соседние файлы не склеены в диапазоны"


def test_partial_zip_resume(loop, tmp_path, wheel_server):
    """Обрыв посреди диапазона и повторный запуск: докачка с места обрыва, ничего не качается дважды."""
    srv, path, files = wheel_server
    spec = pin(path, srv.url + "/torch.whl", exclude=EXCLUDE)
    part = tmp_path / "torch.part"
    srv.cut = 400_000
    ranges, got, _ = fetch_and_extract(loop, srv, spec, part, tmp_path)   # обрыв — повтор внутри Steps
    assert got == want(files)
    assert srv.served == part.stat().st_size, "после обрыва качалось заново"
    served = srv.served
    ranges2, got2, _ = fetch_and_extract(loop, srv, spec, part, tmp_path)   # всё скачано: только проверка хвоста
    assert got2 == got and ranges2 == ranges and srv.served == served


def test_partial_zip_members(loop, tmp_path, wheel_server):
    """Выбор по списку (как EMA-копия чекпойнта): только эти файлы."""
    srv, path, files = wheel_server
    names = ["torch/lib/torch_cuda.dll", "torch/mod03.py"]
    spec = pin(path, srv.url + "/torch.whl", members=names)
    _, got, _ = fetch_and_extract(loop, srv, spec, tmp_path / "m.part", tmp_path)
    assert got == {k: files[k] for k in names}


def test_partial_zip_tampered(loop, tmp_path, wheel_server):
    """Подмена: другой хвост — ошибка и .part удалён; другой файл при том же хвосте — ошибка распаковки."""
    srv, path, files = wheel_server
    spec = pin(path, srv.url + "/torch.whl", exclude=EXCLUDE)
    bad_tail = dict(spec, tail_sha256="0" * 64)
    part = tmp_path / "t.part"
    with pytest.raises(RuntimeError, match="контрольная сумма"):
        fetch_and_extract(loop, srv, bad_tail, part, tmp_path)
    assert not part.exists()

    data = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("torch/lib/torch_cpu.dll")   # без сжатия: данные — сразу за локальным заголовком
    at = info.header_offset + 30 + len(info.filename) + 100
    data[at] ^= 0xFF
    srv.files["torch.whl"] = bytes(data)
    with pytest.raises(Exception, match="CRC|не совпали"):
        fetch_and_extract(loop, srv, spec, tmp_path / "t2.part", tmp_path)


def test_unpack_wheel(tmp_path):
    """Колесо — распаковкой: .data/purelib — в site-packages, скрипты не нужны; путь наружу — ошибка."""
    w = tmp_path / "pkg.whl"
    with zipfile.ZipFile(w, "w") as z:
        z.writestr("pkg/__init__.py", "x = 1")
        z.writestr("pkg-1.0.dist-info/METADATA", "Name: pkg")
        z.writestr("pkg-1.0.data/purelib/extra.py", "y = 2")
        z.writestr("pkg-1.0.data/scripts/tool.exe", "MZ")
    site = tmp_path / "site"
    site.mkdir()
    download._unpack_wheel(str(w), str(site))
    found = sorted(os.path.relpath(os.path.join(r, f), site).replace(os.sep, "/")
                   for r, _, fs in os.walk(site) for f in fs)
    assert found == ["extra.py", "pkg-1.0.dist-info/METADATA", "pkg/__init__.py"]
    evil = tmp_path / "evil.whl"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../evil.py", "boom")
    with pytest.raises(RuntimeError, match="недопустимый путь"):
        download._unpack_wheel(str(evil), str(site))


def test_runtime_state(tmp_path, monkeypatch):
    """Рантайм: нет — missing, версия другого закрепления — outdated, своя — ok; модель — по файлам папки."""
    monkeypatch.setattr(launch, "RUNTIME", str(tmp_path / "rt"))
    monkeypatch.setattr(paths, "MODELS", str(tmp_path / "models"))
    assert download.runtime_state() == "missing"
    os.makedirs(os.path.dirname(launch.runtime_python()))
    with open(launch.runtime_python(), "wb") as f:
        f.write(b"MZ")
    (tmp_path / "rt" / "version.txt").write_text("old")
    assert download.runtime_state() == "outdated"
    (tmp_path / "rt" / "version.txt").write_text(download.runtime_version())
    assert download.runtime_state() == "ok"
    assert not download.model_ready()
    for f in launch.MODEL_FILES:
        p = tmp_path / "models" / "espeech" / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    assert download.model_ready()


def test_unpack_tar(tmp_path):
    """Python из tar.gz (python-build-standalone): без верхней папки python/ и без масок exclude, ссылки сохраняются;
    путь наружу — ошибка фильтра data."""
    def tgz(path, entries):
        with tarfile.open(path, "w:gz") as t:
            for name, data, link in entries:
                i = tarfile.TarInfo(name)
                if link:
                    i.type, i.linkname = tarfile.SYMTYPE, link
                    t.addfile(i)
                else:
                    i.size, i.mode = len(data), 0o755
                    t.addfile(i, io.BytesIO(data))
    good = tmp_path / "py.tar.gz"
    tgz(good, [("python/bin/python3.14", b"ELF", None), ("python/bin/python3", b"", "python3.14"),
               ("python/lib/python3.14/os.py", b"x = 1", None), ("python/share/terminfo/a", b"t", None),
               ("python/lib/python3.14/site-packages/pip/__init__.py", b"", None)])
    out = tmp_path / "rt"
    download._unpack_tar(str(good), str(out), ["python/share/*", "python/lib/python3.14/site-packages/*"])
    found = sorted(os.path.relpath(os.path.join(r, f), out).replace(os.sep, "/")
                   for r, _, fs in os.walk(out) for f in fs)
    # символическая ссылка — только не на Windows (там tarfile её пропускает, а tar.gz нужен только Linux)
    assert found == ["bin/python3"] * (os.name != "nt") + ["bin/python3.14", "lib/python3.14/os.py"]
    evil = tmp_path / "evil.tar.gz"
    tgz(evil, [("python/../../evil.py", b"boom", None)])
    with pytest.raises(tarfile.FilterError):
        download._unpack_tar(str(evil), str(tmp_path / "rt2"), [])


def test_pinned_downloads():
    """downloads.json: рантаймы обеих систем и модель закреплены целиком (pin-espeech.py), заглушки есть для всех
    библиотек из списков; настоящие библиотеки CUDA — те же, что ищет launch.py."""
    with open(os.path.join(STREAM_TTS, "streamtts", "downloads.json"), encoding="utf-8") as f:
        e = json.load(f)["espeech"]
    m, packaging = e["model"], os.path.join(os.path.dirname(STREAM_TTS), "packaging")
    assert sorted(e["runtime"]) == ["linux", "windows"]
    for system, r in e["runtime"].items():
        for item in [r["python"], *r["wheels"], *m["files"]]:
            assert item["url"].startswith("https://") and item["size"] > 0 and len(item["sha256"]) == 64
        for part in (r["torch"], m["checkpoint"]):
            assert 0 < part["tail"] < part["size"] and len(part["tail_sha256"]) == 64 and len(part["digest"]) == 64
            assert part["download"] < part["size"]
        names = [os.path.basename(w["url"]) for w in r["wheels"]]
        assert all(("win_amd64" in n) == (system == "windows") or n.endswith("-none-any.whl") for n in names), names
        for lib in r["stubs"] + r["cublas"]:
            sub = ["cublas"] if lib in r["cublas"] else []
            stub = (os.path.join(packaging, "windows", "stubs", lib[:-4] + ".def") if system == "windows" else
                    os.path.join(packaging, "linux", "stubs", *sub, lib + ".map"))
            assert os.path.isfile(stub), stub
    assert tuple(e["runtime"][download.SYSTEM]["cublas"]) == launch.CUBLAS
    assert e["runtime"]["linux"]["python"]["url"].endswith(".tar.gz") and e["runtime"]["linux"]["python"]["exclude"]
    assert all(f["path"].count("/") >= 2 for f in m["files"])   # <владелец>/<репозиторий>/<путь>
