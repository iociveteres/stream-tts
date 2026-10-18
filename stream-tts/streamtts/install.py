"""Первый запуск через админку (режим managed): загрузка модели и установка наборов голосов.

Модель качается как в scripts/download-model.sh: Lourdle/Fun-CosyVoice3-0.5B-2512-GGUF с Hugging Face, квант на
выбор (по умолчанию Q8_0), по желанию ONNX-фронтенд int8 (нужен только для извлечения голоса из аудио, для озвучки
не нужен; он же — дополнительный пакет из PACKAGES, качается и удаляется из настроек админки). Другие пакеты
(htdemucs для очистки образца от музыки, separate.py) — из своих репозиториев Hugging Face. Файл пишется в <имя>.part
с докачкой (Range) и повторами, сверяется с sha256 из x-linked-etag и только потом переименовывается. Папка
моделей — paths.MODELS.

CUDA (ggml-cuda.dll и рантайм CUDA 13) в установщик не входит: это два архива из релиза llama.cpp той же сборки,
что и ядро GGML (адреса, размеры и sha256 — в downloads.json, cuda.archives; там же закреплены кванты модели для
установщика). Если в системе есть драйвер NVIDIA (nvcuda.dll), а ggml-cuda.dll рядом с cosyvoice-server нет,
вкладка «Сервер» предлагает скачать их: из архивов берутся только нужные файлы (cuda.files), они распаковываются в
папку сервера при остановленном сервере, версия пишется в cuda-version.txt (по ней установщик при обновлении
решает, качать ли CUDA заново), потом сервер перезапускается и заново ищет устройства.

Набор голосов — zip с *.gguf и manifest.json (ключи — имена голосов: title, post…) в корне или в одной папке.
Имя набора — эта папка или имя zip; оно же группа на странице голосов (voices.GROUPS). Набор ставится в
paths.VOICES/<набор>/ целиком, старая версия заменяется. Голос с именем, которое уже есть в другом наборе,
пропускается. Пока файлы меняются, cosyvoice-server остановлен (на Windows он держит открытые .gguf), потом
запускается с новым списком голосов."""
import asyncio, contextlib, hashlib, json, logging, os, re, shutil, time, zipfile

import aiohttp

from . import paths
from .i18n import tr

log = logging.getLogger("install")

# Закреплённые загрузки (их же берёт установщик, build-exe.py передаёт их в StreamTTS.iss)
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

REPO = "Lourdle/Fun-CosyVoice3-0.5B-2512-GGUF"
BASE = f"https://huggingface.co/{REPO}/resolve/main"
# Кванты на выбор (полный список — scripts/download-model.sh --list): размер файла в байтах и видеопамять
# cosyvoice-server на CUDA, МБ — обычно и на самых длинных образцах (замеры на RTX 3060 Ti, 2026-10-17;
# на Vulkan пик примерно на 0,5 ГБ больше). На 54 фразах (6 голосов × 3 seed) разборчивость по Whisper и
# сходство CAM++ у всех трёх в пределах шума. Ниже Q5 не проверяли, F16 не лучше Q8_0 и вдвое больше.
# Примеры звука — static/quants/<голос>-<квант>.mp3 (одна фраза, seed 42), те же файлы берёт установщик. Голос
# ohf-denis — из датасета ru_RU-denis (OHF-Voice/voice-datasets, CC0), чтобы примеры можно было распространять;
# как он собран — docs/stream-tts-deploy.md, «Первый запуск».
QUANTS = {
    "Q8_0": dict(vram=1500, vram_max=1900),
    "Q6_K": dict(vram=1450, vram_max=1800),
    "Q5_K_M": dict(vram=1300, vram_max=1700),
}
for _q, _d in QUANTS.items():
    _d["size"] = DOWNLOADS["model"]["quants"][_q]["size"]
DEFAULT_QUANT = "Q8_0"  # рекомендация автора модели
SAMPLES = (("ohf-denis", "Пример"),)  # голос в установщике — SampleVoice в StreamTTS.iss
SAMPLE_PHRASE = "Ну всё, я пошёл в бой! Если не вернусь — считайте, что я был прав, а вы нет. Вперёд!"
# Квант, выбранный в установщике (StreamTTS.iss): при первом запуске без модели он качается сам
SETUP_CHOICE = os.path.join(paths.APP, "setup-model.txt")
FRONTEND = ("frontend-onnx/campplus.int8.onnx", "frontend-onnx/speech_tokenizer_v3.int8.onnx")
# Необязательные пакеты: качаются из админки (настройки и редактор голоса). files — пути в репозитории source на
# Hugging Face, локально — paths.MODELS/<into>/<путь>. size — байт. alt — те же файлы в полной точности
# (scripts/download-model.sh --frontend full): тоже считаются установленными. restart — файлы читает
# cosyvoice-server: после загрузки он перезапускается, а удаляются они при остановленном сервере.
PACKAGES = {
    "frontend": dict(
        title="Создание голосов из записи",
        about="ONNX-фронтенд CosyVoice3: речевой токенизатор и CAM++ (отпечаток голоса). Нужен, чтобы сделать "
              "голос из записи; для озвучки не нужен.",
        files=FRONTEND, size=252891972, license="Apache-2.0", source=REPO, restart=True,
        alt=("frontend-onnx/campplus.onnx", "frontend-onnx/speech_tokenizer_v3.onnx")),
    # Веса Demucs обучены на MusDB: автор называет их исследовательскими (facebookresearch/demucs#267, #327, #508),
    # код Demucs и экспорт в ONNX — MIT. Поэтому пакет не входит в установщик, а качается по кнопке, с пометкой.
    "music": dict(
        title="Очистка образца от музыки",
        about="Demucs v4 (htdemucs, int8): отделяет голос от музыки — галочка «убрать музыку» у варианта образца в "
              "редакторе голоса. Работает на процессоре: 20 секунд записи — около 10 секунд.",
        files=("htdemucs_int8.onnx",), into="demucs", size=84108789, source="HRSadeghi/demucs-onnx",
        license="веса — только для исследований"),
}
CHUNK = 1 << 20
RETRIES = 5

SET_NAME = re.compile(r"[^a-z0-9_-]+")
VOICE_NAME = re.compile(r"^[\w.-]{1,64}$")
MAX_GGUF = 64 << 20        # голос ~120 КБ; больше — точно не голос
MAX_ZIP_FILES = 2000


def package_files(pid):
    """[(url, путь от paths.MODELS)] файлов пакета."""
    p = PACKAGES[pid]
    return [(f"https://huggingface.co/{p['source']}/resolve/main/{f}", f"{p['into']}/{f}" if p.get("into") else f)
            for f in p["files"]]


def model_name(quant):
    return f"CosyVoice3-2512_{quant}.gguf"


def installed_models():
    try:
        return sorted(f for f in os.listdir(paths.MODELS) if f.endswith(".gguf"))
    except OSError:
        return []


class Cancelled(Exception):
    pass


class Installer:
    def __init__(self, settings, runtime, changed):
        self.s, self.runtime, self.changed = settings, runtime, changed  # changed() — разбудить админку
        self.task = None
        self.dl = dict(state="idle", quant="", package="", file="", done=0, total=0, speed=0.0, error="")
        self._last_push = 0.0

    # --- состояние для админки ---
    def public(self):
        quants = [dict(QUANTS[q], quant=q, file=model_name(q)) for q in QUANTS]
        samples = [(voice, tr(label)) for voice, label in SAMPLES]
        return dict(quants=quants, default_quant=DEFAULT_QUANT, samples=samples, sample_phrase=SAMPLE_PHRASE,
                    models=installed_models(),
                    model=os.path.basename(self.runtime.model) if self.runtime.model else self.s.get("model", ""),
                    models_dir=paths.MODELS, voices_dir=paths.VOICES, download=dict(self.dl), sets=list_sets(),
                    packages=packages(), cuda=self.cuda_state())

    # --- CUDA ---
    def cuda_state(self):
        """CUDA для вкладки «Сервер»: offer — предложить скачать (есть драйвер NVIDIA, а ggml-cuda.dll нет)."""
        d = self.runtime.dir
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
        d = self.runtime.dir
        archives = cuda_archives()
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                self.dl.update(total=sum(a[2] for a in archives),
                               done=sum(_part_size(os.path.join(d, a[1])) for a in archives))
                for url, name, size, sha, _ in archives:
                    dst = os.path.join(d, name)
                    if not (os.path.isfile(dst) and os.path.getsize(dst) == size):  # скачан в прошлый раз
                        await self._fetch(session, url, name, dst, size, sha)
                    else:
                        self.dl["done"] += size
            self.dl.update(file=tr("{file}: распаковка", file="CUDA"), speed=0.0)
            self._push(force=True)
            async with self.runtime.paused():  # сервер держит открытыми ggml-*.dll
                await asyncio.to_thread(_extract_cuda, archives, d)
                self.runtime.backends = None  # после перезапуска устройства ищутся заново: появится CUDA
            self.dl.update(state="done", file="", speed=0.0)
            log.info("CUDA %s установлена в %s", CUDA["version"], d)
        except asyncio.CancelledError:
            self.dl.update(state="cancelled", speed=0.0, error="")
            log.info("загрузка CUDA отменена (докачается со следующей попытки)")
        except Exception as e:
            self.dl.update(state="error", speed=0.0, error=str(e) or type(e).__name__)
            log.warning("CUDA: %s", self.dl["error"])
        finally:
            self._push(force=True)

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

    def first_run(self):
        """Квант, выбранный в установщике. Установщик его скачал — он и используется (один раз на выбор: потом
        решает вкладка «Сервер»); модели нет совсем (загрузку в установщике пропустили или она не удалась) — качаем
        сами, тоже один раз (отмена не перезапускает)."""
        try:
            with open(SETUP_CHOICE, encoding="utf-8-sig") as f:
                quant = f.read().strip()
        except OSError:
            return
        if quant not in QUANTS:
            return
        if model_name(quant) in installed_models():
            if self.s.get("setup_model") != quant:
                log.info("модель %s скачана установщиком", quant)
                self.s.update(model=model_name(quant), setup_model=quant)
            return
        if installed_models() or self.s.get("setup_download"):
            return
        log.info("модель %s выбрана в установщике, качаю", quant)
        self.s.update(setup_download=quant)
        self.start_download(quant, False)

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
            if quant or PACKAGES[self.dl["package"]].get("restart"):
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

    async def delete_set(self, name):
        target = os.path.join(paths.VOICES, name)
        if name not in [s["name"] for s in list_sets()]:
            raise ValueError(tr("нет такого набора"))
        async with self.runtime.paused():
            await asyncio.to_thread(shutil.rmtree, target)
        log.info("набор %s удалён", name)


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
    # фронтенд: каждый файл в int8 или в полной точности (как выбирает runtime.py)
    alt = PACKAGES[pid].get("alt") or files
    return all(have(a) or have(b) for a, b in zip(files, alt))


def package_usable(pid):
    """Программа умеет работать с пакетом: очистке от музыки нужен onnxruntime."""
    if pid == "music":
        from . import separate
        return separate.runtime_ok()
    return True


def packages(installed=None):
    """Необязательные пакеты для админки. installed — id -> bool для пакетов, файлов которых не видно (фронтенд на
    внешнем сервере)."""
    installed = installed or {}
    return [dict(id=pid, title=tr(p["title"]), about=tr(p["about"]), size=p["size"], license=tr(p["license"]),
                 source=p["source"], restart=bool(p.get("restart")), usable=package_usable(pid),
                 dir=os.path.join(paths.MODELS, p.get("into", "")),
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


def list_sets():
    """Установленные наборы: подпапки paths.VOICES с голосами."""
    out = []
    try:
        names = sorted(os.listdir(paths.VOICES))
    except OSError:
        return out
    for n in names:
        d = os.path.join(paths.VOICES, n)
        if n.startswith(".") or not os.path.isdir(d):
            continue
        voices = sorted(f[:-5] for f in os.listdir(d) if f.endswith(".gguf"))
        if voices:
            out.append(dict(name=n, voices=voices, manifest=os.path.isfile(os.path.join(d, "manifest.json"))))
    return out


def _existing_voices(skip_set):
    """Голос -> где лежит (набор или корень), кроме заменяемого набора."""
    out = {}
    for f in os.listdir(paths.VOICES) if os.path.isdir(paths.VOICES) else []:
        if f.endswith(".gguf"):
            out[f[:-5]] = tr("голоса без набора")
    for s in list_sets():
        if s["name"] != skip_set:
            out.update(dict.fromkeys(s["voices"], tr("набор {name}", name=s["name"])))
    return out


def _check_zip(path, filename):
    """Что ставить из zip: имя набора, голоса (имя -> запись в zip), manifest. Ошибка — ValueError."""
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
            if "/" in rel:  # только верхний уровень набора
                ignored.append(i.filename)
            elif base == "manifest.json":
                manifest = i.filename
            elif base.lower().endswith(".gguf"):
                voice = base[:-5]
                if not VOICE_NAME.match(voice):
                    raise ValueError(tr("недопустимое имя голоса: {voice}", voice=voice))
                if i.file_size > MAX_GGUF:
                    raise ValueError(tr("{file}: слишком большой для голоса", file=base))
                with z.open(i) as f:
                    if f.read(4) != b"GGUF":
                        raise ValueError(tr("{file}: не GGUF", file=base))
                voices[voice] = i.filename
            else:
                ignored.append(i.filename)
        if not voices:
            raise ValueError(tr("в архиве нет голосов *.gguf"))
        meta = {}
        if manifest:
            try:
                meta = json.loads(z.read(manifest).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                raise ValueError(f"manifest.json: {e}")
            if not isinstance(meta, dict):
                raise ValueError(tr("manifest.json: ожидался объект {голос: {...}}"))
    return dict(name=name, voices=voices, manifest=meta, ignored=ignored)


def _install_set(path, info):
    name = info["name"]
    taken = _existing_voices(skip_set=name)
    added, skipped = [], [tr("{voice} (уже есть: {where})", voice=v, where=taken[v]) for v in sorted(info["voices"]) if v in taken]
    os.makedirs(paths.VOICES, exist_ok=True)
    tmp = os.path.join(paths.VOICES, f".{name}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with zipfile.ZipFile(path) as z:
        for voice, entry in sorted(info["voices"].items()):
            if voice in taken:
                continue
            with z.open(entry) as src, open(os.path.join(tmp, voice + ".gguf"), "wb") as dst:
                shutil.copyfileobj(src, dst)
            added.append(voice)
    if not added:
        shutil.rmtree(tmp)
        raise ValueError(tr("все голоса набора уже установлены в других наборах: {voices}", voices=", ".join(skipped)))
    if info["manifest"]:
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in info["manifest"].items() if k in added}, f, ensure_ascii=False, indent=1)
    target = os.path.join(paths.VOICES, name)
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.replace(tmp, target)
    return dict(name=name, added=added, skipped=skipped, ignored=info["ignored"])
