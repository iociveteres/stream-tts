"""Запуск cosyvoice-server в режиме managed (общий наблюдатель процесса — runtime.py).

Сервер берётся из рантайма (paths.RUNTIME, рядом с программой) или из COSYVOICE_SERVER (путь к нему или к его
папке). Аргументы собираются как в cosyvoice/entrypoint.sh:
- без AVX2 рантайм cosyvoice.cpp не работает — сервер не запускается;
- модель: MODEL, иначе выбранная в админке (settings.model), иначе единственный *.gguf в paths.MODELS
  (нет модели — ждём, пока появится: её можно скачать в админке, install.py);
- голоса: paths.VOICES/*.gguf и paths.VOICES/<набор>/*.gguf, имя голоса — имя файла;
- ONNX-фронтенд из paths.MODELS/frontend-onnx (полные версии приоритетнее int8): в режиме --webui флаги только
  заполняют форму (GET /frontend/model), загружает его editor.py перед созданием голоса;
- бэкенд (COSYVOICE_BACKEND=auto|cuda|vulkan|cpu, иначе выбор в админке, по умолчанию auto) — по выводу
  cosyvoice-cli --list-backends:
  CUDA, если есть ggml-cuda.dll и видеокарта NVIDIA, иначе Vulkan (дискретная видеокарта приоритетнее
  встроенной), иначе CPU. Набор, где лежат и ggml-cuda, и ggml-vulkan, на машине без них уходит на CPU;
- всегда GGML_CUDA_DISABLE_GRAPHS=1, а на CUDA ещё --llm-flash-attn 0: без них вместо речи шум (AGENTS.md).
Порт — COSYVOICE_PORT (иначе свободный), адрес сервера — COSYVOICE_HOST (runtime.py)."""
import asyncio, glob, logging, os, re, subprocess

from ... import paths
from ...i18n import tr
from ...paths import EXE, env
from ...runtime import Unavailable, spawn_kwargs

log = logging.getLogger("runtime")

BACKENDS = ("auto", "cuda", "vulkan", "cpu")


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


class Launcher:
    ID = "cosyvoice"            # логгер вывода сервера
    NAME = "cosyvoice-server"

    def __init__(self, server, settings=None):
        self.server, self.dir = server, os.path.dirname(os.path.abspath(server))
        self.s = settings
        self.port = int(env("COSYVOICE_PORT", "0"))
        self.host = env("COSYVOICE_HOST", "127.0.0.1")  # только --host сервера
        self.backend = ""          # выбранный бэкенд: CUDA0, Vulkan0, CPU
        self.backends = None       # [(имя, встроенная)] из --list-backends, на время работы stream-tts
        self.model = ""
        self.voices = 0
        self.frontend = None       # (speech_tokenizer, campplus), флаги сервера
        self.override = ""         # устройство на время проверки скорости (bench.py), в настройки не пишется

    @property
    def prefer(self):
        """Бэкенд: COSYVOICE_BACKEND, иначе проверка скорости, иначе выбор в админке, иначе auto."""
        v = (env("COSYVOICE_BACKEND") or self.override or (self.s.get("backend", "") if self.s else "")
             or "auto").lower()
        return v if v in BACKENDS else "auto"

    def public(self):
        return dict(backend=self.backend, prefer=self.prefer, model=os.path.basename(self.model), voices=self.voices,
                    backends=[n for n, _ in self.backends or []],
                    locked=dict(backend=bool(env("COSYVOICE_BACKEND")), model=bool(env("MODEL"))))

    def env(self):
        e = dict(os.environ, GGML_CUDA_DISABLE_GRAPHS="1")
        if os.name != "nt":
            e["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, (self.dir, e.get("LD_LIBRARY_PATH"))))
        return e

    # --- для лога ---
    def shown(self, args):
        """Командная строка без --voice-prompt: голосов бывают сотни."""
        return [a for i, a in enumerate(args) if "--voice-prompt" not in args[max(i - 1, 0):i + 1]]

    def started(self):
        return f" (голосов: {self.voices})"

    def ready(self):
        return f"бэкенд {self.backend}, голосов {self.voices}"

    @staticmethod
    def noise(line):
        """Строки вывода, которые не объясняют падение: журнал запросов."""
        return "[REQ]" in line

    # --- аргументы ---
    async def _detect_backends(self):
        cli = os.path.join(self.dir, "cosyvoice-cli" + EXE)
        if not os.path.isfile(cli):
            log.warning("нет %s: бэкенд не проверить, беру CPU", cli)
            return []
        proc = await asyncio.create_subprocess_exec(
            cli, "--list-backends", cwd=self.dir, env=self.env(), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, **spawn_kwargs())
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
                raise Unavailable(tr("модель не найдена: {model}", model=model))
            return model
        ggufs = sorted(glob.glob(os.path.join(paths.MODELS, "*.gguf")))
        chosen = os.path.join(paths.MODELS, self.s.get("model", "")) if self.s and self.s.get("model") else ""
        if chosen in ggufs:
            return chosen
        if not ggufs:
            raise Unavailable(tr("нет модели: скачайте её в админке (вкладка «Сервер») или положите *.gguf в {dir}",
                                 dir=paths.MODELS))
        if len(ggufs) > 1:
            raise Unavailable(tr("в {dir} несколько моделей: выберите одну в админке (вкладка «Сервер»)", dir=paths.MODELS))
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

    async def args(self, host, port):
        if not has_avx2():
            raise Unavailable(tr("процессор без AVX2: cosyvoice.cpp на нём не работает"))
        if not os.path.isfile(self.server):
            raise Unavailable(tr("нет {path}", path=self.server))
        if self.backends is None:  # до модели: выбор устройства в админке виден и без неё
            self.backends = await self._detect_backends()
        self.backend = choose_backend(self.backends, self.prefer)
        self.model = ""
        self.model = self._model()
        if self.prefer not in ("auto", "cpu") and not self.backend.lower().startswith(self.prefer):
            log.warning("бэкенд %s не найден, работаю на CPU", self.prefer)
        args = [self.server, "--model", self.model, "--host", host, "--port", str(port), "--webui",
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
