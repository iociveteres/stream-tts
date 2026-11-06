"""Загрузки через админку (Installer): модель, CUDA, ESpeech, дополнительные пакеты; состояние загрузок и пакетов
для админки; установка наборов голосов с остановкой сервера (сами наборы — voicesets.py).
Установщик программы для Windows ничего не качает: модель, CUDA и ESpeech — кнопки на вкладке «Сервер» (при первом
запуске без модели админка открывается на ней), пакеты — в настройках.

Модель качается как в scripts/download-model.sh: Lourdle/Fun-CosyVoice3-0.5B-2512-GGUF с Hugging Face (ревизия
закреплена в downloads.json, model.url), квант на выбор (по умолчанию Q8_0), по желанию ONNX-фронтенд int8 (нужен
только для извлечения голоса из аудио, для озвучки не нужен; он же — дополнительный пакет из PACKAGES, качается и
удаляется из настроек админки). Другие пакеты
(htdemucs для очистки образца от музыки, separate.py) — из своих репозиториев Hugging Face. Что качается и откуда —
downloads.py, файл с докачкой и проверкой sha256 — fetch.py. Папка моделей — paths.MODELS.

CUDA (ggml-cuda и рантайм CUDA 13) — два архива из релиза llama.cpp той же сборки, что и ядро GGML (адреса,
размеры и sha256 — в downloads.json, cuda.windows — zip, cuda.linux — tar.gz для образа Docker; там же размеры квантов
модели). Если есть драйвер NVIDIA (paths.nvidia: nvcuda.dll, в контейнере — libcuda.so.1 от --gpus), а ggml-cuda
нет, вкладка «Сервер» предлагает скачать их: из архивов берутся только нужные файлы (files), они распаковываются при
остановленном сервере в папку сервера или в paths.CUDA (в Docker — том /cuda, образ один и без CUDA), версия
пишется в cuda-version.txt (по ней установщик при обновлении решает, сохранить ли CUDA или удалить вместе со старым
рантаймом), потом сервер перезапускается и заново ищет устройства.

ESpeech (рантайм с torch и модель) программа для Windows и образ Docker качают по кнопке на вкладке «Сервер» —
engines/espeech/download.py; CUDA, если её нет, а видеокарта NVIDIA есть, качается вместе с ним (torch берёт cuBLAS
оттуда). Supertonic 3 (модель и 10 готовых голосов) — тоже по кнопке, везде, где найден supertonic-server:
engines/supertonic/download.py; после загрузки ждущий модель сервер запускается.

Набор голосов из zip (voicesets.py) ставится и удаляется при остановленном сервере синтеза: на Windows
cosyvoice-server держит открытые .gguf; потом он запускается с новым списком голосов."""
import asyncio, contextlib, logging, os, shutil, tarfile, time, zipfile

import aiohttp

from . import fetch, paths, voicesets
from .downloads import (BASE, CUDA, CUDA_VERSION_FILE, DEFAULT_QUANT, PACKAGES, QUANTS, SAMPLE_PHRASE, SAMPLES,
                        cuda_archives, package_files)
from .engines import cosyvoice
from .engines.espeech import download as espeech_download, launch as espeech_launch
from .engines.supertonic import download as supertonic_download
from .fetch import CHUNK
from .i18n import tr

log = logging.getLogger("install")


model_name = cosyvoice.model_name


def installed_models():
    try:
        return sorted(f for f in os.listdir(paths.MODELS) if f.endswith(".gguf"))
    except OSError:
        return []


class Cancelled(Exception):
    pass


class Installer:
    def __init__(self, settings, runtime, changed, launchers=None):
        """launchers — запуски найденных движков {id: Launcher}: у каждого свои загрузки."""
        self.s, self.runtime, self.changed = settings, runtime, changed  # changed() — разбудить админку
        launchers = launchers or {}
        self.cosy = launchers.get("cosyvoice")   # запуск CosyVoice (engines/cosyvoice): модель, CUDA и фронтенд — его
        # запуск ESpeech: у программы для Windows и в Docker рантайм и модель качаются (download.py)
        self.espeech = launchers.get("espeech")
        # запуск Supertonic 3: модель и готовые голоса качаются везде, где он есть (engines/supertonic/download.py)
        self.supertonic = launchers.get("supertonic")
        self.task = None
        self.dl = dict(state="idle", quant="", package="", file="", done=0, total=0, speed=0.0, error="")
        self._last_push = 0.0

    # --- состояние для админки ---
    def public(self):
        quants = [dict(QUANTS[q], quant=q, file=model_name(q)) for q in QUANTS]
        samples = [(voice, tr(label)) for voice, label in SAMPLES]
        model = self.cosy.model if self.cosy else ""
        return dict(quants=quants, default_quant=DEFAULT_QUANT, samples=samples, sample_phrase=SAMPLE_PHRASE,
                    models=installed_models(),
                    model=os.path.basename(model) if model else self.s.get("model", ""),
                    models_dir=paths.MODELS, voices_dir=paths.VOICES, download=dict(self.dl), sets=voicesets.list_sets(),
                    packages=packages(), cuda=self.cuda_state(), espeech=self.espeech_state(),
                    supertonic=self.supertonic_state())

    # --- CUDA ---
    def cuda_state(self):
        """CUDA для вкладки «Сервер»: offer — предложить скачать (есть драйвер NVIDIA, а ggml-cuda нет)."""
        if not self.cosy:
            return dict(offer=False, installed=False, version="", size=0, unpacked=0, dir="")
        lib, d = self.cosy.cuda_lib(), self.cosy.cuda
        version = ""
        with contextlib.suppress(OSError):
            with open(os.path.join(os.path.dirname(lib) or d, CUDA_VERSION_FILE), encoding="utf-8-sig") as f:
                version = f.read().strip()
        return dict(offer=paths.nvidia() and not lib, installed=bool(lib), version=version,
                    size=sum(a["size"] for a in CUDA["archives"]), unpacked=CUDA["unpacked"], dir=d)

    def start_cuda(self):
        if not self.cuda_state()["offer"]:
            raise ValueError(tr("CUDA уже установлена или здесь не нужна"))
        if self.task and not self.task.done():
            raise ValueError(tr("загрузка уже идёт"))
        self.dl = dict(state="running", quant="", package="cuda", file="", done=0, total=0, speed=0.0, error="")
        self.task = asyncio.create_task(self._download_cuda())

    async def _download_cuda(self):
        d = self.cosy.cuda
        archives = cuda_archives()
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                self.dl.update(total=sum(a[2] for a in archives), done=_present(archives, d))
                await self._get_cuda(session)
            self.dl.update(state="done", file="", speed=0.0)
        except asyncio.CancelledError:
            self.dl.update(state="cancelled", speed=0.0, error="")
            log.info("загрузка CUDA отменена (докачается со следующей попытки)")
        except Exception as e:
            self.dl.update(state="error", speed=0.0, error=str(e) or type(e).__name__)
            log.warning("CUDA: %s", self.dl["error"])
        finally:
            self._push(force=True)

    async def _get_cuda(self, session):
        """Скачать архивы CUDA (докачка) и распаковать нужные файлы в папку CUDA (cosyvoice-server или paths.CUDA)."""
        d, archives = self.cosy.cuda, cuda_archives()
        os.makedirs(d, exist_ok=True)
        for url, name, size, sha, _ in archives:
            dst = os.path.join(d, name)
            if not (os.path.isfile(dst) and os.path.getsize(dst) == size):  # скачан в прошлый раз
                await fetch.fetch(self, session, url, name, dst, size, sha)
        self.dl.update(file=tr("{file}: распаковка", file="CUDA"), speed=0.0)
        self._push(force=True)
        async with self.runtime.paused():  # сервер держит открытыми ggml-*.dll; после — заново ищет устройства
            await asyncio.to_thread(_extract_cuda, archives, d)
            self.cosy.backends = None  # после перезапуска устройства ищутся заново: появится CUDA
        log.info("CUDA %s установлена в %s", CUDA["version"], d)

    # --- ESpeech (программа для Windows, образ Docker) ---
    def espeech_state(self):
        if not self.espeech or not self.espeech.downloaded:
            return dict(offer=False)
        return espeech_download.state(self.cuda_state())

    def start_espeech(self):
        st = self.espeech_state()
        if not st["offer"]:
            raise ValueError(tr("ESpeech здесь не скачивается"))
        if st["installed"]:
            raise ValueError(tr("ESpeech уже скачан"))
        if self.task and not self.task.done():
            raise ValueError(tr("загрузка уже идёт"))
        espeech_download.check_space(st)
        self.dl = dict(state="running", quant="", package="espeech", file="", done=0, total=0, speed=0.0, error="")
        self.task = asyncio.create_task(self._download_espeech(st))

    async def _download_espeech(self, st):
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                cuda = cuda_archives() if st["cuda"] else []
                self.dl.update(total=st["size"], done=espeech_download.bytes_present() +
                               (_present(cuda, self.cosy.cuda) if cuda else 0))
                steps = espeech_download.Steps(self, session)
                if cuda:   # cuBLAS для torch — из CUDA cosyvoice-server
                    await self._get_cuda(session)
                if st["runtime"] != "ok":
                    await steps.runtime(self._espeech_swap)
                if not st["model"]:
                    await steps.model()
            self.dl.update(state="done", file="", speed=0.0)
            log.info("ESpeech скачан")
            if self.runtime.launcher is self.espeech:
                await self.runtime.restart()
        except asyncio.CancelledError:
            self.dl.update(state="cancelled", speed=0.0, error="")
            log.info("загрузка ESpeech отменена (докачается со следующей попытки)")
        except Exception as e:
            self.dl.update(state="error", speed=0.0, error=str(e) or type(e).__name__)
            log.warning("ESpeech: %s", self.dl["error"])
        finally:
            self._push(force=True)

    async def _espeech_swap(self, tmp):
        """Собранный рантайм — на место прежнего (сервер ESpeech держит его файлы, если запущен)."""
        busy = self.runtime.launcher is self.espeech
        async with self.runtime.paused() if busy else contextlib.nullcontext():
            await asyncio.to_thread(_replace_dir, tmp, espeech_launch.RUNTIME)

    async def delete_espeech(self):
        if not self.espeech_state()["offer"]:
            raise ValueError(tr("ESpeech здесь не скачивается"))
        if self.task and not self.task.done() and self.dl.get("package") == "espeech":
            raise ValueError(tr("загрузка уже идёт"))
        busy = self.runtime.launcher is self.espeech
        async with self.runtime.paused() if busy else contextlib.nullcontext():
            await asyncio.to_thread(espeech_download.remove)
        log.info("ESpeech удалён")

    # --- Supertonic 3 ---
    def supertonic_state(self):
        return supertonic_download.state(bool(self.supertonic))

    def start_supertonic(self):
        st = self.supertonic_state()
        if not st["offer"]:
            raise ValueError(tr("Supertonic 3 здесь не скачивается"))
        if st["installed"]:
            raise ValueError(tr("Supertonic 3 уже скачан"))
        if self.task and not self.task.done():
            raise ValueError(tr("загрузка уже идёт"))
        supertonic_download.check_space()
        self.dl = dict(state="running", quant="", package="supertonic", file="", done=0, total=0, speed=0.0, error="")
        self.task = asyncio.create_task(self._download_supertonic())

    async def _download_supertonic(self):
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                await supertonic_download.download(self, session)
            self.dl.update(state="done", file="", speed=0.0)
            log.info("Supertonic 3 скачан, голоса: %s", ", ".join(sorted(supertonic_download.VOICES.values())))
            # сервер ждёт модель (runtime.Unavailable) или работает со старым списком голосов: запуск сразу
            if self.runtime.launcher is self.supertonic:
                await self.runtime.restart()
        except asyncio.CancelledError:
            self.dl.update(state="cancelled", speed=0.0, error="")
            log.info("загрузка Supertonic 3 отменена (докачается со следующей попытки)")
        except Exception as e:
            self.dl.update(state="error", speed=0.0, error=str(e) or type(e).__name__)
            log.warning("Supertonic 3: %s", self.dl["error"])
        finally:
            self._push(force=True)

    async def delete_supertonic(self):
        if not self.supertonic_state()["offer"]:
            raise ValueError(tr("Supertonic 3 здесь не скачивается"))
        if self.task and not self.task.done() and self.dl.get("package") == "supertonic":
            raise ValueError(tr("загрузка уже идёт"))
        busy = self.runtime.launcher is self.supertonic   # сервер держит открытыми файлы модели
        async with self.runtime.paused() if busy else contextlib.nullcontext():
            await asyncio.to_thread(supertonic_download.remove)
        log.info("Supertonic 3 удалён вместе с готовыми голосами")

    def _push(self, force=False):
        now = time.monotonic()
        if force or now - self._last_push > 0.5:
            self._last_push = now
            self.changed()

    # --- модель ---
    def start_download(self, quant, frontend):
        if quant not in QUANTS:
            raise ValueError(tr("неизвестный квант {quant}", quant=quant))
        if self.task and not self.task.done():
            raise ValueError(tr("загрузка уже идёт"))
        files = [(f"{BASE}/{model_name(quant)}", model_name(quant))]
        if frontend:
            files += package_files("frontend")
        self.dl = dict(state="running", quant=quant, package="", file="", done=0, total=0, speed=0.0, error="")
        self.task = asyncio.create_task(self._download(quant, files))

    def start_package(self, pid):
        if pid not in PACKAGES:
            raise ValueError(tr("нет такого пакета"))
        if self.task and not self.task.done():
            raise ValueError(tr("загрузка уже идёт"))
        self.dl = dict(state="running", quant="", package=pid, file="", done=0, total=0, speed=0.0, error="")
        self.task = asyncio.create_task(self._download("", package_files(pid)))

    async def delete_package(self, pid):
        if pid not in PACKAGES:
            raise ValueError(tr("нет такого пакета"))
        p = PACKAGES[pid]
        files = [rel for _, rel in package_files(pid)] + list(p.get("alt", ()))
        # сервер держит открытыми файлы фронтенда
        async with self.runtime.paused() if p.get("restart") else contextlib.nullcontext():
            for rel in files:
                with contextlib.suppress(FileNotFoundError):
                    os.remove(os.path.join(paths.MODELS, *rel.split("/")))
        log.info("пакет %s удалён", pid)

    def cancel(self):
        if self.task and not self.task.done():
            self.task.cancel()

    async def _download(self, quant, files):
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                plan = []  # (url, путь от paths.MODELS, dst, size, sha256)
                for url, rel in files:
                    dst = os.path.join(paths.MODELS, *rel.split("/"))
                    size, sha = await fetch.head(session, url, rel)
                    if os.path.isfile(dst) and (not size or os.path.getsize(dst) == size):
                        continue
                    plan.append((url, rel, dst, size, sha))
                self.dl["total"] = sum(p[3] for p in plan)
                self.dl["done"] = sum(fetch.part_size(p[2]) for p in plan)
                for p in plan:
                    await fetch.fetch(self, session, *p)
            self.dl.update(state="done", file="", speed=0.0)
            if quant:
                log.info("модель %s скачана", quant)
                self.s.update(model=model_name(quant))
            else:
                log.info("пакет %s скачан", self.dl["package"])
            if (quant or PACKAGES[self.dl["package"]].get("restart")) and self.runtime.launcher is self.cosy:
                await self.runtime.restart()  # новая модель или фронтенд подхватываются только при запуске
        except asyncio.CancelledError:
            self.dl.update(state="cancelled", speed=0.0, error="")
            log.info("загрузка отменена (докачается со следующей попытки)")
        except Exception as e:
            self.dl.update(state="error", speed=0.0, error=str(e) or type(e).__name__)
            log.warning("загрузка: %s", self.dl["error"])
        finally:
            self._push(force=True)


    def use_model(self, name):
        if name not in installed_models():
            raise ValueError(tr("нет такой модели"))
        self.s.update(model=name)

    # --- наборы голосов ---
    async def import_set(self, zip_path, filename):
        info = await asyncio.to_thread(voicesets._check_zip, zip_path, filename)
        async with self.runtime.paused():
            result = await asyncio.to_thread(voicesets._install_set, zip_path, info)
        log.info("набор %s: голосов %d, пропущено %d", result["name"], len(result["added"]), len(result["skipped"]))
        return result

    async def delete_set(self, name, model=None):
        """Голоса модели model из набора name; без model — весь набор (все модели)."""
        if not any(s["name"] == name and model in (None, s["model"]) for s in voicesets.list_sets()):
            raise ValueError(tr("нет такого набора"))
        async with self.runtime.paused():
            await asyncio.to_thread(voicesets._delete_set, name, model)
        log.info("набор %s%s удалён", name, f" ({model})" if model else "")


def _present(archives, d):
    """Сколько из архивов CUDA уже скачано: целиком или .part."""
    return sum(a[2] if os.path.isfile(os.path.join(d, a[1])) and os.path.getsize(os.path.join(d, a[1])) == a[2]
               else fetch.part_size(os.path.join(d, a[1])) for a in archives)


def _replace_dir(src, dst):
    shutil.rmtree(dst, ignore_errors=True)
    if os.path.exists(dst):
        raise RuntimeError(tr("не удалось удалить {dir}: файлы заняты", dir=dst))
    os.replace(src, dst)


def _put(src, dest, name):
    """Файл из архива — через .part, чтобы оборванная распаковка не оставила половину библиотеки."""
    tmp = os.path.join(dest, name + ".part")
    with src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst, CHUNK)
    if os.name != "nt":
        os.chmod(tmp, 0o755)
    os.replace(tmp, os.path.join(dest, name))


def _extract_cuda(archives, dest):
    """Из каждого архива — только его файлы из downloads.json, архивы потом не нужны: zip (Windows) — файлы в корне,
    tar.gz (Linux) — в папке сборки, файл ищется по имени среди обычных файлов (ссылки на них не нужны)."""
    for _, name, _, _, files in archives:
        path, got = os.path.join(dest, name), set()
        if name.endswith(".zip"):
            with zipfile.ZipFile(path) as z:
                names = {i.filename: i for i in z.infolist()}
                for f in files:
                    if f in names:
                        _put(z.open(names[f]), dest, f)
                        got.add(f)
        else:
            with tarfile.open(path, "r|gz") as tar:   # за один проход: tar.gz не перематывается
                for m in tar:
                    f = os.path.basename(m.name)
                    if m.isfile() and f in files and f not in got:
                        _put(tar.extractfile(m), dest, f)
                        got.add(f)
        missing = [f for f in files if f not in got]
        if missing:
            raise RuntimeError(tr("в архиве CUDA нет {files}", files=", ".join(missing)))
    with open(os.path.join(dest, CUDA_VERSION_FILE), "w", encoding="utf-8") as f:
        f.write(CUDA["version"])
    for _, name, _, _, _ in archives:
        os.remove(os.path.join(dest, name))


def package_installed(pid):
    have = lambda f: os.path.isfile(os.path.join(paths.MODELS, *f.split("/")))
    files = [rel for _, rel in package_files(pid)]
    # фронтенд: каждый файл в int8 или в полной точности (как выбирает engines/cosyvoice/launch.py)
    alt = PACKAGES[pid].get("alt") or files
    return all(have(a) or have(b) for a, b in zip(files, alt))


def package_usable(pid):
    """Программа умеет работать с пакетом: очистке от музыки нужен onnxruntime."""
    if pid == "music":
        from . import separate
        return separate.runtime_ok()
    return True


def packages(installed=None):
    """Необязательные пакеты для админки. installed — id -> bool для пакетов, файлов которых не видно (фронтенд
    готового сервера в тестах)."""
    installed = installed or {}
    return [dict(id=pid, title=tr(p["title"]), about=tr(p["about"]), size=p["size"],
                 source=p["source"], restart=bool(p.get("restart")), usable=package_usable(pid),
                 installed=installed[pid] if pid in installed else package_installed(pid))
            for pid, p in PACKAGES.items()]


