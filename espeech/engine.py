"""Модель espeech-server в рабочем потоке (Engine): загрузка и выгрузка, нормализация и ударения, синтез кусками,
состояние для /status; почему не видна видеокарта (cuda_note).

Всё, что трогает модель, идёт в одном рабочем потоке по очереди: синтез строго по одному, а CUDA не заводит ресурсы
на каждый поток запроса (с синтезом прямо в потоках ThreadingHTTPServer память росла на ~2 МБ за запрос). /status,
/tts/stop, GET /train и список голосов отвечают и во время синтеза и обучения."""
import ctypes, gc, logging, os, queue, random, threading, time

import numpy as np

from . import trainjob
from .wire import HTTPError

log = logging.getLogger("espeech")
API_VERSION = 1
MODEL_NAME = "ESpeech-TTS-1_SFT-256K"
STEPS = (12, 16, 32)
PARAMS = {"nfe_steps": {"type": "choice", "choices": list(STEPS), "default": 16, "title": "Качество синтеза",
                        "help": "12 — быстрее, но заметно теряется качество; 16 — оптимально; "
                                "32 — наибольшее качество, но вдвое медленнее"}}


def driver_cuda():
    """Версия CUDA, которую поддерживает установленный драйвер NVIDIA (cuDriverGetVersion: 12080 = CUDA 12.8),
    0 — драйвера нет."""
    try:
        cu = ctypes.WinDLL("nvcuda.dll") if os.name == "nt" else ctypes.CDLL("libcuda.so.1")
        v = ctypes.c_int()
        return v.value if cu.cuDriverGetVersion(ctypes.byref(v)) == 0 else 0
    except (OSError, AttributeError):
        return 0


def cuda_note(torch_cuda, driver):
    """Почему torch не видит видеокарту: версия CUDA у драйвера против той, под которую собран torch. Драйвер
    младшей версии CUDA той же мажорной годится (minor version compatibility), старшей — нет."""
    if not driver:
        return "драйвер NVIDIA не найден: видеокарты нет или драйвер не установлен"
    have = f"драйвер NVIDIA поддерживает CUDA {driver // 1000}.{driver % 1000 // 10}"
    try:
        major = int(str(torch_cuda).split(".")[0])
    except ValueError:
        major = 0
    if major and driver // 1000 < major:
        return f"{have}, а torch собран под CUDA {torch_cuda}: нужен драйвер новее"
    return f"{have}, torch под CUDA {torch_cuda or '?'}: видеокарта не найдена — проверьте диспетчер устройств"


class Engine:
    def __init__(self, args):
        self.args = args
        self.tts = self.acc = self.voices = self.adapters = None
        self.error = ""
        self.loading = False
        self.text_lock = threading.Lock()    # RUAccent не потокобезопасен; под ней же — правка словаря пользователя
        from .accent import UserAccents
        self.user = UserAccents(args.user_accents or None)
        self.stop = threading.Event()
        self.device = None
        self.gpu = {}                        # видеопамять карты {free_mb, total_mb}: после каждого задания потока
        self.training = trainjob.Training(self)
        self.jobs = queue.Queue()
        threading.Thread(target=self._work, name="model", daemon=True).start()

    # --- рабочий поток модели ---
    def _work(self):
        while True:
            fn, out = self.jobs.get()
            try:
                res = fn()
                if hasattr(res, "__next__"):
                    for item in res:
                        out.put(("item", item))
                    res = None
                out.put(("end", res))
            except BaseException as e:
                out.put(("error", e))
            self._gpu()

    def _gpu(self):
        """Свободная видеопамять — в рабочем потоке (CUDA из потоков запросов копит память), для GET /train."""
        if self.device == "cuda" and self.tts is not None:
            try:
                import torch
                free, total = torch.cuda.mem_get_info()
                self.gpu = dict(free_mb=free >> 20, total_mb=total >> 20)
            except Exception:
                pass

    def submit(self, fn, *a):
        """Поставить fn(*a) в очередь рабочего потока. → очередь ответа."""
        out = queue.Queue()
        self.jobs.put((lambda: fn(*a), out))
        return out

    def call(self, fn, *a):
        """fn(*a) в рабочем потоке, дождаться результата."""
        kind, x = self.submit(fn, *a).get()
        if kind == "error":
            raise x
        return x

    def stream(self, fn, *a):
        """Генератор fn(*a) в рабочем потоке: куски — по мере готовности."""
        out = self.submit(fn, *a)
        while True:
            kind, x = out.get()
            if kind == "item":
                yield x
            elif kind == "end":
                return
            else:
                raise x

    # --- модель ---
    def load(self):
        import torch
        from . import model as M, voices, accent, lora
        self.loading = True
        try:
            torch.backends.cudnn.enabled = False   # без cuDNN на 20 % быстрее (samples/espeech-lean)
            dev = self.args.device
            if dev == "auto":
                dev = "cuda" if torch.cuda.is_available() else "cpu"
                if dev == "cpu":
                    log.warning("CUDA недоступна (%s): синтез на процессоре",
                                cuda_note(torch.version.cuda, driver_cuda()))
            t = time.perf_counter()
            if self.acc is None:
                self.acc = accent.Accentor(os.path.join(self.args.models, "ruaccent"), user=self.user)
            try:
                tts = M.ESpeech(self.args.models, dev)
            except Exception as e:
                if dev != "cuda":
                    raise
                # nvcuda.dll есть, а драйвер не работает (torch: Found no NVIDIA driver) — лучше на процессоре, чем без модели
                log.warning("CUDA не поднялась (%s: %s): загружаю на процессоре", type(e).__name__, e)
                dev = "cpu"
                tts = M.ESpeech(self.args.models, dev)
            if dev == "cuda" and self.args.vram_limit_mb:   # имитация карты с меньшей памятью (проверка OOM)
                torch.cuda.set_per_process_memory_fraction(
                    min(1.0, self.args.vram_limit_mb / (torch.cuda.get_device_properties(0).total_memory >> 20)))
            self.adapters = lora.Adapters(tts.dit)
            self.voices = voices.Registry(tts, self.args.voices)
            self.tts, self.device = tts, dev
            self._warmup()
            if dev == "cuda":
                torch.cuda.empty_cache()   # после загрузки и прогрева — минус ~600 МБ видеопамяти
            self.error = ""
            log.info("модель загружена на %s за %.1f с, голосов: %d", dev, time.perf_counter() - t,
                     len(self.voices.names()))
        except Exception as e:
            self.tts = None
            self.error = f"{type(e).__name__}: {e}"
            log.exception("модель не загружена")
        finally:
            self.loading = False

    def _warmup(self):
        """Первый синтез в процессе дольше (ядра CUDA грузятся лениво) — прогрев на шуме вместо образца."""
        from . import model as M
        rng = np.random.default_rng(0)
        cond, _ = self.tts.cond((rng.standard_normal(M.SR) * 0.1).astype(np.float32))
        self.tts.sample(cond, M.SR // M.HOP, "Пр+огрев. Пр+огрев.", 50, steps=2)

    def unload(self):
        import torch
        self.training.unload()
        self.tts = self.voices = self.adapters = None
        gc.collect()
        if self.device == "cuda":
            torch.cuda.empty_cache()
        log.info("модель выгружена")

    def require(self):
        if self.tts is None:
            raise HTTPError(503, "модель загружается" if self.loading else f"модель не загружена: {self.error}")

    # --- текст ---
    def normalize(self, text):
        from . import normalize
        return normalize.normalize(text, self.acc.lexicon if self.acc else None)

    def accent(self, text):
        with self.text_lock:
            return self.acc(text)

    # --- синтез ---
    def synth(self, text, voice, seed=None, steps=16, adapter=None):
        """Генератор кусков float32 24 кГц: тишина по краям обрезана, между кусками пауза. Stopped — остановлен.
        adapter — last / best: снимок обучения вместо адаптера голоса, none — без адаптера."""
        from . import model as M, pipeline as PL
        self.require()
        self.stop.clear()   # /tts/stop останавливает только текущий синтез
        try:
            v = self.voices.get(voice)
        except KeyError:
            raise HTTPError(404, f"unknown voice {voice}")
        seed = random.randrange(2 ** 31) if seed is None else int(seed)
        t0 = time.perf_counter()
        norm = self.normalize(text)
        pieces = PL.chunks(norm, v.bps) if norm else []
        if adapter == "none":
            self.adapters.use(None)
        elif adapter:
            self.adapters.use(*self.training.adapter(adapter))
        elif v.has_lora:
            if self.adapters.current is not v:
                tensors, md = self.voices.lora_of(v)
                self.adapters.use(v, tensors, md)
        else:
            self.adapters.use(None)
        audio = first = 0.0
        for i, c in enumerate(pieces):
            acc = PL.gen_text(self.accent(c))
            mel = self.tts.sample(v.cond, v.ref_len, v.text + acc, PL.frames(acc, v.rate), steps, seed, self.stop)
            w = PL.trim(self.tts.decode(mel, v.rms))
            if i:
                yield np.zeros(int(PL.PAUSE * M.SR), np.float32)
            else:
                first = time.perf_counter() - t0
            audio += len(w) / M.SR
            yield w
        el = time.perf_counter() - t0
        log.info("синтез %s%s: %d симв., %d кусков, звук %.1f с, elapsed_ms=%d, первый звук %d мс, RTF %.2f, seed %d",
                 voice, f" (adapter {adapter})" if adapter else "", len(text), len(pieces), audio, el * 1000,
                 first * 1000, el / audio if audio else 0, seed)

    def status(self):
        d = dict(model_loaded=self.tts is not None, loading=self.loading, model=MODEL_NAME, engine="espeech",
                 api_version=API_VERSION, device=self.device or "",
                 capabilities=dict(stream=True, accent=True, extract=True, train=True, voice_formats=["espeech"],
                                   user_accents=True),
                 params=PARAMS, frontend_available=True, error=self.error, memory=self.memory(),
                 training=self.training.status(full=False))
        return d

    def memory(self):
        out = {}
        try:
            import psutil
            out["uss_mb"] = round(psutil.Process().memory_full_info().uss / 2 ** 20)
        except Exception:
            pass
        if self.device == "cuda":
            import torch
            out["vram_mb"] = round(torch.cuda.memory_reserved() / 2 ** 20)
        return out
