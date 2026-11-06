"""Загрузки через админку (Installer, вкладка «Загрузки»): кванты модели CosyVoice3, CUDA, ESpeech, Supertonic 3,
дополнительные пакеты — очередью, по одной; удаление скачанного; состояние загрузок, пакетов и места на диске для
админки; установка наборов голосов с остановкой сервера (сами наборы — voicesets.py).
Установщик программы для Windows ничего не качает: всё — кнопками на вкладке «Загрузки» (при первом запуске, пока
модель не выбрана или не скачана, админка открывается на ней).

Очередь: id загрузки — квант (Q8_0…), cuda, espeech, supertonic или id пакета (PACKAGES). «Скачать» во время загрузки
ставит в конец очереди, «Убрать» убирает оттуда, отмена или ошибка идущей запускает следующую; оборванная (failed)
докачается с того же места. ESpeech, которому нужна CUDA (cuBLAS для torch), ставит её в очередь перед собой. Первая
скачанная модель синтеза (любой движок) становится выбранной (settings.engine, engines.Tts), следующие выбор не меняют.

Модель качается как в scripts/download-model.sh: Lourdle/Fun-CosyVoice3-0.5B-2512-GGUF с Hugging Face (ревизия
закреплена в downloads.json, model.url), квант на выбор (по умолчанию Q8_0). Скачанный квант становится вариантом
CosyVoice3, только если выбранного нет; выбранный можно удалить — CosyVoice3 перейдёт на оставшийся (первый по имени)
или будет ждать модель. ONNX-фронтенд int8 (нужен только для извлечения голоса из аудио) и htdemucs для очистки
образца от музыки (separate.py) — дополнительные пакеты из PACKAGES, каждый из своего репозитория Hugging Face. Что
качается и откуда — downloads.py, файл с докачкой и проверкой sha256 — fetch.py. Папка моделей — paths.MODELS.

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
from .downloads import (BASE, CUDA, CUDA_VERSION_FILE, DEFAULT_QUANT, PACKAGES, QUANTS, SAMPLE_PHRASE, cuda_archives,
                        package_files)
from .engines import cosyvoice
from .engines.espeech import download as espeech_download, launch as espeech_launch
from .engines.supertonic import download as supertonic_download
from .fetch import CHUNK
from .i18n import tr

log = logging.getLogger("install")


model_name = cosyvoice.model_name
# Контейнер Docker: CUDA видна, только если его запустили с docker-compose.gpu.yml; папки — тома
DOCKER = os.name != "nt" and os.path.exists("/.dockerenv")
TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)


def installed_models():
    try:
        return sorted(f for f in os.listdir(paths.MODELS) if f.endswith(".gguf"))
    except OSError:
        return []


class Cancelled(Exception):
    pass


class Installer:
    def __init__(self, settings, runtime, changed, launchers=None, pick=None):
        """launchers — запуски найденных движков {id: Launcher}: у каждого свои загрузки. pick(engine) — сделать
        модель выбранной (смена модели задачей, как POST /api/engine): первая скачанная становится выбранной."""
        self.s, self.runtime, self.changed = settings, runtime, changed  # changed() — разбудить админку
        self.launchers, self.pick = launchers or {}, pick
        self.cosy = self.launchers.get("cosyvoice")   # запуск CosyVoice (engines/cosyvoice): модель, CUDA и фронтенд — его
        # запуск ESpeech: у программы для Windows и в Docker рантайм и модель качаются (download.py)
        self.espeech = self.launchers.get("espeech")
        # запуск Supertonic 3: модель и готовые голоса качаются везде, где он есть (engines/supertonic/download.py)
        self.supertonic = self.launchers.get("supertonic")
        self.task = None
        # идущая загрузка или последняя; queue — следующие по порядку; failed — оборванные и отменённые {id: dl}
        self.dl = dict(state="idle", id="", file="", done=0, total=0, speed=0.0, error="")
        self.queue, self.failed = [], {}
        self.closing = False
        self._last_push = 0.0

    # --- состояние для админки ---
    def public(self):
        quants = [dict(QUANTS[q], quant=q, file=model_name(q)) for q in QUANTS]
        model = self.cosy.model if self.cosy else ""
        return dict(quants=quants, default_quant=DEFAULT_QUANT, sample_phrase=SAMPLE_PHRASE, samples=samples(),
                    models=installed_models(), model=os.path.basename(model) if model else self.selected_model(),
                    models_dir=paths.MODELS, voices_dir=paths.VOICES, sets=voicesets.list_sets(),
                    download=dict(self.dl), queue=list(self.queue), failed=dict(self.failed),
                    packages=packages(), cuda=self.cuda_public(), espeech=self.espeech_state(),
                    supertonic=self.supertonic_state(), disk=disk())

    def cuda_public(self):
        """CUDA для «Загрузок»: gpu — есть видеокарта NVIDIA (CUDA ей пригодится), pinned — закреплённая версия.
        deletable — удалить можно только скачанную кнопкой (cuda-version.txt), а не из сборки сервера."""
        st = self.cuda_state()
        return dict(st, gpu=st["offer"] or st["installed"], pinned=CUDA["version"], docker=DOCKER, deletable=False)

    def selected_model(self):
        """Вариант CosyVoice3, с которым запустится сервер (engines/cosyvoice/launch.py, _model): выбранный или
        единственный; "" — нет или выбрать нечего."""
        have, m = installed_models(), self.s.get("model", "")
        return m if m in have else have[0] if len(have) == 1 else ""

    # --- очередь загрузок ---
    def current(self):
        """id идущей загрузки или ""."""
        return self.dl["id"] if self.task and not self.task.done() else ""

    def _check(self, did):
        """Можно ли скачать did сейчас: ValueError с причиной (неизвестная, уже скачана, здесь не качается, мало места)."""
        if did in QUANTS:
            if not self.cosy:
                raise ValueError(tr("CosyVoice3 здесь не скачивается"))
            if model_name(did) in installed_models():
                raise ValueError(tr("{name} уже скачан", name=did))
        elif did == "cuda":
            if not self.cuda_state()["offer"]:
                raise ValueError(tr("CUDA уже установлена или здесь не нужна"))
        elif did == "espeech":
            st = self.espeech_state()
            if not st["offer"]:
                raise ValueError(tr("ESpeech здесь не скачивается"))
            if st["installed"]:
                raise ValueError(tr("ESpeech уже скачан"))
            espeech_download.check_space(st)
        elif did == "supertonic":
            st = self.supertonic_state()
            if not st["offer"]:
                raise ValueError(tr("Supertonic 3 здесь не скачивается"))
            if st["installed"]:
                raise ValueError(tr("Supertonic 3 уже скачан"))
            supertonic_download.check_space()
        elif did in PACKAGES:
            if package_installed(did):
                raise ValueError(tr("пакет уже скачан"))
            if not package_usable(did):
                raise ValueError(tr("пакет здесь недоступен"))
        else:
            raise ValueError(tr("нет такой загрузки"))

    def enqueue(self, did):
        """Скачать did: сразу или в конец очереди. ESpeech, которому нужна CUDA, ставит её перед собой."""
        self._check(did)
        if did == self.current() or did in self.queue:
            raise ValueError(tr("уже в очереди"))
        todo = [did]
        if did == "espeech" and self.espeech_state()["cuda"] and "cuda" not in (self.current(), *self.queue):
            todo.insert(0, "cuda")   # cuBLAS для torch — из CUDA cosyvoice-server
        self.queue += todo
        self._next()
        self._push(force=True)

    def unqueue(self, did):
        if did not in self.queue:
            raise ValueError(tr("этого нет в очереди"))
        self.queue.remove(did)
        self._push(force=True)

    def cancel(self):
        """Отменить идущую загрузку: следующая из очереди запустится сама."""
        if self.task and not self.task.done():
            self.task.cancel()

    def close(self):
        """Остановка stream-tts: очередь не продолжается."""
        self.closing = True
        self.queue.clear()
        self.cancel()

    def _next(self):
        while not self.closing and not self.current() and self.queue:
            did = self.queue.pop(0)
            try:
                self._check(did)   # пока ждала, могла скачаться (CUDA вместе с ESpeech) или кончиться место
            except ValueError as e:
                self.failed[did] = dict(state="error", id=did, file="", done=0, total=0, speed=0.0, error=str(e))
                log.warning("загрузка %s: %s", did, e)
                continue
            self.failed.pop(did, None)
            self.dl = dict(state="running", id=did, file="", done=0, total=0, speed=0.0, error="")
            self.task = asyncio.create_task(self._run(did))

    async def _run(self, did):
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                await self._job(did, session)
            self.dl.update(state="done", file="", speed=0.0)
            log.info("скачано: %s", did)
        except asyncio.CancelledError:
            self.dl.update(state="cancelled", speed=0.0, error="")
            self.failed[did] = dict(self.dl)
            log.info("загрузка %s отменена (докачается со следующей попытки)", did)
        except Exception as e:
            self.dl.update(state="error", speed=0.0, error=str(e) or type(e).__name__)
            self.failed[did] = dict(self.dl)
            log.warning("загрузка %s: %s", did, self.dl["error"])
        else:
            try:
                await self._after(did)
            except Exception as e:
                log.warning("после загрузки %s: %s", did, e)
        finally:
            self.task = None
            self._next()
            self._push(force=True)

    async def _job(self, did, session):
        if did in QUANTS:
            await self._fetch_files(session, [(f"{BASE}/{model_name(did)}", model_name(did))])
        elif did == "cuda":
            d = self.cosy.cuda
            self.dl.update(total=sum(a[2] for a in cuda_archives()), done=_present(cuda_archives(), d))
            await self._get_cuda(session)
        elif did == "espeech":
            await self._download_espeech(session, self.espeech_state())
        elif did == "supertonic":
            await supertonic_download.download(self, session)
            log.info("Supertonic 3: голоса %s", ", ".join(sorted(supertonic_download.VOICES.values())))
        else:
            await self._fetch_files(session, package_files(did))

    async def _after(self, did):
        """Скачанное подхватывается сервером только при запуске: модель — выбрать (первую) и запустить сервер."""
        if did in QUANTS:
            # квант становится вариантом, только если выбранного нет (файл уже на месте: selected_model() — он)
            changed = self.s.get("model", "") not in installed_models()
            if changed:
                self.s.update(model=model_name(did))
            await self._model_ready("cosyvoice", changed)
        elif did in ("espeech", "supertonic"):
            await self._model_ready(did)
        elif did in PACKAGES and PACKAGES[did].get("restart") and self.runtime.launcher is self.cosy:
            await self.runtime.restart()

    async def _model_ready(self, engine, restart=True):
        """Модель engine скачана: первая скачанная становится выбранной (смена модели — задачей, pick), а сервер этой
        модели перезапускается — ждущий модель запустится."""
        launcher = self.launchers.get(engine)
        if not self.s.get("engine"):
            self.s.update(engine=engine)
            log.info("первая скачанная модель — выбрана: %s", engine)
            if self.runtime.launcher is not launcher and self.pick:
                self.pick(engine)
                return
            restart = True
        if restart and self.runtime.launcher is launcher:
            await self.runtime.restart()

    async def _fetch_files(self, session, files):
        """Файлы [(url, путь от paths.MODELS)]: уже скачанные (тот же размер) пропускаются, остальные — с докачкой."""
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

    # --- удаление ---
    async def delete(self, did):
        if did == self.current():
            raise ValueError(tr("идёт загрузка: сначала отмените её"))
        if did in QUANTS:
            await self.delete_model(did)
        elif did == "espeech":
            await self.delete_espeech()
        elif did == "supertonic":
            await self.delete_supertonic()
        elif did in PACKAGES:
            await self.delete_package(did)
        else:
            raise ValueError(tr("это не удаляется"))
        with contextlib.suppress(ValueError):
            self.queue.remove(did)
        self.failed.pop(did, None)
        self._push(force=True)

    async def delete_model(self, quant):
        """Квант CosyVoice3. Выбранный тоже: CosyVoice3 перейдёт на оставшийся (первый по имени) или будет ждать модель."""
        name = model_name(quant)
        path = os.path.join(paths.MODELS, name)
        if not os.path.isfile(path):
            raise ValueError(tr("нет такой модели"))
        used = self.selected_model() == name
        rest = [m for m in installed_models() if m != name]
        busy = used and self.runtime.launcher is self.cosy   # на Windows сервер держит свой .gguf открытым
        async with self.runtime.paused() if busy else contextlib.nullcontext():
            await asyncio.to_thread(os.remove, path)
            if used:
                self.s.update(model=rest[0] if rest else "")
        log.info("модель %s удалена%s", quant, f", вариант CosyVoice3 — {rest[0]}" if used and rest else "")

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

    async def _download_espeech(self, session, st):
        cuda = cuda_archives() if st["cuda"] else []
        self.dl.update(total=st["size"], done=espeech_download.bytes_present() +
                       (_present(cuda, self.cosy.cuda) if cuda else 0))
        steps = espeech_download.Steps(self, session)
        if cuda:   # CUDA не скачалась перед ним (очередь): cuBLAS для torch — из CUDA cosyvoice-server
            await self._get_cuda(session)
        if st["runtime"] != "ok":
            await steps.runtime(self._espeech_swap)
        if not st["model"]:
            await steps.model()

    async def _espeech_swap(self, tmp):
        """Собранный рантайм — на место прежнего (сервер ESpeech держит его файлы, если запущен)."""
        busy = self.runtime.launcher is self.espeech
        async with self.runtime.paused() if busy else contextlib.nullcontext():
            await asyncio.to_thread(_replace_dir, tmp, espeech_launch.RUNTIME)

    async def delete_espeech(self):
        if not self.espeech_state()["offer"]:
            raise ValueError(tr("ESpeech здесь не скачивается"))
        busy = self.runtime.launcher is self.espeech
        async with self.runtime.paused() if busy else contextlib.nullcontext():
            await asyncio.to_thread(espeech_download.remove)
        log.info("ESpeech удалён")

    # --- Supertonic 3 ---
    def supertonic_state(self):
        return supertonic_download.state(bool(self.supertonic))

    async def delete_supertonic(self):
        if not self.supertonic_state()["offer"]:
            raise ValueError(tr("Supertonic 3 здесь не скачивается"))
        busy = self.runtime.launcher is self.supertonic   # сервер держит открытыми файлы модели
        async with self.runtime.paused() if busy else contextlib.nullcontext():
            await asyncio.to_thread(supertonic_download.remove)
        log.info("Supertonic 3 удалён вместе с готовыми голосами")

    # --- пакеты ---
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

    def _push(self, force=False):
        now = time.monotonic()
        if force or now - self._last_push > 0.5:
            self._last_push = now
            self.changed()

    # --- вариант CosyVoice3 (вкладка «Сервер») ---
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


def samples():
    """Примеры звучания вариантов, которые есть (static/samples/<вариант>.mp3): без примера кнопки нет."""
    try:
        return sorted(f[:-4] for f in os.listdir(os.path.join(paths.STATIC, "samples")) if f.endswith(".mp3"))
    except OSError:
        return []


def disk():
    """Свободное место там, куда качаются модели: where — диск (Windows), том (Docker) или ""."""
    d = os.path.abspath(paths.MODELS)
    while not os.path.isdir(d) and os.path.dirname(d) != d:   # папки моделей ещё нет
        d = os.path.dirname(d)
    try:
        free = shutil.disk_usage(d).free
    except OSError:
        free = None
    where = os.path.splitdrive(os.path.abspath(paths.MODELS))[0] if os.name == "nt" else paths.MODELS if DOCKER else ""
    return dict(free=free, where=where, docker=DOCKER)


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
