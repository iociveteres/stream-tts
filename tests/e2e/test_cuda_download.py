"""CUDA по кнопке (install.py; программа для Windows и образ Docker) без сети и без настоящих файлов: распаковка zip
(Windows, файлы в корне) и tar.gz (Linux, файлы в папке сборки, рядом ссылки), загрузка с локального HTTP-сервера в
отдельную папку (в Docker — том /cuda), когда кнопка предлагается и как сервер подключает CUDA из этой папки
(GGML_BACKEND_PATH). Закрепление: архивы для Linux — из того же релиза llama.cpp и CUDA, что в Dockerfile, образ
один (docker-compose.gpu.yml не собирает свой). Что настоящая CUDA из загрузки работает с ядром GGML образа,
проверяет запуск контейнера с --gpus (docs/stream-tts-deploy.md, «Docker»)."""
import asyncio, contextlib, hashlib, io, json, os, re, sys, tarfile, zipfile

import aiohttp
import pytest

from harness import ROOT, STREAM_TTS
from test_espeech_download import Files

sys.path.insert(0, STREAM_TTS)
from streamtts import downloads, install, paths  # noqa: E402
from streamtts.engines.cosyvoice import launch  # noqa: E402

PINS = json.load(open(os.path.join(STREAM_TTS, "streamtts", "downloads.json"), encoding="utf-8"))["cuda"]


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def make_tar(path, top, files, extra=()):
    """tar.gz как у релиза llama.cpp: файлы в папке top, рядом ненужные файлы и ссылки."""
    with tarfile.open(path, "w:gz") as tar:
        def add(name, data):
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name in extra:
            add(name, b"other " + name.encode())
            link = tarfile.TarInfo(f"{top}/{name}.0")
            link.type, link.linkname = tarfile.SYMTYPE, name
            tar.addfile(link)
        for name, data in files.items():
            add(name, data)


def test_pins():
    """Обе системы: файлы архивов = files, на диске — часть архивов, версия одна; Linux — релиз llama.cpp и CUDA
    из Dockerfile (ядро GGML образа той же версии), tar.gz."""
    for system, pin in PINS.items():
        assert sorted(f for a in pin["archives"] for f in a["files"]) == sorted(pin["files"]), system
        assert 0 < pin["unpacked"] < sum(a["unpacked"] for a in pin["archives"]), system   # из архивов — часть
        assert all(re.fullmatch(r"[0-9a-f]{64}", a["sha256"]) and a["size"] > 0 for a in pin["archives"]), system
    assert PINS["windows"]["version"] == PINS["linux"]["version"]
    df = read("Dockerfile")
    tag = re.search(r"^ARG LLAMACPP_TAG=(\S+)", df, re.M).group(1)
    cuda = re.search(r"^ARG CUDA_VERSION=(\S+)", df, re.M).group(1)
    assert PINS["linux"]["version"] == f"{cuda}-{tag}"
    for a in PINS["linux"]["archives"]:
        assert f"/download/{tag}/" in a["url"] and f"-ubuntu-cuda-{cuda}-x64.tar.gz" in a["url"], a["url"]
    assert launch.CUDA_LIB in PINS["windows" if os.name == "nt" else "linux"]["files"]
    assert downloads.CUDA == PINS["windows" if os.name == "nt" else "linux"]


def test_docker_one_image():
    """Образ stream-tts один: CUDA — в томе /cuda (STREAM_TTS_CUDA), docker-compose.gpu.yml только даёт видеокарту."""
    assert re.search(r"^\s*STREAM_TTS_CUDA=/cuda\b", read("Dockerfile"), re.M)
    assert re.search(r"^\s*- \./cuda:/cuda\s", read("docker-compose.yml"), re.M)
    gpu = read("docker-compose.gpu.yml")
    st = gpu[gpu.index("  stream-tts:"):]
    assert "build:" not in st and "image:" not in st, "docker-compose.gpu.yml собирает свой образ stream-tts"
    assert "capabilities: [gpu]" in st


def test_extract(tmp_path):
    """zip — файлы из корня, tar.gz — по имени из папки сборки (ссылки и чужие файлы не берутся); архивы удаляются,
    версия — в cuda-version.txt; чего-то нет — ошибка."""
    with zipfile.ZipFile(tmp_path / "a.zip", "w") as z:
        z.writestr("ggml-cuda.dll", b"dll")
        z.writestr("llama-cli.exe", b"exe")
    make_tar(tmp_path / "b.tar.gz", "cudart-llama", {"libcudart.so.13": b"rt", "libcublas.so.13": b"blas"},
             extra=("libggml-base.so",))
    archives = [("", "a.zip", 0, "", ["ggml-cuda.dll"]),
                ("", "b.tar.gz", 0, "", ["libcudart.so.13", "libcublas.so.13"])]
    install._extract_cuda(archives, str(tmp_path))
    assert sorted(os.listdir(tmp_path)) == sorted(["ggml-cuda.dll", "libcudart.so.13", "libcublas.so.13",
                                                   install.CUDA_VERSION_FILE])
    assert (tmp_path / "libcublas.so.13").read_bytes() == b"blas"
    assert (tmp_path / install.CUDA_VERSION_FILE).read_text() == downloads.CUDA["version"]

    make_tar(tmp_path / "c.tar.gz", "x", {"libcudart.so.13": b"rt"})
    with pytest.raises(RuntimeError, match="libcublasLt.so.13"):
        install._extract_cuda([("", "c.tar.gz", 0, "", ["libcudart.so.13", "libcublasLt.so.13"])], str(tmp_path))


def test_server_env(tmp_path):
    """CUDA рядом с сервером GGML находит сама; скачанную в отдельную папку подключает GGML_BACKEND_PATH, а её
    библиотеки — путь поиска (LD_LIBRARY_PATH, на Windows PATH)."""
    server, cuda = tmp_path / "runtime", tmp_path / "cuda"
    server.mkdir(), cuda.mkdir()
    L = launch.Launcher(str(server / ("cosyvoice-server" + paths.EXE)))
    L.cuda = str(cuda)
    var = "PATH" if os.name == "nt" else "LD_LIBRARY_PATH"
    split = lambda e: e.get(var, "").split(os.pathsep)

    e = L.env()
    assert L.cuda_lib() == "" and "GGML_BACKEND_PATH" not in e and str(cuda) not in split(e)
    assert e["GGML_CUDA_DISABLE_GRAPHS"] == "1"

    (cuda / launch.CUDA_LIB).write_bytes(b"")
    e = L.env()
    assert e["GGML_BACKEND_PATH"] == str(cuda / launch.CUDA_LIB) == L.cuda_lib()
    first = [str(cuda)] if os.name == "nt" else [str(server), str(cuda)]
    assert split(e)[:len(first)] == first

    (server / launch.CUDA_LIB).write_bytes(b"")   # образ, собранный с GPU=cuda: CUDA уже у сервера
    e = L.env()
    assert L.cuda_lib() == str(server / launch.CUDA_LIB) and "GGML_BACKEND_PATH" not in e


class Runtime:
    """То, что Installer берёт у сервера при распаковке CUDA: остановить на время и вернуть."""

    def __init__(self):
        self.pauses = 0

    @contextlib.asynccontextmanager
    async def paused(self):
        self.pauses += 1
        yield


def installer(tmp_path):
    L = launch.Launcher(str(tmp_path / "runtime" / ("cosyvoice-server" + paths.EXE)))
    L.cuda = str(tmp_path / "cuda")   # как STREAM_TTS_CUDA=/cuda в Docker
    return install.Installer({}, Runtime(), lambda: None, {"cosyvoice": L}), L


def test_offer(tmp_path, monkeypatch):
    """Кнопка — только при драйвере NVIDIA и без CUDA; папка и размеры — для подписи в админке."""
    inst, L = installer(tmp_path)
    monkeypatch.setattr(paths, "nvidia", lambda: False)
    assert inst.cuda_state()["offer"] is False
    monkeypatch.setattr(paths, "nvidia", lambda: True)
    st = inst.cuda_state()
    assert st["offer"] is True and st["installed"] is False and st["dir"] == L.cuda
    assert st["size"] == sum(a["size"] for a in downloads.CUDA["archives"]) and st["unpacked"] == downloads.CUDA["unpacked"]
    os.makedirs(L.cuda)
    with open(os.path.join(L.cuda, launch.CUDA_LIB), "wb"):
        pass
    with open(os.path.join(L.cuda, install.CUDA_VERSION_FILE), "w") as f:
        f.write("13.4-test")
    st = inst.cuda_state()
    assert st["offer"] is False and st["installed"] is True and st["version"] == "13.4-test"
    with pytest.raises(ValueError):
        inst.enqueue("cuda")


def test_download(tmp_path, monkeypatch):
    """Загрузка в отдельную папку: архивы с проверкой sha256, распаковка при остановленном сервере, после — сервер
    заново ищет устройства, архивов не остаётся."""
    inst, L = installer(tmp_path)
    monkeypatch.setattr(paths, "nvidia", lambda: True)
    want = {f: f"{f} bytes".encode() * 1000 for f in downloads.CUDA["files"]}
    blobs = {}
    for i, a in enumerate(downloads.CUDA["archives"]):
        name = a["url"].rsplit("/", 1)[-1]
        part = {f: want[f] for f in a["files"]}
        if name.endswith(".zip"):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for f, data in part.items():
                    z.writestr(f, data)
            blobs[name] = buf.getvalue()
        else:
            make_tar(tmp_path / name, f"build-{i}", part, extra=("libggml.so",))
            blobs[name] = (tmp_path / name).read_bytes()

    async def go():
        files = await Files(blobs).start()
        monkeypatch.setattr(install, "cuda_archives", lambda: [
            (f"{files.url}/{n}", n, len(b), hashlib.sha256(b).hexdigest(), a["files"])
            for (n, b), a in zip(blobs.items(), downloads.CUDA["archives"])])
        try:
            L.backends = [("CPU", False)]
            inst.enqueue("cuda")
            await asyncio.wait_for(inst.task, 30)
        finally:
            await files.close()

    asyncio.run(go())
    assert inst.dl["state"] == "done", inst.dl
    assert inst.runtime.pauses == 1 and L.backends is None
    got = {f: open(os.path.join(L.cuda, f), "rb").read() for f in downloads.CUDA["files"]}
    assert got == want
    assert sorted(os.listdir(L.cuda)) == sorted([*want, install.CUDA_VERSION_FILE])
    assert not os.path.exists(os.path.join(L.dir))   # к серверу ничего не легло
    assert inst.cuda_state()["installed"] is True and L.env().get("GGML_BACKEND_PATH") == L.cuda_lib()
