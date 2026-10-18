"""Первый запуск через админку (режим managed): загрузка модели и установка наборов голосов.

Модель качается как в scripts/download-model.sh: Lourdle/Fun-CosyVoice3-0.5B-2512-GGUF с Hugging Face, квант на
выбор (по умолчанию Q8_0), по желанию ONNX-фронтенд int8 (нужен только для извлечения голоса из аудио, для озвучки
не нужен). Файл пишется в <имя>.part с докачкой (Range) и повторами, сверяется с sha256 из x-linked-etag и только
потом переименовывается. Папка моделей — paths.MODELS.

Набор голосов — zip с *.gguf и manifest.json (ключи — имена голосов: title, post…) в корне или в одной папке.
Имя набора — эта папка или имя zip; оно же группа на странице голосов (voices.GROUPS). Набор ставится в
paths.VOICES/<набор>/ целиком, старая версия заменяется. Голос с именем, которое уже есть в другом наборе,
пропускается. Пока файлы меняются, cosyvoice-server остановлен (на Windows он держит открытые .gguf), потом
запускается с новым списком голосов."""
import asyncio, hashlib, json, logging, os, re, shutil, time, zipfile

import aiohttp

from . import paths

log = logging.getLogger("install")

REPO = "Lourdle/Fun-CosyVoice3-0.5B-2512-GGUF"
BASE = f"https://huggingface.co/{REPO}/resolve/main"
# Кванты на выбор (полный список — scripts/download-model.sh --list): размер файла в байтах и видеопамять
# cosyvoice-server на CUDA, МБ — обычно и на самых длинных образцах (замеры на RTX 3060 Ti, 2026-10-17;
# на Vulkan пик примерно на 0,5 ГБ больше). На 54 фразах (6 голосов × 3 seed) разборчивость по Whisper и
# сходство CAM++ у всех трёх в пределах шума. Ниже Q5 не проверяли, F16 не лучше Q8_0 и вдвое больше.
# Примеры звука — static/quants/<голос>-<квант>.mp3 (одна фраза, seed 42), те же файлы берёт установщик. Голос
# ohf-denis — из датасета ru_RU-denis (OHF-Voice/voice-datasets, CC0), чтобы примеры можно было распространять;
# как он собран — docs/stream-tts.md.
QUANTS = {
    "Q8_0": dict(size=943933536, vram=1500, vram_max=1900, note="Лучшее качество, рекомендация автора модели"),
    "Q6_K": dict(size=839539424, vram=1450, vram_max=1800, note="На слух как Q8_0, видеопамяти на ~0,1 ГБ меньше"),
    "Q5_K_M": dict(size=682814464, vram=1300, vram_max=1700,
                   note="Для видеокарт с 4 ГБ: видеопамяти на ~0,2 ГБ меньше"),
}
DEFAULT_QUANT = "Q8_0"  # рекомендация автора модели
SAMPLES = (("ohf-denis", "Пример"),)  # голос в установщике — SampleVoice в StreamTTS.iss
SAMPLE_PHRASE = "Ну всё, я пошёл в бой! Если не вернусь — считайте, что я был прав, а вы нет. Вперёд!"
# Квант, выбранный в установщике (StreamTTS.iss): при первом запуске без модели он качается сам
SETUP_CHOICE = os.path.join(paths.APP, "setup-model.txt")
FRONTEND = ("frontend-onnx/campplus.int8.onnx", "frontend-onnx/speech_tokenizer_v3.int8.onnx")
CHUNK = 1 << 20
RETRIES = 5

SET_NAME = re.compile(r"[^a-z0-9_-]+")
VOICE_NAME = re.compile(r"^[\w.-]{1,64}$")
MAX_GGUF = 64 << 20        # голос ~120 КБ; больше — точно не голос
MAX_ZIP_FILES = 2000


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
        self.dl = dict(state="idle", quant="", file="", done=0, total=0, speed=0.0, error="")
        self._last_push = 0.0

    # --- состояние для админки ---
    def public(self):
        quants = [dict(QUANTS[q], quant=q, file=model_name(q)) for q in QUANTS]
        return dict(quants=quants, default_quant=DEFAULT_QUANT, samples=SAMPLES, sample_phrase=SAMPLE_PHRASE,
                    models=installed_models(),
                    model=os.path.basename(self.runtime.model) if self.runtime.model else self.s.get("model", ""),
                    models_dir=paths.MODELS, voices_dir=paths.VOICES, download=dict(self.dl), sets=list_sets())

    def _push(self, force=False):
        now = time.monotonic()
        if force or now - self._last_push > 0.5:
            self._last_push = now
            self.changed()

    # --- модель ---
    def start_download(self, quant, frontend):
        if quant not in QUANTS:
            raise ValueError(f"неизвестный квант {quant}")
        if self.task and not self.task.done():
            raise ValueError("загрузка уже идёт")
        files = [(model_name(quant), model_name(quant))]
        if frontend:
            files += [(f, f) for f in FRONTEND]
        self.dl = dict(state="running", quant=quant, file="", done=0, total=0, speed=0.0, error="")
        self.task = asyncio.create_task(self._download(quant, files))

    def first_run(self):
        """Модели нет, а в установщике выбран квант: качаем его сам, один раз (отмена не перезапускает)."""
        if installed_models() or self.s.get("setup_download"):
            return
        try:
            with open(SETUP_CHOICE, encoding="utf-8-sig") as f:
                quant = f.read().strip()
        except OSError:
            return
        if quant in QUANTS:
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
                plan = []  # (rel, dst, size, sha256)
                for rel, local in files:
                    dst = os.path.join(paths.MODELS, *local.split("/"))
                    size, sha = await self._head(session, rel)
                    if os.path.isfile(dst) and (not size or os.path.getsize(dst) == size):
                        continue
                    plan.append((rel, dst, size, sha))
                self.dl["total"] = sum(p[2] for p in plan)
                self.dl["done"] = sum(_part_size(p[1]) for p in plan)
                for rel, dst, size, sha in plan:
                    await self._fetch(session, rel, dst, size, sha)
            self.dl.update(state="done", file="", speed=0.0)
            log.info("модель %s скачана", quant)
            self.s.update(model=model_name(quant))
            await self.runtime.restart()
        except asyncio.CancelledError:
            self.dl.update(state="cancelled", speed=0.0, error="")
            log.info("загрузка модели отменена (докачается со следующей попытки)")
        except Exception as e:
            self.dl.update(state="error", speed=0.0, error=str(e) or type(e).__name__)
            log.warning("загрузка модели: %s", self.dl["error"])
        finally:
            self._push(force=True)

    async def _head(self, session, rel):
        """Размер и sha256 файла: у LFS-файлов Hugging Face отдаёт их в x-linked-size и x-linked-etag до редиректа."""
        async with session.head(f"{BASE}/{rel}", allow_redirects=False) as r:
            if r.status >= 400:
                raise RuntimeError(f"{rel}: HTTP {r.status}")
            size = int(r.headers.get("x-linked-size") or r.headers.get("content-length") or 0)
            sha = r.headers.get("x-linked-etag", "").strip('"')
            return size, sha if re.fullmatch(r"[0-9a-f]{64}", sha) else ""

    async def _fetch(self, session, rel, dst, size, sha):
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
                async with session.get(f"{BASE}/{rel}", headers=headers) as r:
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
            self.dl.update(file=f"{rel}: проверка", speed=0.0)
            self._push(force=True)
            got = await asyncio.to_thread(_sha256, part)
            if got != sha:
                os.remove(part)
                raise RuntimeError(f"{rel}: контрольная сумма не совпала, файл удалён — скачайте ещё раз")
        os.replace(part, dst)

    def use_model(self, name):
        if name not in installed_models():
            raise ValueError("нет такой модели")
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
            raise ValueError("нет такого набора")
        async with self.runtime.paused():
            await asyncio.to_thread(shutil.rmtree, target)
        log.info("набор %s удалён", name)


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
            out[f[:-5]] = "голоса без набора"
    for s in list_sets():
        if s["name"] != skip_set:
            out.update(dict.fromkeys(s["voices"], f"набор {s['name']}"))
    return out


def _check_zip(path, filename):
    """Что ставить из zip: имя набора, голоса (имя -> запись в zip), manifest. Ошибка — ValueError."""
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ValueError("это не zip")
    with z:
        entries = [i for i in z.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
        if len(entries) > MAX_ZIP_FILES:
            raise ValueError("слишком много файлов в архиве")
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
                    raise ValueError(f"недопустимое имя голоса: {voice}")
                if i.file_size > MAX_GGUF:
                    raise ValueError(f"{base}: слишком большой для голоса")
                with z.open(i) as f:
                    if f.read(4) != b"GGUF":
                        raise ValueError(f"{base}: не GGUF")
                voices[voice] = i.filename
            else:
                ignored.append(i.filename)
        if not voices:
            raise ValueError("в архиве нет голосов *.gguf")
        meta = {}
        if manifest:
            try:
                meta = json.loads(z.read(manifest).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                raise ValueError(f"manifest.json: {e}")
            if not isinstance(meta, dict):
                raise ValueError("manifest.json: ожидался объект {голос: {...}}")
    return dict(name=name, voices=voices, manifest=meta, ignored=ignored)


def _install_set(path, info):
    name = info["name"]
    taken = _existing_voices(skip_set=name)
    added, skipped = [], [f"{v} (уже есть: {taken[v]})" for v in sorted(info["voices"]) if v in taken]
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
        raise ValueError("все голоса набора уже установлены в других наборах: " + ", ".join(skipped))
    if info["manifest"]:
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in info["manifest"].items() if k in added}, f, ensure_ascii=False, indent=1)
    target = os.path.join(paths.VOICES, name)
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.replace(tmp, target)
    return dict(name=name, added=added, skipped=skipped, ignored=info["ignored"])
