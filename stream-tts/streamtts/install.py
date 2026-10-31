"""Загрузки через админку: модель, CUDA, ESpeech, дополнительные пакеты; установка наборов голосов.
Установщик программы для Windows ничего не качает: модель, CUDA и ESpeech — кнопки на вкладке «Сервер» (при первом
запуске без модели админка открывается на ней), пакеты — в настройках.

Модель качается как в scripts/download-model.sh: Lourdle/Fun-CosyVoice3-0.5B-2512-GGUF с Hugging Face (ревизия
закреплена в downloads.json, model.url), квант на выбор (по умолчанию Q8_0), по желанию ONNX-фронтенд int8 (нужен
только для извлечения голоса из аудио, для озвучки не нужен; он же — дополнительный пакет из PACKAGES, качается и
удаляется из настроек админки). Другие пакеты
(htdemucs для очистки образца от музыки, separate.py) — из своих репозиториев Hugging Face. Файл пишется в <имя>.part
с докачкой (Range) и повторами, сверяется с sha256 из x-linked-etag и только потом переименовывается. Папка
моделей — paths.MODELS.

CUDA (ggml-cuda.dll и рантайм CUDA 13) — два архива из релиза llama.cpp той же сборки, что и ядро GGML (адреса,
размеры и sha256 — в downloads.json, cuda.archives; там же размеры квантов модели). Если в системе есть драйвер
NVIDIA (nvcuda.dll), а ggml-cuda.dll рядом с cosyvoice-server нет, вкладка «Сервер» предлагает скачать их: из
архивов берутся только нужные файлы (cuda.files), они распаковываются в папку сервера при остановленном сервере,
версия пишется в cuda-version.txt (по ней установщик при обновлении решает, сохранить ли CUDA или удалить вместе со
старым рантаймом), потом сервер перезапускается и заново ищет устройства.

ESpeech (рантайм с torch и модель) программа для Windows качает по кнопке на вкладке «Сервер» —
engines/espeech/download.py; CUDA, если её нет, качается вместе с ним (torch берёт cuBLAS оттуда).

Набор голосов — zip с голосами (*.gguf CosyVoice, *.espeech ESpeech) и manifest.json (ключи — имена голосов:
title, post…) в корне или в одной папке. Имя набора — эта папка или имя zip; оно же группа на странице голосов
(voices.GROUPS). Набор ставится в paths.VOICES/<набор>/; голоса тех моделей, что есть в zip, заменяются целиком,
голоса другой модели в той же папке остаются (коллекция «модель + источник»: voice-sets/<модель>/<источник>.zip).
Голос, который у этой модели уже есть в другом наборе или без набора, пропускается. Удаляется набор тоже по модели.
Пока файлы меняются, сервер синтеза остановлен (на Windows cosyvoice-server держит открытые .gguf), потом
запускается с новым списком голосов."""
import asyncio, contextlib, hashlib, json, logging, os, re, shutil, time, zipfile

import aiohttp

from . import paths
from .engines import ENGINES, cosyvoice
from .engines.espeech import download as espeech_download
from .i18n import tr

log = logging.getLogger("install")

# Закреплённые загрузки (версию и файлы CUDA, размеры для сводки build-exe.py передаёт ещё и в StreamTTS.iss)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads.json"), encoding="utf-8") as _f:
    DOWNLOADS = json.load(_f)
CUDA = DOWNLOADS["cuda"]
# Другая папка с теми же архивами (проверка без GitHub: python -m http.server в packaging/windows/build/cache)
CUDA_BASE = paths.env("STREAM_TTS_CUDA_URL").rstrip("/")


def cuda_archives():
    """[(url, имя файла, размер, sha256, нужные файлы)] архивов CUDA."""
    out = []
    for a in CUDA["archives"]:
        name = a["url"].rsplit("/", 1)[-1]
        out.append((f"{CUDA_BASE}/{name}" if CUDA_BASE else a["url"], name, a["size"], a["sha256"], a["files"]))
    return out
CUDA_VERSION_FILE = "cuda-version.txt"

# Модель CosyVoice: репозиторий, кванты и видеопамять — в описании движка (engines/cosyvoice), размеры и адрес с
# закреплённой ревизией — из downloads.json (sha256 сверяется по x-linked-etag и совпадает с закреплённым)
REPO = cosyvoice.REPO
BASE = DOWNLOADS["model"]["url"].rstrip("/")
QUANTS = {q: dict(d, size=DOWNLOADS["model"]["quants"][q]["size"]) for q, d in cosyvoice.QUANTS.items()}
DEFAULT_QUANT = cosyvoice.DEFAULT_QUANT
SAMPLES = (("ohf-denis", "Пример"),)
SAMPLE_PHRASE = "Ну всё, я пошёл в бой! Если не вернусь — считайте, что я был прав, а вы нет. Вперёд!"
FRONTEND = cosyvoice.FRONTEND
# Необязательные пакеты: качаются из админки (настройки и редактор голоса). files — пути в репозитории source на
# Hugging Face, локально — paths.MODELS/<into>/<путь>. size — байт. alt — те же файлы в полной точности
# (scripts/download-model.sh --frontend full): тоже считаются установленными. restart — файлы читает
# cosyvoice-server: после загрузки он перезапускается, а удаляются они при остановленном сервере.
PACKAGES = {
    "frontend": dict(
        title="Создание голосов из записи",
        about="ONNX-фронтенд CosyVoice3: речевой токенизатор и CAM++ (отпечаток голоса). Нужен, чтобы сделать "
              "голос из записи.",
        files=FRONTEND, size=252891972, source=REPO, restart=True,
        alt=("frontend-onnx/campplus.onnx", "frontend-onnx/speech_tokenizer_v3.onnx")),
    # Веса Demucs обучены на MusDB: автор называет их исследовательскими (facebookresearch/demucs#267, #327, #508),
    # код Demucs и экспорт в ONNX — MIT. Поэтому пакет не входит в установщик, а качается по кнопке; лицензии всех
    # загрузок перечислены на странице «Информация» установщика (packaging/windows/THIRD-PARTY*.txt).
    "music": dict(
        title="Очистка образца от музыки",
        about="Demucs v4 отделяет голос от музыки. Нужен для функции «убрать музыку» в редакторе голоса.",
        files=("htdemucs_int8.onnx",), into="demucs", size=84108789, source="HRSadeghi/demucs-onnx"),
}
CHUNK = 1 << 20
RETRIES = 5

SET_NAME = re.compile(r"[^a-z0-9_-]+")
VOICE_NAME = re.compile(r"^[\w.-]{1,64}$")
MAX_VOICE = 64 << 20       # голос CosyVoice ~200 КБ, ESpeech с адаптером ~10 МБ; больше — точно не голос
# Модель по расширению файла голоса. Папка набора (источника) держит голоса обеих моделей: голос с тем же именем у
# другой модели — в том же наборе (editor.py). Коллекция в zip — голоса одной модели одного источника
# (voice-sets/<модель>/<источник>.zip, tools/voices/pack-sets.py): её установка и удаление не трогают файлы другой.
EXT_MODEL = {E.VOICE_EXT: i for i, E in ENGINES.items()}
VOICE_EXTS = tuple(EXT_MODEL)
MAX_ZIP_FILES = 2000


def package_files(pid):
    """[(url, путь от paths.MODELS)] файлов пакета."""
    p = PACKAGES[pid]
    return [(f"https://huggingface.co/{p['source']}/resolve/main/{f}", f"{p['into']}/{f}" if p.get("into") else f)
            for f in p["files"]]


model_name = cosyvoice.model_name


def installed_models():
    try:
        return sorted(f for f in os.listdir(paths.MODELS) if f.endswith(".gguf"))
    except OSError:
        return []


class Cancelled(Exception):
    pass


class Installer:
    def __init__(self, settings, runtime, changed, cosy=None, espeech=None):
        self.s, self.runtime, self.changed = settings, runtime, changed  # changed() — разбудить админку
        self.cosy = cosy   # запуск CosyVoice (engines/cosyvoice): модель, CUDA и фронтенд — его
        self.espeech = espeech   # запуск ESpeech: у программы для Windows рантайм и модель качаются (download.py)
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
                    models_dir=paths.MODELS, voices_dir=paths.VOICES, download=dict(self.dl), sets=list_sets(),
                    packages=packages(), cuda=self.cuda_state(), espeech=self.espeech_state())

    # --- CUDA ---
    def cuda_state(self):
        """CUDA для вкладки «Сервер»: offer — предложить скачать (есть драйвер NVIDIA, а ggml-cuda.dll нет)."""
        if not self.cosy:
            return dict(offer=False, installed=False, version="", size=0, unpacked=0, dir="")
        d = self.cosy.dir
        version = ""
        with contextlib.suppress(OSError):
            with open(os.path.join(d, CUDA_VERSION_FILE), encoding="utf-8-sig") as f:
                version = f.read().strip()
        installed = os.path.isfile(os.path.join(d, "ggml-cuda" + (".dll" if os.name == "nt" else ".so")))
        nvidia = os.name == "nt" and os.path.isfile(
            os.path.join(paths.env("SystemRoot", r"C:\Windows"), "System32", "nvcuda.dll"))
        return dict(offer=nvidia and not installed, installed=installed, version=version,
                    size=sum(a["size"] for a in CUDA["archives"]), unpacked=CUDA["unpacked"], dir=d)

    def start_cuda(self):
        if not self.cuda_state()["offer"]:
            raise ValueError(tr("CUDA уже установлена или здесь не нужна"))
        if self.task and not self.task.done():
            raise ValueError(tr("загрузка уже идёт"))
        self.dl = dict(state="running", quant="", package="cuda", file="", done=0, total=0, speed=0.0, error="")
        self.task = asyncio.create_task(self._download_cuda())

    async def _download_cuda(self):
        d = self.cosy.dir
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
        """Скачать архивы CUDA (докачка) и распаковать нужные файлы в папку cosyvoice-server."""
        d, archives = self.cosy.dir, cuda_archives()
        for url, name, size, sha, _ in archives:
            dst = os.path.join(d, name)
            if not (os.path.isfile(dst) and os.path.getsize(dst) == size):  # скачан в прошлый раз
                await self._fetch(session, url, name, dst, size, sha)
        self.dl.update(file=tr("{file}: распаковка", file="CUDA"), speed=0.0)
        self._push(force=True)
        async with self.runtime.paused():  # сервер держит открытыми ggml-*.dll
            await asyncio.to_thread(_extract_cuda, archives, d)
            self.cosy.backends = None  # после перезапуска устройства ищутся заново: появится CUDA
        log.info("CUDA %s установлена в %s", CUDA["version"], d)

    # --- ESpeech (программа для Windows) ---
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
                               (_present(cuda, self.cosy.dir) if cuda else 0))
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
            await asyncio.to_thread(_replace_dir, tmp, espeech_download.launch.RUNTIME)

    async def delete_espeech(self):
        if not self.espeech_state()["offer"]:
            raise ValueError(tr("ESpeech здесь не скачивается"))
        if self.task and not self.task.done() and self.dl.get("package") == "espeech":
            raise ValueError(tr("загрузка уже идёт"))
        busy = self.runtime.launcher is self.espeech
        async with self.runtime.paused() if busy else contextlib.nullcontext():
            await asyncio.to_thread(espeech_download.remove)
        log.info("ESpeech удалён")

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
                    size, sha = await self._head(session, url, rel)
                    if os.path.isfile(dst) and (not size or os.path.getsize(dst) == size):
                        continue
                    plan.append((url, rel, dst, size, sha))
                self.dl["total"] = sum(p[3] for p in plan)
                self.dl["done"] = sum(_part_size(p[2]) for p in plan)
                for p in plan:
                    await self._fetch(session, *p)
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

    async def _head(self, session, url, rel):
        """Размер и sha256 файла: у LFS-файлов Hugging Face отдаёт их в x-linked-size и x-linked-etag до редиректа."""
        async with session.head(url, allow_redirects=False) as r:
            if r.status >= 400:
                raise RuntimeError(f"{rel}: HTTP {r.status}")
            size = int(r.headers.get("x-linked-size") or r.headers.get("content-length") or 0)
            sha = r.headers.get("x-linked-etag", "").strip('"')
            return size, sha if re.fullmatch(r"[0-9a-f]{64}", sha) else ""

    async def _fetch(self, session, url, rel, dst, size, sha):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        part = dst + ".part"
        self.dl["file"] = rel
        for attempt in range(1, RETRIES + 1):
            have = _part_size(dst)
            if size and have > size:
                os.remove(part)
                self.dl["done"] -= have
                have = 0
            if size and have == size:
                break
            headers = {"Range": f"bytes={have}-"} if have else {}
            try:
                async with session.get(url, headers=headers) as r:
                    if r.status == 200 and have:  # сервер не умеет Range: заново
                        self.dl["done"] -= have
                        have = 0
                    elif r.status not in (200, 206):
                        raise RuntimeError(f"HTTP {r.status}")
                    t0, got0 = time.monotonic(), self.dl["done"]
                    with open(part, "ab" if have else "wb") as f:
                        async for chunk in r.content.iter_chunked(CHUNK):
                            f.write(chunk)
                            self.dl["done"] += len(chunk)
                            dt = time.monotonic() - t0
                            if dt > 1:
                                self.dl["speed"] = (self.dl["done"] - got0) / dt
                            self._push()
                if not size or _part_size(dst) == size:
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
                if attempt == RETRIES:
                    raise RuntimeError(f"{rel}: {e or type(e).__name__}") from e
                log.warning("%s: %s, повтор %d через 3 с", rel, e or type(e).__name__, attempt)
                await asyncio.sleep(3)
        if sha:
            self.dl.update(file=tr("{file}: проверка", file=rel), speed=0.0)
            self._push(force=True)
            got = await asyncio.to_thread(_sha256, part)
            if got != sha:
                os.remove(part)
                raise RuntimeError(tr("{file}: контрольная сумма не совпала, файл удалён — скачайте ещё раз", file=rel))
        os.replace(part, dst)

    def use_model(self, name):
        if name not in installed_models():
            raise ValueError(tr("нет такой модели"))
        self.s.update(model=name)

    # --- наборы голосов ---
    async def import_set(self, zip_path, filename):
        info = await asyncio.to_thread(_check_zip, zip_path, filename)
        async with self.runtime.paused():
            result = await asyncio.to_thread(_install_set, zip_path, info)
        log.info("набор %s: голосов %d, пропущено %d", result["name"], len(result["added"]), len(result["skipped"]))
        return result

    async def delete_set(self, name, model=None):
        """Голоса модели model из набора name; без model — весь набор (все модели)."""
        if not any(s["name"] == name and model in (None, s["model"]) for s in list_sets()):
            raise ValueError(tr("нет такого набора"))
        async with self.runtime.paused():
            await asyncio.to_thread(_delete_set, name, model)
        log.info("набор %s%s удалён", name, f" ({model})" if model else "")


def _present(archives, d):
    """Сколько из архивов CUDA уже скачано: целиком или .part."""
    return sum(a[2] if os.path.isfile(os.path.join(d, a[1])) and os.path.getsize(os.path.join(d, a[1])) == a[2]
               else _part_size(os.path.join(d, a[1])) for a in archives)


def _replace_dir(src, dst):
    shutil.rmtree(dst, ignore_errors=True)
    if os.path.exists(dst):
        raise RuntimeError(tr("не удалось удалить {dir}: файлы заняты", dir=dst))
    os.replace(src, dst)


def _extract_cuda(archives, dest):
    """Из каждого архива — только его файлы из downloads.json (лежат в корне архива); архивы потом не нужны."""
    for _, name, _, _, files in archives:
        zpath = os.path.join(dest, name)
        with zipfile.ZipFile(zpath) as z:
            names = {i.filename: i for i in z.infolist()}
            missing = [f for f in files if f not in names]
            if missing:
                raise RuntimeError(tr("в архиве CUDA нет {files}", files=", ".join(missing)))
            for f in files:
                tmp = os.path.join(dest, f + ".part")
                with z.open(names[f]) as src, open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
                os.replace(tmp, os.path.join(dest, f))
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


def _part_size(dst):
    try:
        return os.path.getsize(dst + ".part")
    except OSError:
        return 0


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return h.hexdigest()


def _voice_files(d):
    """{модель: {голос: файл}} в папке d (без подпапок)."""
    out = {}
    with contextlib.suppress(OSError):
        for f in os.listdir(d):
            name, ext = os.path.splitext(f)
            if ext in EXT_MODEL and os.path.isfile(os.path.join(d, f)):
                out.setdefault(EXT_MODEL[ext], {})[name] = f
    return out


def list_sets():
    """Установленные наборы: подпапки paths.VOICES с голосами, по записи на модель (порядок ENGINES)."""
    out = []
    try:
        names = sorted(os.listdir(paths.VOICES))
    except OSError:
        return out
    for n in names:
        d = os.path.join(paths.VOICES, n)
        if n.startswith(".") or not os.path.isdir(d):
            continue
        files = _voice_files(d)
        for model in (m for m in ENGINES if m in files):
            out.append(dict(name=n, model=model, title=ENGINES[model].TITLE, voices=sorted(files[model]),
                            manifest=os.path.isfile(os.path.join(d, "manifest.json"))))
    return out


def _existing_voices(skip_set, model):
    """Голос модели -> где лежит (набор или корень), кроме заменяемого набора."""
    out = dict.fromkeys(_voice_files(paths.VOICES).get(model, {}), tr("голоса без набора"))
    for s in list_sets():
        if s["name"] != skip_set and s["model"] == model:
            out.update(dict.fromkeys(s["voices"], tr("набор {name}", name=s["name"])))
    return out


def _check_voice(z, i, ext):
    """Файл похож на голос своей модели: GGUF — по сигнатуре, .espeech (safetensors) — по заголовку JSON."""
    with z.open(i) as f:
        head = f.read(9)
    if ext == cosyvoice.VOICE_EXT:
        return head[:4] == b"GGUF"
    n = int.from_bytes(head[:8], "little")
    return len(head) == 9 and 0 < n < i.file_size and head[8:9] == b"{"


def _check_zip(path, filename):
    """Что ставить из zip: имя набора, голоса ({модель: {имя: запись в zip}}), manifest. Ошибка — ValueError."""
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ValueError(tr("это не zip"))
    with z:
        entries = [i for i in z.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
        if len(entries) > MAX_ZIP_FILES:
            raise ValueError(tr("слишком много файлов в архиве"))
        tops = {i.filename.split("/")[0] for i in entries if "/" in i.filename}
        single = len(tops) == 1 and all("/" in i.filename for i in entries)
        raw = tops.pop() if single else os.path.splitext(os.path.basename(filename or "voices"))[0]
        name = SET_NAME.sub("-", raw.lower()).strip("-_") or "voices"
        voices, manifest, ignored = {}, None, []
        for i in entries:
            rel = i.filename.split("/", 1)[1] if single else i.filename
            base = rel.rsplit("/", 1)[-1]
            voice, ext = os.path.splitext(base)
            ext = ext.lower()
            if "/" in rel:  # только верхний уровень набора
                ignored.append(i.filename)
            elif base == "manifest.json":
                manifest = i.filename
            elif ext in EXT_MODEL:
                if not VOICE_NAME.match(voice):
                    raise ValueError(tr("недопустимое имя голоса: {voice}", voice=voice))
                if i.file_size > MAX_VOICE:
                    raise ValueError(tr("{file}: слишком большой для голоса", file=base))
                if not _check_voice(z, i, ext):
                    raise ValueError(tr("{file}: не голос {model}", file=base, model=ENGINES[EXT_MODEL[ext]].TITLE))
                voices.setdefault(EXT_MODEL[ext], {})[voice] = i.filename
            else:
                ignored.append(i.filename)
        if not voices:
            raise ValueError(tr("в архиве нет голосов (*.gguf, *.espeech)"))
        meta = {}
        if manifest:
            try:
                meta = json.loads(z.read(manifest).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                raise ValueError(f"manifest.json: {e}")
            if not isinstance(meta, dict):
                raise ValueError(tr("manifest.json: ожидался объект {голос: {...}}"))
    return dict(name=name, voices=voices, manifest=meta, ignored=ignored)


def _prune_manifest(d, add=None, keep=()):
    """manifest.json набора: записи новых голосов поверх старых, без голосов, у которых не осталось файлов.
    Запись голоса из keep (у него есть файл другой модели) сливается по полям: манифест общий для обеих моделей, и
    коллекция ESpeech без post иначе стирала бы эффект голоса CosyVoice. Остальные записи заменяются целиком —
    новая версия коллекции может убрать поле."""
    path = os.path.join(d, "manifest.json")
    have = {v for vs in _voice_files(d).values() for v in vs}
    try:
        with open(path, encoding="utf-8") as f:
            meta = json.load(f)
    except (OSError, ValueError):
        meta = {}
    meta = {k: v for k, v in (meta if isinstance(meta, dict) else {}).items() if k in have}
    for k, v in (add or {}).items():
        if k in have:
            old = meta.get(k)
            meta[k] = {**old, **v} if k in keep and isinstance(old, dict) and isinstance(v, dict) else v
    if meta:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
    else:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


def _install_set(path, info):
    name = info["name"]
    target = os.path.join(paths.VOICES, name)
    added, skipped = [], []
    os.makedirs(paths.VOICES, exist_ok=True)
    tmp = os.path.join(paths.VOICES, f".{name}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        with zipfile.ZipFile(path) as z:   # сначала всё во временную папку: битый zip не трогает набор
            for model, voices in sorted(info["voices"].items()):
                taken = _existing_voices(name, model)
                ext = ENGINES[model].VOICE_EXT
                for voice, entry in sorted(voices.items()):
                    if voice in taken:
                        skipped.append(tr("{voice} (уже есть: {where})", voice=voice + ext, where=taken[voice]))
                        continue
                    with z.open(entry) as src, open(os.path.join(tmp, voice + ext), "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    if voice not in added:   # голос с файлами обеих моделей в одном zip — один голос
                        added.append(voice)
        if not added:
            raise ValueError(tr("все голоса набора уже установлены в других наборах: {voices}", voices=", ".join(skipped)))
        # голоса моделей из zip заменяются целиком, голоса другой модели остаются
        os.makedirs(target, exist_ok=True)
        old = _voice_files(target)
        for model in info["voices"]:
            for f in old.get(model, {}).values():
                os.remove(os.path.join(target, f))
        for f in os.listdir(tmp):
            os.replace(os.path.join(tmp, f), os.path.join(target, f))
        others = {v for m, vs in _voice_files(target).items() if m not in info["voices"] for v in vs}
        _prune_manifest(target, {k: v for k, v in info["manifest"].items() if k in added}, keep=others)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dict(name=name, models=sorted(info["voices"]), added=added, skipped=skipped, ignored=info["ignored"])


def _delete_set(name, model=None):
    target = os.path.join(paths.VOICES, name)
    if model:
        for f in _voice_files(target).get(model, {}).values():
            os.remove(os.path.join(target, f))
        _prune_manifest(target)
    if model is None or not _voice_files(target):
        shutil.rmtree(target)
