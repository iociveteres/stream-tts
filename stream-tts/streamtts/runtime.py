"""Встроенный cosyvoice-server (режим managed): stream-tts сам запускает сервер на 127.0.0.1 и следит за ним.

Включается, если COSYVOICE_URL не задан, а cosyvoice-server(.exe) лежит в рантайме (paths.RUNTIME, рядом с
программой) или путь к нему (или к его папке) указан в COSYVOICE_SERVER. Иначе режим external, как раньше.

Аргументы собираются как в cosyvoice/entrypoint.sh:
- без AVX2 рантайм cosyvoice.cpp не работает — сервер не запускается;
- модель: MODEL, иначе выбранная в админке (settings.model), иначе единственный *.gguf в paths.MODELS
  (нет модели — ждём, пока появится: её можно скачать в админке, install.py);
- голоса: paths.VOICES/*.gguf и paths.VOICES/<набор>/*.gguf, имя голоса — имя файла;
- ONNX-фронтенд из paths.MODELS/frontend-onnx (полные версии приоритетнее int8): флаги плюс
  POST /frontend/model/load после старта, потому что в режиме --webui флаги только заполняют форму;
- бэкенд (COSYVOICE_BACKEND=auto|cuda|vulkan|cpu, иначе выбор в админке, по умолчанию auto) — по выводу
  cosyvoice-cli --list-backends:
  CUDA, если есть ggml-cuda.dll и видеокарта NVIDIA, иначе Vulkan (дискретная видеокарта приоритетнее
  встроенной), иначе CPU. Набор, где лежат и ggml-cuda, и ggml-vulkan, на машине без них уходит на CPU;
- всегда GGML_CUDA_DISABLE_GRAPHS=1, а на CUDA ещё --llm-flash-attn 0: без них вместо речи шум (AGENTS.md).
Порт: COSYVOICE_PORT или любой свободный. Упавший сервер перезапускается (пауза растёт до минуты, если он
падает сразу после старта). Сервер завершается вместе с stream-tts: на Windows через Job Object (даже если
stream-tts убит), на Linux через PR_SET_PDEATHSIG."""
import asyncio, collections, contextlib, glob, logging, os, re, socket, subprocess, sys

import aiohttp

from . import paths
from .paths import EXE, env

log = logging.getLogger("runtime")
srv_log = logging.getLogger("cosyvoice")

BACKENDS = ("auto", "cuda", "vulkan", "cpu")


class Unavailable(Exception):
    """Сервер пока нельзя запустить (нет модели, нет AVX2): причина показывается в админке."""


def find_server():
    """Путь к cosyvoice-server для режима managed или None."""
    p = env("COSYVOICE_SERVER")
    if p:
        return os.path.join(p, "cosyvoice-server" + EXE) if os.path.isdir(p) else p
    for d in (paths.RUNTIME, paths.APP):
        p = os.path.join(d, "cosyvoice-server" + EXE)
        if os.path.isfile(p):
            return p
    return None


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def has_avx2():
    if os.name == "nt":
        import ctypes
        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))  # PF_AVX2_INSTRUCTIONS_AVAILABLE
    try:
        with open("/proc/cpuinfo") as f:
            return " avx2" in f.read()
    except OSError:
        return True  # не Linux: пусть решает сам сервер


def first_existing(*files):
    return next((f for f in files if os.path.isfile(f)), None)


def parse_backends(text):
    """Вывод cosyvoice-cli --list-backends: [(имя, встроенная ли видеокарта)] в порядке вывода."""
    uma = {m[1]: m[2] == "1" for m in re.finditer(r"ggml_vulkan: (\d+) = .*?\buma: (\d)", text)}
    out, listing = [], False
    for line in text.splitlines():
        if line.startswith("Available GGML backends"):
            listing = True
        elif listing and line.startswith((" ", "\t")) and line.strip():
            name = line.split()[0]
            m = re.fullmatch(r"vulkan(\d+)", name.lower())
            out.append((name, bool(m and uma.get(m[1]))))
    return out


def choose_backend(found, prefer="auto"):
    """Имя бэкенда для --backend: CUDA, потом Vulkan (дискретная раньше встроенной), потом CPU."""
    names = [n for n, _ in found]
    cuda = [n for n in names if n.lower().startswith("cuda")]
    vulkan = sorted((n for n, _ in found if n.lower().startswith("vulkan")), key=lambda n: dict(found)[n])
    order = {"auto": cuda + vulkan, "cuda": cuda, "vulkan": vulkan, "cpu": []}[prefer]
    return order[0] if order else "CPU"


def backend_args(name):
    kind = name.lower()
    if kind.startswith("cuda"):
        # flash attention в LLM на CUDA даёт лишние звуки в начале фразы (docker-compose.gpu.yml)
        return ["--backend", kind, "--llm-flash-attn", "0"]
    if kind.startswith("vulkan"):
        return ["--backend", kind]
    return ["--cpu"]


class _Job:
    """Job Object Windows с KILL_ON_JOB_CLOSE: процессы в нём завершаются, когда закрывается stream-tts."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ct, k = ctypes, ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        k.OpenProcess.restype = w.HANDLE
        k.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        k.CloseHandle.argtypes = [w.HANDLE]

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("IoInfo", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.k, self.job = k, k.CreateJobObjectW(None, None)
        info = Extended()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.job or not k.SetInformationJobObject(self.job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "Job Object")

    def add(self, pid):
        h = self.k.OpenProcess(0x0101, False, pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
        try:
            if not h or not self.k.AssignProcessToJobObject(self.job, h):
                raise OSError(self.ct.get_last_error(), "AssignProcessToJobObject")
        finally:
            if h:
                self.k.CloseHandle(h)


def _die_with_parent():
    """Linux: сервер получает SIGTERM, когда завершается stream-tts (preexec_fn)."""
    import ctypes, signal
    ctypes.CDLL(None).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


class Managed:
    def __init__(self, server, settings=None):
        self.server, self.dir = server, os.path.dirname(os.path.abspath(server))
        self.s = settings
        self.port = int(env("COSYVOICE_PORT", "0")) or free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.backend = ""          # выбранный бэкенд: CUDA0, Vulkan0, CPU
        self.backends = None       # [(имя, встроенная)] из --list-backends, на время работы stream-tts
        self.model = ""
        self.voices = 0
        self.state = "stopped"     # waiting (нет модели и т. п.) | starting | running | crashed | stopped
        self.error = ""
        self.restarts = 0
        self.starts = 0            # сколько раз сервер становился готов: stream-tts перечитывает голоса
        self.tail = collections.deque(maxlen=40)  # последние строки вывода сервера: причина падения
        self.frontend = None       # (speech_tokenizer, campplus) для POST /frontend/model/load
        self.proc = None
        self.task = None
        self.override = ""         # устройство на время проверки скорости (bench.py), в настройки не пишется
        self._restarting = False
        self._hold = 0             # paused(): сервер остановлен, пока меняются файлы голосов
        self._wake = None          # asyncio.Event: перезапуск без ожидания
        self._job = None
        if os.name == "nt":
            try:
                self._job = _Job()
            except OSError as e:
                log.warning("Job Object не создан, сервер может пережить stream-tts: %s", e)

    @property
    def prefer(self):
        """Бэкенд: COSYVOICE_BACKEND, иначе проверка скорости, иначе выбор в админке, иначе auto."""
        v = (env("COSYVOICE_BACKEND") or self.override or (self.s.get("backend", "") if self.s else "")
             or "auto").lower()
        return v if v in BACKENDS else "auto"

    def public(self):
        return dict(mode="managed", backend=self.backend, prefer=self.prefer, state=self.state, error=self.error,
                    model=os.path.basename(self.model), voices=self.voices, restarts=self.restarts,
                    backends=[n for n, _ in self.backends or []], server=self.server,
                    locked=dict(backend=bool(env("COSYVOICE_BACKEND")), model=bool(env("MODEL"))),
                    pid=self.proc.pid if self.proc and self.proc.returncode is None else None)

    def _env(self):
        e = dict(os.environ, GGML_CUDA_DISABLE_GRAPHS="1")
        if os.name != "nt":
            e["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, (self.dir, e.get("LD_LIBRARY_PATH"))))
        return e

    def _spawn_kwargs(self):
        if os.name == "nt":
            return dict(creationflags=subprocess.CREATE_NO_WINDOW)
        return dict(preexec_fn=_die_with_parent) if sys.platform.startswith("linux") else {}

    # --- аргументы ---
    async def _detect_backends(self):
        cli = os.path.join(self.dir, "cosyvoice-cli" + EXE)
        if not os.path.isfile(cli):
            log.warning("нет %s: бэкенд не проверить, беру CPU", cli)
            return []
        proc = await asyncio.create_subprocess_exec(
            cli, "--list-backends", cwd=self.dir, env=self._env(), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, **self._spawn_kwargs())
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 120)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("cosyvoice-cli --list-backends завис: беру CPU")
            return []
        found = parse_backends(out.decode(errors="replace"))
        log.info("бэкенды: %s", ", ".join(n + (" (встроенная)" if i else "") for n, i in found) or "нет")
        return found

    def _model(self):
        model = env("MODEL")
        if model:
            if not os.path.isfile(model):
                raise Unavailable(f"модель не найдена: {model}")
            return model
        ggufs = sorted(glob.glob(os.path.join(paths.MODELS, "*.gguf")))
        chosen = os.path.join(paths.MODELS, self.s.get("model", "")) if self.s and self.s.get("model") else ""
        if chosen in ggufs:
            return chosen
        if not ggufs:
            raise Unavailable("нет модели: скачайте её в админке (вкладка «Сервер») "
                              f"или положите *.gguf в {paths.MODELS}")
        if len(ggufs) > 1:
            raise Unavailable(f"в {paths.MODELS} несколько моделей: выберите одну в админке (вкладка «Сервер»)")
        return ggufs[0]

    def _voices(self):
        """[(имя, путь от paths.VOICES)]: короткие пути, чтобы много голосов влезло в командную строку Windows."""
        out, seen = [], {}
        files = sorted(glob.glob(os.path.join(paths.VOICES, "*.gguf"))) + \
            sorted(glob.glob(os.path.join(paths.VOICES, "*", "*.gguf")))
        for f in files:
            name = os.path.splitext(os.path.basename(f))[0]
            rel = os.path.relpath(f, paths.VOICES)
            if name in seen:
                log.warning("голос %s уже взят из %s, пропускаю %s", name, seen[name], rel)
                continue
            seen[name] = rel
            out.append((name, rel))
        return out

    async def _args(self):
        if not has_avx2():
            raise Unavailable("процессор без AVX2: cosyvoice.cpp на нём не работает")
        if not os.path.isfile(self.server):
            raise Unavailable(f"нет {self.server}")
        if self.backends is None:  # до модели: выбор устройства в админке виден и без неё
            self.backends = await self._detect_backends()
        self.backend = choose_backend(self.backends, self.prefer)
        self.model = ""
        self.model = self._model()
        if self.prefer not in ("auto", "cpu") and not self.backend.lower().startswith(self.prefer):
            log.warning("бэкенд %s не найден, работаю на CPU", self.prefer)
        args = [self.server, "--model", self.model, "--host", "127.0.0.1", "--port", str(self.port), "--webui",
                *backend_args(self.backend)]
        fe = os.path.join(paths.MODELS, "frontend-onnx")
        self.frontend = (first_existing(os.path.join(fe, "speech_tokenizer_v3.onnx"),
                                        os.path.join(fe, "speech_tokenizer_v3.int8.onnx")),
                         first_existing(os.path.join(fe, "campplus.onnx"), os.path.join(fe, "campplus.int8.onnx")))
        if all(self.frontend):
            args += ["--speech-tokenizer", self.frontend[0], "--campplus", self.frontend[1]]
        else:
            self.frontend = None
            log.info("ONNX-фронтенд не найден в %s: извлечение голоса из аудио недоступно", fe)
        voices = self._voices()
        for name, rel in voices:
            if len(subprocess.list2cmdline(args)) > 30000:  # предел командной строки Windows — 32767 символов
                log.error("голосов слишком много для командной строки: загружено %d из %d",
                          args.count("--voice-prompt"), len(voices))
                break
            args += ["--voice-prompt", f"{name}={rel}"]
        self.voices = args.count("--voice-prompt")
        return args

    # --- процесс ---
    async def start(self):
        self._wake = asyncio.Event()
        self.task = asyncio.create_task(self._supervise())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self._kill()
        self.state = "stopped"

    async def restart(self):
        """Перезапуск с новыми моделью, бэкендом и голосами."""
        self._restarting = True
        await self._kill()
        self._wake.set()

    @contextlib.asynccontextmanager
    async def paused(self):
        """Сервер остановлен на время блока: на Windows он держит открытыми файлы голосов."""
        self._hold += 1
        self._restarting = True
        try:
            await self._kill()
            yield
        finally:
            self._hold -= 1
            self._wake.set()

    async def _sleep(self, seconds):
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._wake.wait(), seconds)
        self._wake.clear()

    async def _kill(self):
        p = self.proc
        if not p or p.returncode is not None:
            return
        p.terminate()  # Windows: TerminateProcess; сохранять серверу нечего
        try:
            await asyncio.wait_for(p.wait(), 10)
        except asyncio.TimeoutError:
            p.kill()
            await p.wait()

    async def _pump(self, stream):
        while line := await stream.readline():
            text = line.decode(errors="replace").rstrip()
            if text:
                self.tail.append(text)
                srv_log.info("%s", text)

    async def _ready(self, session):
        """Ждёт ответа /status, пока процесс жив; затем грузит ONNX-фронтенд."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 300
        while self.proc.returncode is None and loop.time() < deadline:
            try:
                async with session.get(f"{self.url}/status", timeout=aiohttp.ClientTimeout(total=3)) as r:
                    if r.status == 200:
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(1)
        else:
            return False
        # «Работает» — только после фронтенда: иначе проверка скорости (bench.py) начинала синтез параллельно с
        # /frontend/model/load, а к серверу можно лишь один запрос за раз
        if self.frontend:
            body = dict(speech_tokenizer=self.frontend[0], campplus=self.frontend[1])
            try:
                async with session.post(f"{self.url}/frontend/model/load", json=body,
                                        timeout=aiohttp.ClientTimeout(total=120)) as r:
                    log.info("ONNX-фронтенд: %s %s", r.status, (await r.text())[:200])
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("ONNX-фронтенд не загружен: %s", e)
        self.state = "running"
        self.starts += 1
        return True

    async def _run(self, args):
        os.makedirs(paths.VOICES, exist_ok=True)
        shown = [a for i, a in enumerate(args) if "--voice-prompt" not in args[max(i - 1, 0):i + 1]]
        log.info("запуск: %s (голосов: %d)", subprocess.list2cmdline(shown), self.voices)
        self.tail.clear()
        self.state, self.error = "starting", ""
        self.proc = await asyncio.create_subprocess_exec(
            *args, cwd=paths.VOICES, env=self._env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, **self._spawn_kwargs())
        if self._job:
            try:
                self._job.add(self.proc.pid)
            except OSError as e:
                log.warning("сервер не добавлен в Job Object: %s", e)
        if self._hold:  # paused() начался, пока процесс запускался
            await self._kill()
        pump = asyncio.create_task(self._pump(self.proc.stdout))
        try:
            async with aiohttp.ClientSession() as session:
                if await self._ready(session):
                    log.info("cosyvoice-server готов: %s, бэкенд %s, голосов %d", self.url, self.backend, self.voices)
                elif self.proc.returncode is None:
                    log.error("cosyvoice-server не ответил за 5 минут, перезапускаю")
                    await self._kill()
            await self.proc.wait()
        finally:
            await self._kill()
            await asyncio.gather(pump, return_exceptions=True)

    async def _supervise(self):
        loop, delay = asyncio.get_running_loop(), 2
        while True:
            if self._hold:
                self.state, self.error = "stopped", "сервер остановлен: меняются голоса"
                await self._sleep(1)
                continue
            try:
                args = await self._args()
            except Unavailable as e:
                if self.error != str(e):
                    log.warning("cosyvoice-server не запущен: %s", e)
                self.state, self.error = "waiting", str(e)
                await self._sleep(5)
                continue
            if self._hold:
                continue
            self._restarting = False
            started = loop.time()
            await self._run(args)
            if self._restarting:
                self._restarting = False
                continue
            code = self.proc.returncode
            code = code - 2 ** 32 if code >= 2 ** 31 else code  # Windows отдаёт код без знака
            last = next((t for t in reversed(self.tail) if "[REQ]" not in t), "")
            self.state, self.error = "crashed", f"cosyvoice-server завершился (код {code}){': ' + last if last else ''}"
            self.restarts += 1
            delay = 2 if loop.time() - started > 60 else min(delay * 2, 60)
            log.warning("%s; перезапуск через %d с", self.error, delay)
            await self._sleep(delay)
