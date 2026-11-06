"""supertonic-server: HTTP API синтеза Supertonic 3 для stream-tts — подмножество API cosyvoice-server, как у
espeech-server, чтобы клиент у движков был общий (stream-tts/streamtts/engines/client.py). Только CPU.

API (всё JSON, кроме звука; ошибки — текстом по-русски):
- GET  /status — engine=supertonic, api_version, model_loaded, device=cpu, capabilities {stream, extract, accent,
  train, voice_formats}, params — схема настроек для админки (total_step, speed);
- GET  /speaker — имена голосов (папка --voices перечитывается при каждом запросе, voices.py);
- DELETE /speaker/<имя> — забыть голос (файл остаётся; голос вернётся, когда файл изменится, или после перезапуска);
- POST /speaker?type=extract, POST /speaker/save — 501 {error: unsupported, message}: своего клонирования у модели нет;
- POST /tts {text, voice, stream?, seed?, total_step?, speed?, lang?=ru} — WAV PCM16 моно 24 кГц; stream=true —
  WAV-заголовок с неизвестной длиной и PCM по мере готовности кусков (chunked). Поля cosyvoice (mode, chunk_tokens,
  …) игнорируются. 404 — нет голоса, 400 — неверные параметры, 503 — модель ещё загружается;
- POST /tts/stop — остановить текущий синтез (на ближайшем шаге модели, десятки мс).

Синтез — строго один за раз (App.lock): второй /tts ждёт первый. Запрос синтеза занимает свой поток
ThreadingHTTPServer, поэтому /status, /speaker и /tts/stop отвечают и во время синтеза."""
import argparse, json, logging, os, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from espeech.wire import HTTPError, pcm16, wav_header

from . import model as M
from .text import prepare
from .voices import EXT, Voices

log = logging.getLogger("supertonic")
API_VERSION = 1
STEPS = (5, 8, 12, 16)
SPEEDS = (0.9, 1.0, 1.05, 1.15, 1.3)
# Схема настроек для админки stream-tts; title и help переводит stream-tts (её DEFAULT_PARAMS — копия этих строк)
PARAMS = {"total_step": {"type": "choice", "choices": list(STEPS), "default": 8, "title": "Качество синтеза",
                         "help": "Шагов модели на фразу: 5 — быстрее всего; 8 — оптимально; "
                                 "12 и 16 — чуть чище звук, но синтез в полтора-два раза дольше"},
          "speed": {"type": "choice", "choices": list(SPEEDS), "default": 1.05, "title": "Скорость речи",
                    "help": "Темп речи: 1 — обычный, меньше — медленнее, больше — быстрее; "
                            "1,05 — как у авторов модели"}}
# Вне choices тоже можно (замеры, ручные запросы); дальше этих границ модель говорит неразборчиво
STEP_RANGE, SPEED_RANGE = (1, 32), (0.5, 2.0)
UNSUPPORTED = {"error": "unsupported", "message": "У Supertonic нет клонирования голоса: только готовые голоса"}


class App:
    def __init__(self, args):
        self.args = args
        self.model = None
        self.voices = Voices(args.voices)
        self.lock = threading.Lock()     # один синтез за раз
        self.stop = threading.Event()

    def load(self):
        t = time.time()
        try:
            self.model = M.Model(self.args.models, self.args.threads)
        except Exception as e:   # файлы на месте, но не читаются: процесс бесполезен — выйти, stream-tts покажет stderr
            log.error("модель не загрузилась (%s): %s: %s", self.args.models, type(e).__name__, e)
            sys.stderr.flush()
            os._exit(1)
        log.info("модель загружена за %.1f с (%s), потоков: %s", time.time() - t, self.model.version or "?",
                 self.args.threads or "по умолчанию onnxruntime")

    def status(self):
        return dict(engine="supertonic", api_version=API_VERSION, model="Supertonic 3",
                    model_loaded=self.model is not None, device="cpu",
                    capabilities=dict(stream=True, extract=False, accent=False, train=False, voice_formats=["supertonic"]),
                    params=PARAMS)


def _number(d, key, kind, default, lo, hi):
    v = d.get(key)
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (kind is int and v != int(v)):
        raise HTTPError(400, f"{key}: нужно {'целое ' if kind is int else ''}число от {lo:g} до {hi:g}")
    v = kind(v)
    if not lo <= v <= hi:
        raise HTTPError(400, f"{key}: нужно от {lo:g} до {hi:g}, а не {v:g}")
    return v


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "supertonic-server"
    # простаивающее соединение keep-alive живёт минуту: stream-tts держит его ≤2 с (KEEPALIVE в engines/client.py)
    # и рассчитывает, что сервер не закроет его раньше 5 с
    timeout = 60
    app: App = None

    def handle(self):
        try:
            super().handle()
        except (ConnectionError, TimeoutError):   # клиент закрыл соединение или простой дольше timeout
            pass

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), fmt % args)

    def send(self, status, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body, ctype = body.encode(), "text/plain; charset=utf-8"
        self._drain()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drain(self):
        """Ответ без чтения тела (ошибка): тело дочитать, иначе keep-alive примет его за следующий запрос."""
        if not self._read:
            self.body()

    def body(self):
        self._read = True
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def json(self):
        try:
            d = json.loads(self.body() or b"{}")
        except ValueError:
            raise HTTPError(400, "тело запроса — не JSON")
        if not isinstance(d, dict):
            raise HTTPError(400, "тело запроса — не объект JSON")
        return d

    def route(self, method):
        self._read = False
        path = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        try:
            fn = ROUTES.get((method, path))
            if fn is None and method == "DELETE" and path.startswith("/speaker/"):
                fn = Handler.speaker_delete
            if fn is None:
                raise HTTPError(404, "нет такого адреса")
            fn(self, path)
        except HTTPError as e:
            self.send(e.status, str(e))
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
    def status(self, path):
        self.send(200, self.app.status())

    def speakers(self, path):
        self.send(200, self.app.voices.names())

    def speaker_delete(self, path):
        name = urllib.parse.unquote(path[len("/speaker/"):])
        if not self.app.voices.delete(name):
            raise HTTPError(404, f"нет голоса {name}")
        self.send(200, dict(ok=True))

    def unsupported(self, path):
        self.send(501, UNSUPPORTED)

    def tts_stop(self, path):
        self.app.stop.set()
        self.send(200, dict(ok=True))

    def tts(self, path):
        app = self.app
        d = self.json()
        text, name = d.get("text"), d.get("voice")
        if not isinstance(text, str) or not text.strip():
            raise HTTPError(400, "text: нужен непустой текст")
        if not isinstance(name, str) or not name:
            raise HTTPError(400, "voice: нужно имя голоса")
        steps = _number(d, "total_step", int, app.args.steps, *STEP_RANGE)
        speed = _number(d, "speed", float, app.args.speed, *SPEED_RANGE)
        seed = d.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or seed < 0):
            raise HTTPError(400, "seed: нужно целое число от 0")
        lang = d.get("lang") or "ru"
        if lang not in M.LANGS:
            raise HTTPError(400, f"lang: один из {', '.join(M.LANGS)}")
        stream = bool(d.get("stream"))
        if app.model is None:
            raise HTTPError(503, "модель ещё загружается")
        try:
            style = app.voices.get(name)
        except KeyError:
            raise HTTPError(404, f"нет голоса {name} (файл {name}{EXT} в папке голосов или в наборе)")
        except ValueError as e:
            raise HTTPError(500, str(e))
        prepared = prepare(text, lang)
        with app.lock:
            app.stop.clear()   # /tts/stop останавливает только текущий синтез, не следующий
            t = time.time()
            gen = app.model.synth(prepared, lang, style, steps, speed, seed, app.stop, first_sentence=stream)
            try:
                n = self._stream(gen) if stream else self._whole(gen)
            finally:
                gen.close()
            log.info("%s: %d символов → %.1f с звука за %.2f с (шагов %d)%s", name, len(prepared), n / M.SR,
                     time.time() - t, steps, ", остановлен" if app.stop.is_set() else "")

    def _whole(self, gen):
        parts = []
        try:
            parts.extend(gen)
        except M.Stopped:
            pass
        pcm = pcm16(np.concatenate(parts)) if parts else pcm16(np.zeros(M.SR // 10, np.float32))
        self.send(200, wav_header(len(pcm)) + pcm, "audio/wav")
        return len(pcm) // 2

    def _stream(self, gen):
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        n = 0
        try:
            self._chunk(wav_header())
            for w in gen:
                self._chunk(pcm16(w))
                n += len(w)
        except M.Stopped:
            pass
        except (ConnectionError, BrokenPipeError):
            self.close_connection = True
            log.info("клиент отключился")
            return n
        except Exception:   # заголовок 200 уже ушёл: оборвать поток без завершающего куска — клиент увидит ошибку
            log.exception("синтез")
            self.close_connection = True
            return n
        self._chunk(b"")
        return n

    def _chunk(self, data):
        self.wfile.write(f"{len(data):X}\r\n".encode() + data + b"\r\n")
        self.wfile.flush()


ROUTES = {("GET", "/status"): Handler.status, ("GET", "/speaker"): Handler.speakers,
          ("POST", "/speaker"): Handler.unsupported, ("POST", "/speaker/save"): Handler.unsupported,
          ("POST", "/tts"): Handler.tts, ("POST", "/tts/stop"): Handler.tts_stop}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="supertonic-server", description=__doc__.split("\n")[0])
    ap.add_argument("--models", required=True,
                    help="папка модели в раскладке HF Supertone/supertonic-3: onnx/*.onnx, onnx/tts.json, "
                         "onnx/unicode_indexer.json")
    ap.add_argument("--voices", default="", help=f"папка голосов: <имя>{EXT} и <набор>/<имя>{EXT}")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8096)
    # замер (docs/supertonic.md, Ryzen 5 5600G): 1 поток вдвое медленнее, 2 — на ~5 %, больше 4 не быстрее;
    # остальные ядра — OBS и игре
    ap.add_argument("--threads", type=int, default=4, help="потоков onnxruntime на сессию, 0 — по числу ядер")
    ap.add_argument("--steps", type=int, default=8, help="total_step по умолчанию (запрос без total_step)")
    ap.add_argument("--speed", type=float, default=1.05, help="speed по умолчанию (запрос без speed)")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    lost = M.missing(args.models)
    if lost:
        print(f"supertonic-server: в папке модели {os.path.abspath(args.models)} нет файлов: {', '.join(lost)} "
              f"(нужна раскладка репозитория Hugging Face Supertone/supertonic-3)", file=sys.stderr)
        sys.exit(2)
    app = App(args)
    Handler.app = app
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=app.load, daemon=True).start()
    log.info("supertonic-server на http://%s:%d", args.host, args.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
