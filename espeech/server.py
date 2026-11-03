"""espeech-server: HTTP API синтеза ESpeech для stream-tts — подмножество API cosyvoice-server, чтобы клиент у
движков был общий, и расширения, объявленные в /status.

API (всё JSON, кроме звука):
- GET  /status — model_loaded, model, engine=espeech, api_version, device, capabilities {stream, accent, extract,
  train, voice_formats}, params — схема настроек модели для админки (nfe_steps: 16 / 32 / 12), memory {uss_mb,
  vram_mb}, error — почему модель не загружена;
- GET  /speaker — имена голосов (папка --voices перечитывается при каждом запросе);
- POST /speaker?type=extract — голос из образца в памяти: multipart name, text (то, что звучит; без «+» — ударения
  ставит RUAccent), audio (WAV PCM16 моно 24 кГц, 4–12 с), необязательные tempo (кадров на байт), title; 409 — имя занято;
- POST /speaker/save {name, path} — голос в файл .espeech (path — на машине сервера; относительный — от папки
  --voices, например «<набор>/<имя>.espeech»);
- DELETE /speaker/<имя> — выгрузить голос (файл остаётся; голос вернётся, когда файл изменится, или после перезапуска);
- POST /tts {text, voice, seed?, stream?, nfe_steps?, adapter?} — WAV 24 кГц PCM16; stream=true — WAV-заголовок с
  неизвестной длиной и PCM по мере готовности кусков (chunked); adapter — прослушивание обучения: last / best
  (снимок обучения LoRA вместо адаптера голоса) или none (голос без адаптера). Поля cosyvoice (mode, chunk_tokens, …)
  игнорируются;
- POST /tts/stop — остановить текущий синтез (шаг Эйлера доработает, видеокарта свободна через 0,1–0,2 с);
- POST /accent {text} — как прочитает модель: {normalized, accented}; ударения, уже стоящие в тексте, остаются как
  есть (accent.keep_marked), повторная разметка текст не меняет;
- GET /accents — словарь ударений пользователя {words: [{word, accented}]}; POST /accents {accented} — добавить или
  заменить слово («з+амок»; 400 — не одно русское слово с «+» перед гласной); DELETE /accents/<слово> — убрать.
  Слова пользователя перекрывают словарь RUAccent и омографы (accent.UserAccents), хранятся в файле --user-accents
  (без флага — только в памяти);
- POST /model/unload, POST /model/load — выгрузить и загрузить модель (torch возвращает системе не всю память:
  stream-tts выгружает модель остановкой процесса); выгрузка во время обучения его останавливает;
- обучение LoRA (trainjob.py): POST /train — multipart voice, реплики (audio + text, парами по порядку), checkpointing,
  steps, seed, val; GET /train — ход; POST /train/stop, POST /train/save {snapshot}, POST /train/discard. Ошибки
  обучения — JSON {error: код, message}.

Всё, что трогает модель, идёт в одном рабочем потоке по очереди: синтез строго по одному, а CUDA не заводит ресурсы
на каждый поток запроса (с синтезом прямо в потоках ThreadingHTTPServer память росла на ~2 МБ за запрос). /status,
/tts/stop, GET /train и список голосов отвечают и во время синтеза и обучения."""
import argparse, ctypes, gc, json, logging, os, queue, random, re, struct, threading, time, urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from . import pairs as P, trainjob
from .trainjob import TrainError

log = logging.getLogger("espeech")
API_VERSION = 1
MODEL_NAME = "ESpeech-TTS-1_SFT-256K"
STEPS = (12, 16, 32)
PARAMS = {"nfe_steps": {"type": "choice", "choices": list(STEPS), "default": 16, "title": "Качество синтеза",
                        "help": "12 — быстрее, но заметно теряется качество; 16 — оптимально; "
                                "32 — наибольшее качество, но вдвое медленнее"}}


MAX_BODY = 512 << 20   # POST /train: 6 мин реплик в WAV 24 кГц — 17 МБ


class HTTPError(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status = status


def multipart(ctype, body):
    """multipart/form-data → [(имя, имя файла или None, bytes)] по порядку (поля могут повторяться)."""
    m = re.search(r'boundary="?([^";]+)"?', ctype)
    if not ctype.startswith("multipart/form-data") or not m:
        raise HTTPError(400, "multipart/form-data expected")
    out = []
    for part in (b"\r\n" + body).split(b"\r\n--" + m.group(1).encode())[1:]:
        if part.startswith(b"--"):
            break
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        head = head.decode("utf-8", "replace")
        name = re.search(r'(?i)content-disposition:[^\r\n]*?\bname="([^"]*)"', head)
        fn = re.search(r'(?i)\bfilename="([^"]*)"', head)
        fn2 = re.search(r"(?i)\bfilename\*=utf-8''([^;\r\n]+)", head)
        if name:
            out.append((name.group(1), urllib.parse.unquote(fn2.group(1)) if fn2 else fn and fn.group(1), data))
    return out


def pcm16(x):
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


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


def wav_header(n_bytes=None, sr=24000):
    """WAV PCM16 моно; n_bytes=None — длина неизвестна (поток, как у cosyvoice-server)."""
    data = 0xFFFFFFFF if n_bytes is None else n_bytes
    riff = 0xFFFFFFFF if n_bytes is None else 36 + n_bytes
    return (b"RIFF" + struct.pack("<I", riff) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16) +
            b"data" + struct.pack("<I", data))


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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    engine: Engine = None

    def handle(self):
        try:
            super().handle()
        except ConnectionError:   # клиент закрыл соединение между запросами
            pass

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    # --- ответы ---
    def send(self, status, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body, ctype = body.encode(), "text/plain; charset=utf-8"
        self._drain()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._read = True
        return self.rfile.read(n) if n else b""

    def _drain(self):
        """Ответ до чтения тела (ошибка, 409 обучения до десятков МБ реплик): тело — дочитать, иначе соединение
        keep-alive примет его остаток за следующий запрос; слишком большое — закрыть соединение."""
        n = int(self.headers.get("Content-Length") or 0)
        if n and not getattr(self, "_read", True):
            self._read = True
            if n > MAX_BODY:
                self.close_connection = True
            else:
                self.rfile.read(n)

    def json(self):
        try:
            d = json.loads(self.body() or b"{}")
        except ValueError:
            raise HTTPError(400, "bad json")
        if not isinstance(d, dict):
            raise HTTPError(400, "bad json")
        return d

    def route(self, method):
        self._read = False
        url = urllib.parse.urlsplit(self.path)
        path, query = url.path.rstrip("/") or "/", dict(urllib.parse.parse_qsl(url.query))
        try:
            fn = ROUTES.get((method, path))
            if fn is None and path.startswith("/speaker/") and method == "DELETE":
                fn = Handler.speaker_delete
            if fn is None and path.startswith("/accents/") and method == "DELETE":
                fn = Handler.accents_delete
            if fn is None:
                raise HTTPError(404, "not found")
            fn(self, path, query)
        except HTTPError as e:
            self.send(e.status, str(e))
        except TrainError as e:
            self.send(e.status, dict(error=e.code, message=str(e)))
        except (ConnectionError, BrokenPipeError):
            pass
        except Exception as e:
            log.exception("%s %s", method, path)
            self.send(500, f"{type(e).__name__}: {e}")

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")

    # --- ручки ---
    def status(self, path, q):
        self.send(200, self.engine.status())

    def speakers(self, path, q):
        e = self.engine
        e.require()
        self.send(200, e.voices.names())

    def speaker_extract(self, path, q):
        from . import accent, voices
        e = self.engine
        e.require()
        if q.get("type") != "extract":
            raise HTTPError(400, "only type=extract")
        form = {k: v for k, _, v in multipart(self.headers.get("Content-Type", ""), self.body())}
        name = (form.get("name") or b"").decode().strip()
        text = (form.get("text") or b"").decode().strip()
        if not name or not text or not form.get("audio"):
            raise HTTPError(400, "name, text и audio обязательны")
        if name in e.voices.names():
            raise HTTPError(409, f"speaker {name} exists")
        try:
            ref = voices.read_wav(form["audio"])
        except Exception as ex:
            raise HTTPError(400, f"bad wav: {ex}")
        sec = len(ref) / 24000
        if not 1.0 <= sec <= voices.MAX_REF_SEC:
            raise HTTPError(400, f"образец {sec:.1f} с: нужно от 1 до {voices.MAX_REF_SEC:g} с (лучше 6–7)")
        text = accent.keep_marked(text, e.accent(accent.unmark(text)))   # без «+» — разметка модели
        tempo = float(form["tempo"]) if form.get("tempo") else None
        title = (form.get("title") or b"").decode() or None
        e.call(e.voices.add, name, ref, text, tempo, dict(title=title))
        log.info("голос %s из образца %.1f с: %s", name, sec, text)
        self.send(200, dict(ok=True, name=name, text=text))

    def speaker_save(self, path, q):
        e = self.engine
        e.require()
        d = self.json()
        name, target = d.get("name"), d.get("path")
        if not name or not target:
            raise HTTPError(400, "name и path обязательны")
        if not os.path.isabs(target):   # от папки голосов: так голос из редактора stream-tts ложится в набор
            root = os.path.abspath(e.args.voices or "")
            target = os.path.abspath(os.path.join(root, target))
            if not e.args.voices or os.path.commonpath([root, target]) != root:
                raise HTTPError(400, "относительный path — внутри папки --voices")
        try:
            e.voices.save(name, target)
        except KeyError:
            raise HTTPError(404, "unknown speaker")
        self.send(200, dict(ok=True))

    def speaker_delete(self, path, q):
        e = self.engine
        e.require()
        name = urllib.parse.unquote(path[len("/speaker/"):])
        if not e.voices.delete(name):
            raise HTTPError(404, "unknown speaker")
        if e.adapters and getattr(e.adapters.current, "name", None) == name:   # ключ снимка обучения — кортеж
            e.call(e.adapters.use, None)
        self.send(200, dict(ok=True))

    def accent(self, path, q):
        from . import accent
        e = self.engine
        if e.acc is None:
            raise HTTPError(503, "RUAccent не загружен")
        text = str(self.json().get("text", ""))
        # ударения, поставленные вручную (или прошлым нажатием «Ударения»), остаются как есть
        norm = e.normalize(accent.unmark(text))
        self.send(200, dict(normalized=norm, accented=accent.keep_marked(text, e.accent(norm))))

    def accents(self, path, q):
        e = self.engine
        with e.text_lock:
            words = [dict(word=k, accented=v) for k, v in e.user.items()]
        self.send(200, dict(words=words))

    def accents_put(self, path, q):
        e = self.engine
        try:
            with e.text_lock:
                word, acc = e.user.put(self.json().get("accented", ""))
        except ValueError as ex:
            raise HTTPError(400, str(ex))
        log.info("словарь ударений: %s", acc)
        self.send(200, dict(ok=True, word=word, accented=acc))

    def accents_delete(self, path, q):
        e = self.engine
        word = urllib.parse.unquote(path[len("/accents/"):])
        with e.text_lock:
            found = e.user.delete(word)
        if not found:
            raise HTTPError(404, "нет такого слова")
        log.info("словарь ударений: убрано %s", word)
        self.send(200, dict(ok=True))

    def tts_stop(self, path, q):
        self.engine.stop.set()
        self.send(200, dict(ok=True))

    def model_unload(self, path, q):
        self.engine.call(self.engine.unload)
        self.send(200, dict(ok=True))

    def model_load(self, path, q):
        e = self.engine
        if e.tts is None and not e.loading:
            e.call(e.load)
        e.require()
        self.send(200, dict(ok=True))

    def tts(self, path, q):
        from . import model as M
        e = self.engine
        d = self.json()
        text, voice = str(d.get("text", "")), str(d.get("voice", ""))
        steps = int(d.get("nfe_steps") or e.args.steps)
        if steps not in STEPS:
            raise HTTPError(400, f"nfe_steps: {' / '.join(map(str, STEPS))}")
        adapter = d.get("adapter") or None
        if adapter not in (None, "none") + trainjob.SNAPSHOTS:
            raise HTTPError(400, "adapter: last, best или none")
        gen = e.stream(e.synth, text, voice, d.get("seed"), steps, adapter)
        if not d.get("stream"):
            parts = []
            try:
                parts.extend(gen)
            except M.Stopped:
                log.info("синтез остановлен")
            pcm = pcm16(np.concatenate(parts)) if parts else pcm16(np.zeros(M.SR // 10, np.float32))
            self.send(200, wav_header(len(pcm)) + pcm, "audio/wav")
            return
        # поток: первый кусок синтезируется до заголовка, чтобы ошибка (нет голоса) пришла кодом ответа
        try:
            first = next(gen, None)
        except M.Stopped:
            first = None
            gen = iter(())
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            self._chunk(wav_header())
            if first is not None:
                self._chunk(pcm16(first))
            for w in gen:
                self._chunk(pcm16(w))
            self._chunk(b"")
        except M.Stopped:
            log.info("синтез остановлен")
            self._chunk(b"")
        except (ConnectionError, BrokenPipeError):
            e.stop.set()   # рабочий поток бросит синтез на следующем шаге
            try:
                for _ in gen:   # дождаться его: следующий запрос встанет в очередь за ним
                    pass
            except M.Stopped:
                pass
            log.info("клиент отключился")

    def _chunk(self, data):
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()

    # --- обучение LoRA (trainjob.py) ---
    def train_start(self, path, q):
        e = self.engine
        e.require()
        e.training.check_free()   # до чтения десятков МБ реплик
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise TrainError(413, "too_large", f"запрос {n >> 20} МБ, можно до {MAX_BODY >> 20} МБ")
        parts = multipart(self.headers.get("Content-Type", ""), self.body())
        form = {k: v for k, _, v in parts}
        audios = [(fn, v) for k, fn, v in parts if k == "audio"]
        texts = [v.decode("utf-8", "replace") for k, _, v in parts if k == "text"]

        def num(key, lo, hi):
            raw = (form.get(key) or b"").decode().strip()
            if not raw:
                return None
            try:
                x = int(raw)
            except ValueError:
                x = None
            if x is None or not lo <= x <= hi:
                raise TrainError(400, "bad_param", f"{key}: целое от {lo} до {hi}")
            return x
        steps, seed, val = num("steps", 1, trainjob.MAX_STEPS), num("seed", 0, 2 ** 31 - 1), num("val", 0, 1000)
        ckpt = (form.get("checkpointing") or b"").decode().strip().lower() in ("1", "true", "on", "yes")
        voice = (form.get("voice") or b"").decode().strip()
        if not voice:
            raise TrainError(400, "no_voice", "voice обязателен")
        names = e.voices.names()
        if voice not in names:
            raise TrainError(404, "unknown_voice", f"нет голоса {voice}")
        if voice in e.voices.memory or voice not in e.voices.files:
            raise TrainError(400, "voice_not_saved", f"голос {voice} только в памяти: адаптер пишется в файл голоса — "
                                                     "сначала сохраните голос (POST /speaker/save)")
        if not audios:
            raise TrainError(400, "empty", "нет реплик: части audio (WAV) и text парами")
        if len(audios) != len(texts):
            raise TrainError(400, "bad_pairs", f"частей audio {len(audios)}, а text {len(texts)}: нужны парами")
        items, bad = [], []
        for i, ((fn, data), text) in enumerate(zip(audios, texts), 1):
            label = f"{i} ({fn})" if fn else str(i)
            try:
                audio = P.read_audio(data)
            except ValueError as ex:
                bad.append(f"реплика {label}: {ex}")
                continue
            why = P.check_audio(audio)
            if why:
                bad.append(f"реплика {label}: {why}")
                continue
            text = P.prepare_text(text, e.accent, e.normalize)
            if not text:
                bad.append(f"реплика {label}: пустой текст")
                continue
            items.append((audio, text))
        if bad:
            raise TrainError(400, "bad_items", "; ".join(bad))
        train, held = P.split_val(items, 1 if seed is None else seed, val)
        note = None
        if not held:
            note = ("проверка выключена (val=0)" if val == 0 else
                    f"без проверки: нужно от {P.VAL_MIN_ITEMS} реплик и от {P.VAL_MIN_SEC:g} с отложенных")
        st = e.call(e.training.start, voice, e.voices.files[voice], train, held, steps, ckpt,
                    1 if seed is None else seed, note)
        self.send(200, st)

    def train_status(self, path, q):
        e = self.engine
        d = e.training.status()
        gpu = dict(e.gpu) if e.gpu else None   # None — CPU
        if gpu:   # подсказка к «экономии видеопамяти»: свободно меньше, чем нужно обучению без неё
            gpu["checkpointing_advised"] = gpu["free_mb"] < trainjob.VRAM_NEED_MB["default"]
        d.update(limits=trainjob.limits(), gpu=gpu)
        self.send(200, d)

    def train_stop(self, path, q):
        e = self.engine
        if e.training.request_stop():
            self.send(200, e.call(e.training.stop))
        else:
            self.send(200, e.training.status())

    def train_save(self, path, q):
        e = self.engine
        d = self.json()
        res = e.call(e.training.save, str(d.get("snapshot") or "last"))
        self.send(200, dict(ok=True, **res))

    def train_discard(self, path, q):
        e = self.engine
        self.send(200, e.call(e.training.discard))


ROUTES = {("GET", "/status"): Handler.status, ("GET", "/speaker"): Handler.speakers,
          ("POST", "/speaker"): Handler.speaker_extract, ("POST", "/speaker/save"): Handler.speaker_save,
          ("POST", "/tts"): Handler.tts, ("POST", "/tts/stop"): Handler.tts_stop, ("POST", "/accent"): Handler.accent,
          ("GET", "/accents"): Handler.accents, ("POST", "/accents"): Handler.accents_put,
          ("POST", "/model/unload"): Handler.model_unload, ("POST", "/model/load"): Handler.model_load,
          ("POST", "/train"): Handler.train_start, ("GET", "/train"): Handler.train_status,
          ("POST", "/train/stop"): Handler.train_stop, ("POST", "/train/save"): Handler.train_save,
          ("POST", "/train/discard"): Handler.train_discard}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="espeech-server", description=__doc__.split("\n")[0])
    ap.add_argument("--models", required=True, help="папка модели (tools/espeech/prepare.py): веса, vocab, ruaccent/")
    ap.add_argument("--voices", default="", help="папка голосов: <имя>.espeech и <набор>/<имя>.espeech")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8094)
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    ap.add_argument("--user-accents", default="", help="файл словаря ударений пользователя (JSON; без него — в памяти)")
    ap.add_argument("--steps", type=int, default=16, choices=STEPS, help="шагов синтеза по умолчанию")
    ap.add_argument("--vram-limit-mb", type=int, default=0,
                    help="потолок видеопамяти процесса (проверка: карта меньше и OOM обучения), 0 — без него")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    engine = Engine(args)
    Handler.engine = engine
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    engine.submit(engine.load)
    log.info("espeech-server на http://%s:%d", args.host, args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
