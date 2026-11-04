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

Всё, что трогает модель, — в рабочем потоке Engine (engine.py), по очереди: /status, /tts/stop, GET /train и
список голосов отвечают и во время синтеза и обучения."""
import argparse, json, logging, os, urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from . import pairs as P, trainjob
from .engine import STEPS, Engine
from .trainjob import TrainError
from .wire import HTTPError, multipart, pcm16, wav_header

log = logging.getLogger("espeech")


MAX_BODY = 512 << 20   # POST /train: 6 мин реплик в WAV 24 кГц — 17 МБ


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
