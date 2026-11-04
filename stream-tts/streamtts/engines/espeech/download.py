"""Загрузка ESpeech программой для Windows по кнопке на вкладке «Сервер»: рантайм и модель из первоисточников.

Что и откуда — downloads.json, раздел espeech (закрепляет packaging/windows/pin-espeech.py):
- рантайм → launch.RUNTIME (папка espeech-runtime рядом с программой, а не в runtime/: установщик при обновлении
  очищает runtime/ целиком, а здесь ~1,1 ГБ): встраиваемый Python 3.14 (python.org), колёса пакетов (PyPI) —
  распаковкой, без pip, и нужные файлы колеса torch диапазонами (partzip.py, ~430 МБ из 1,9 ГБ). Код espeech-server и
  DLL-заглушки ставит установщик (launch.CODE). Рядом с python.exe — CRT из рантайма cosyvoice-server: новее, чем во
  встраиваемом Python, и с vcruntime140_threads.dll для torch. Потом байткод (compileall: без него каждый запуск
  компилирует torch заново, +1 с) и проверка — импорт torch и пакетов. Собирается в <папка>.tmp и переименовывается;
  версия — хэш раздела runtime в version.txt, при другой закреплённой версии рантайм качается заново;
- модель → paths.MODELS/espeech: исходные файлы с Hugging Face в .espeech-src/ (из чекпойнта — только EMA-копия
  весов, урезанный чекпойнт собирается как zip), сборка — python -m espeech.prepare в скачанном рантайме, потом
  исходники удаляются;
- CUDA: если видеокарта NVIDIA есть, а CUDA для cosyvoice-server нет, она качается первой (cuBLAS torch берёт оттуда;
  без CUDA ESpeech работает только на процессоре).
Всё докачивается с места обрыва; прогресс — общий для загрузок (Installer.dl, install.py)."""
import asyncio, contextlib, glob, hashlib, json, logging, os, shutil, time, zipfile

import aiohttp

from . import launch
from ... import partzip, paths
from ...i18n import tr
from ...runtime import spawn_kwargs

log = logging.getLogger("install")

SRC_DIR = "espeech-src"     # исходники модели: paths.MODELS/.espeech-src
CRT = ("msvcp140*.dll", "vcruntime140*.dll", "concrt140.dll")
RUNTIME_EXTRA = 350 << 20   # рантайм без torch: Python, пакеты, байткод (замер на 3.14: 293 МБ файлов, из них
                            # sympy 83 МБ, + кластеры диска)
MODEL_DISK = 1450 << 20     # готовая папка модели (замер)
CHUNK = 1 << 20
RETRIES = 5
# Проверка рантайма: всё, что импортируют espeech-server и сборка модели; espeech — первым: он добавляет папки DLL
# (ruaccent — так, как его грузит accent.py: без transformers и huggingface_hub; torch._dynamo — как его импортирует
# torch.optim при обучении LoRA, ему нужен sympy)
SMOKE = ("import espeech, torch, numpy, safetensors, onnxruntime, tokenizers, razdel, psutil, onnx, espeech.server, "
         "torch._dynamo; "
         "from espeech import accent; accent._import_ruaccent(); "
         "print('torch', torch.__version__, 'cuda', torch.cuda.is_available())")


def spec():
    from ...install import DOWNLOADS
    return DOWNLOADS["espeech"]


def runtime_version():
    """Хэш закреплённого рантайма: другой — качать заново."""
    return hashlib.sha256(json.dumps(spec()["runtime"], sort_keys=True).encode()).hexdigest()[:16]


def runtime_state():
    """ok | missing | outdated."""
    try:
        with open(os.path.join(launch.RUNTIME, "version.txt"), encoding="utf-8") as f:
            have = f.read().strip()
    except OSError:
        return "missing"
    if not os.path.isfile(os.path.join(launch.RUNTIME, "python.exe")):
        return "missing"
    return "ok" if have == runtime_version() else "outdated"


def model_dir():
    return os.path.abspath(os.path.join(paths.MODELS, "espeech"))


def model_ready():
    return launch.model_ready(model_dir())


def runtime_disk():
    """Рантайм на диске: torch и ~RUNTIME_EXTRA на Python, пакеты и байткод."""
    return spec()["runtime"]["torch"]["unpacked"] + RUNTIME_EXTRA


def runtime_size():
    r = spec()["runtime"]
    return r["python"]["size"] + sum(w["size"] for w in r["wheels"]) + r["torch"]["download"]


def model_size():
    m = spec()["model"]
    return sum(f["size"] for f in m["files"]) + m["checkpoint"]["download"]


def offered():
    """Программа для Windows: код espeech-server поставлен установщиком, рантайм качается по кнопке."""
    return launch.downloadable()


def state(cuda):
    """Для вкладки «Сервер». cuda — состояние CUDA cosyvoice-server (install.cuda_state)."""
    if not offered():
        return dict(offer=False)
    rt, model = runtime_state(), model_ready()
    need_cuda = bool(cuda.get("offer"))
    size = (0 if rt == "ok" else runtime_size()) + (0 if model else model_size()) + (cuda["size"] if need_cuda else 0)
    disk = runtime_disk() + MODEL_DISK
    return dict(offer=True, runtime=rt, model=model, installed=rt == "ok" and model, size=size, disk=disk,
                cuda=need_cuda, runtime_dir=launch.RUNTIME, model_dir=model_dir(),
                gpu=launch.nvidia(), cublas=launch.cublas_dir() is not None)


# --- загрузка ---
class Steps:
    """Шаги загрузки ESpeech для Installer: inst — его _fetch, dl и _push."""

    def __init__(self, inst, session):
        self.inst, self.session = inst, session

    def label(self, text):
        self.inst.dl.update(file=text, speed=0.0)
        self.inst._push(force=True)

    async def fetch(self, url, name, dst, size, sha):
        if os.path.isfile(dst) and os.path.getsize(dst) == size:
            return
        await self.inst._fetch(self.session, url, name, dst, size, sha)

    async def ranges(self, url, name, part, ranges):
        """Диапазоны архива подряд в part, с места обрыва (по размеру part)."""
        dl = self.inst.dl
        dl["file"] = name
        pos = 0
        for start, end in ranges:
            n, attempt = end - start, 0
            while (have := _size(part)) < pos + n:
                if have < pos:
                    raise RuntimeError(f"{name}: файл {part} короче ожидаемого")
                try:
                    async with self.session.get(url, headers={"Range": f"bytes={start + have - pos}-{end - 1}"}) as r:
                        if r.status != 206:   # 200 — сервер отдал бы весь архив
                            raise RuntimeError(f"HTTP {r.status}")
                        t0, got0, left = time.monotonic(), dl["done"], pos + n - have
                        with open(part, "ab") as f:
                            async for chunk in r.content.iter_chunked(CHUNK):
                                chunk = chunk[:left]   # больше диапазона не пишем: следующий начнётся с конца файла
                                f.write(chunk)
                                left -= len(chunk)
                                dl["done"] += len(chunk)
                                dt = time.monotonic() - t0
                                if dt > 1:
                                    dl["speed"] = (dl["done"] - got0) / dt
                                self.inst._push()
                                if not left:
                                    break
                    if _size(part) == have:
                        raise RuntimeError(tr("пустой ответ"))
                except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
                    attempt += 1
                    if attempt == RETRIES:
                        raise RuntimeError(f"{name}: {e or type(e).__name__}") from e
                    log.warning("%s: %s, повтор %d через 3 с", name, e or type(e).__name__, attempt)
                    await asyncio.sleep(3)
            pos += n

    async def partial(self, s, part, name):
        """Нужные файлы архива s (partzip) в part: хвост, проверка, диапазоны. → диапазоны для partzip.extract."""
        tail = [(s["tail"], s["size"])]
        if _size(part) >= partzip.tail_len(s) and not partzip.check_tail(part, s):
            self.inst.dl["done"] = max(0, self.inst.dl["done"] - _size(part))
            os.remove(part)            # другой архив или битый хвост: заново
        await self.ranges(s["url"], name, part, tail)
        if not partzip.check_tail(part, s):
            os.remove(part)
            raise RuntimeError(tr("{file}: контрольная сумма не совпала, файл удалён — скачайте ещё раз", file=name))
        _, ranges = await asyncio.to_thread(partzip.plan, part, s)
        await self.ranges(s["url"], name, part, tail + ranges)
        return ranges

    # --- рантайм ---
    async def runtime(self, on_ready):
        """Скачать и собрать рантайм; on_ready(готовая папка) переносит её на место (сервер ESpeech остановлен)."""
        r = spec()["runtime"]
        dl_dir = launch.RUNTIME + ".dl"
        os.makedirs(dl_dir, exist_ok=True)
        py = os.path.join(dl_dir, _name(r["python"]["url"]))
        await self.fetch(r["python"]["url"], "Python", py, r["python"]["size"], r["python"]["sha256"])
        wheels = []
        for w in r["wheels"]:
            dst = os.path.join(dl_dir, _name(w["url"]))
            await self.fetch(w["url"], _name(w["url"]), dst, w["size"], w["sha256"])
            wheels.append(dst)
        part = os.path.join(dl_dir, "torch.part")
        ranges = await self.partial(r["torch"], part, _name(r["torch"]["url"]))

        tmp = launch.RUNTIME + ".tmp"
        self.label(tr("ESpeech: распаковка"))
        await asyncio.to_thread(_unpack, tmp, py, wheels, part, r["torch"], ranges)
        self.label(tr("ESpeech: подготовка Python"))
        # ошибки — только у файлов, которые не импортируются (py312_intrinsics.py с синтаксисом 3.12 в тестах torch);
        # в один процесс: дочерние процессы -j открыли бы окна консоли
        await self.run([os.path.join(tmp, "python.exe"), "-m", "compileall", "-q",
                        os.path.join(tmp, "Lib", "site-packages")], tmp, check=False, quiet=True)
        self.label(tr("ESpeech: проверка"))
        await self.run([os.path.join(tmp, "python.exe"), "-c", SMOKE], tmp)
        with open(os.path.join(tmp, "version.txt"), "w", encoding="utf-8") as f:
            f.write(runtime_version())
        await on_ready(tmp)
        shutil.rmtree(dl_dir, ignore_errors=True)

    async def run(self, args, runtime_dir, check=True, quiet=False):
        """Процесс рантайма ESpeech (python.exe из runtime_dir) с выводом в лог (quiet — без); ошибка — RuntimeError
        с последней строкой вывода."""
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=runtime_dir, env=launch.process_env(runtime_dir), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **spawn_kwargs())
        lines = []
        try:
            while line := await proc.stdout.readline():
                text = line.decode(errors="replace").rstrip()
                if text:
                    lines.append(text)
                    if not quiet:
                        log.info("espeech: %s", text)
            await proc.wait()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise
        if check and proc.returncode:
            raise RuntimeError(tr("{what} завершился с кодом {code}", what=os.path.basename(args[0]),
                                  code=proc.returncode) + (": " + lines[-1] if lines else ""))
        return "\n".join(lines)

    # --- модель ---
    async def model(self):
        m = spec()["model"]
        out = os.path.abspath(model_dir())   # сборка идёт в папке рантайма
        src = os.path.abspath(os.path.join(paths.MODELS, "." + SRC_DIR))
        for f in m["files"]:
            await self.fetch(f["url"], f["path"].rsplit("/", 1)[-1], os.path.join(src, *f["path"].split("/")),
                             f["size"], f["sha256"])
        ck = m["checkpoint"]
        dst = os.path.join(src, *ck["path"].split("/"))
        if not os.path.isfile(dst):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            part = dst + ".ranges"
            ranges = await self.partial(ck, part, _name(ck["url"]))
            self.label(tr("ESpeech: распаковка"))
            await asyncio.to_thread(_slim_checkpoint, part, ck, ranges, dst)
            os.remove(part)
        self.label(tr("ESpeech: сборка модели"))
        await self.run([os.path.join(launch.RUNTIME, "python.exe"), "-m", "espeech.prepare", "--src", src, out],
                       launch.RUNTIME)
        if not model_ready():
            raise RuntimeError(tr("модель ESpeech собрана не полностью"))
        shutil.rmtree(src, ignore_errors=True)


def bytes_present():
    """Сколько уже скачано (для прогресса после обрыва): файлы целиком и .part."""
    n = 0
    if runtime_state() != "ok":
        n += sum(_size(p) for p in glob.glob(os.path.join(launch.RUNTIME + ".dl", "*")))
    if not model_ready():
        src = os.path.join(paths.MODELS, "." + SRC_DIR)
        ck = spec()["model"]["checkpoint"]
        slim = os.path.join(src, *ck["path"].split("/"))
        for root, _, files in os.walk(src):
            n += sum(_size(os.path.join(root, f)) for f in files if os.path.join(root, f) != slim)
        if os.path.isfile(slim):   # урезанный чекпойнт уже собран: его часть скачана целиком
            n += ck["download"]
    return n


# --- распаковка (в потоке) ---
def _unpack(tmp, py, wheels, part, torch_spec, ranges):
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    with zipfile.ZipFile(py) as z:
        z.extractall(tmp)
    site = os.path.join(tmp, "Lib", "site-packages")
    os.makedirs(site)
    for w in wheels:
        _unpack_wheel(w, site)

    def sink(info, src):
        dst = os.path.join(site, *info.filename.split("/"))
        if not os.path.abspath(dst).startswith(os.path.abspath(site) + os.sep):
            raise RuntimeError(f"torch: недопустимый путь {info.filename}")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "wb") as f:
            shutil.copyfileobj(src, f, CHUNK)

    partzip.extract(part, torch_spec, ranges, sink)
    for mask in CRT:   # CRT новее, чем во встраиваемом Python, и с vcruntime140_threads.dll (torch_cpu.dll)
        for f in glob.glob(os.path.join(paths.RUNTIME, mask)):
            shutil.copy(f, tmp)
    missing = [f for f in ("vcruntime140_threads.dll", "msvcp140.dll") if not os.path.isfile(os.path.join(tmp, f))]
    if missing:
        raise RuntimeError(tr("нет {files} в {dir}: переустановите программу", files=", ".join(missing),
                              dir=paths.RUNTIME))
    launch.write_pth(tmp)


def _unpack_wheel(path, site):
    """Колесо — распаковкой: <пакет>-<версия>.data/purelib|platlib — в site-packages, прочее из .data (скрипты,
    заголовки) не нужно."""
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            name = info.filename
            parts = name.split("/")
            if parts[0].endswith(".data"):
                if len(parts) < 3 or parts[1] not in ("purelib", "platlib"):
                    continue
                parts = parts[2:]
            if info.is_dir() or not parts[-1]:
                continue
            dst = os.path.join(site, *parts)
            if ".." in parts or not os.path.abspath(dst).startswith(os.path.abspath(site) + os.sep):
                raise RuntimeError(f"{os.path.basename(path)}: недопустимый путь {name}")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with z.open(info) as src, open(dst, "wb") as f:
                shutil.copyfileobj(src, f, CHUNK)


def _slim_checkpoint(part, ck, ranges, dst):
    """Урезанный чекпойнт: выбранные файлы (EMA-копия весов и data.pkl) — новым zip без сжатия."""
    tmp = dst + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as out:
        def sink(info, src):
            with out.open(zipfile.ZipInfo(info.filename, info.date_time), "w", force_zip64=True) as f:
                shutil.copyfileobj(src, f, CHUNK)
        partzip.extract(part, ck, ranges, sink)
    os.replace(tmp, dst)


def _name(url):
    from urllib.parse import unquote
    return unquote(url.rsplit("/", 1)[-1])


def _size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def remove():
    """Удалить рантайм и модель (сервер ESpeech остановлен)."""
    for d in (launch.RUNTIME, launch.RUNTIME + ".tmp", launch.RUNTIME + ".dl", model_dir(),
              os.path.join(paths.MODELS, "." + SRC_DIR)):
        shutil.rmtree(d, ignore_errors=True)


def check_space(st):
    """Места хватит на загрузку: скачанное и собранное лежат вместе, пока исходники не удалены. Иначе ValueError."""
    need = st["size"] + (0 if st["runtime"] == "ok" else runtime_disk()) + (0 if st["model"] else MODEL_DISK)
    try:
        os.makedirs(paths.MODELS, exist_ok=True)
        free = min(shutil.disk_usage(paths.MODELS).free, shutil.disk_usage(os.path.dirname(launch.RUNTIME)).free)
    except OSError:
        return
    if free < need:
        raise ValueError(tr("для ESpeech нужно {need} ГБ свободного места, а есть {free} ГБ",
                            need=f"{need / 2**30:.1f}", free=f"{free / 2**30:.1f}"))
