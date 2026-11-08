"""Обвязка e2e-тестов: процесс stream-tts, клиент его API, тестовый оверлей, ожидание и разбор звука.

Тесты синхронные. Всё асинхронное (поддельный cosyvoice, WebSocket оверлея) живёт в цикле событий фонового
потока (Loop), тест зовёт его через Loop.run. stream-tts — отдельный процесс со своими портом и папками данных,
моделей и голосов во временной папке: Docker-версию на 8090 и stream-tts-data/ он не трогает."""
import asyncio, http.cookiejar, io, json, os, socket, sqlite3, subprocess, sys, threading, time, urllib.error, \
    urllib.parse, urllib.request, warnings, wave

import aiohttp
import numpy as np

from fake_cosyvoice import VOICES

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
STREAM_TTS = os.path.join(ROOT, "stream-tts")
SR = 24000


def make_app():
    """Приложение stream-tts (web.make_app) без запуска — для тестов, которые берут роуты из самого приложения.
    → (модуль streamtts.web, приложение)."""
    if STREAM_TTS not in sys.path:
        sys.path.insert(0, STREAM_TTS)
    from streamtts import web
    secret, web._secret = web._secret, lambda: "test"   # make_app пишет секрет в папку данных — тут не нужен
    try:
        with warnings.catch_warnings():   # приложение без запуска: предупреждения aiohttp о ключах — не о тесте
            warnings.simplefilter("ignore")
            return web, web.make_app(None)
    finally:
        web._secret = secret


def wait_for(fn, timeout=10.0, what="условие", step=0.05):
    """Ждать, пока fn() не вернёт истину; вернуть её. Вместо sleep: тесты не зависят от скорости машины."""
    deadline = time.monotonic() + timeout
    while True:
        v = fn()
        if v:
            return v
        if time.monotonic() > deadline:
            raise AssertionError(f"не дождались за {timeout} с: {what}")
        time.sleep(step)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Loop:
    """Цикл событий в фоновом потоке."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro, timeout=30):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)


class Response:
    def __init__(self, status, headers, body):
        self.status, self.headers, self.body = status, headers, body

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


class Api:
    """HTTP-клиент stream-tts со своими cookie; перенаправления не выполняет (их проверяют тесты доступа).
    Адрес — 127.0.0.1: localhost из Python на Windows сначала пробует IPv6 и добавляет ~2 с."""

    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), _NoRedirect)

    def request(self, method, path, json_body=None, data=None, headers=None, timeout=15):
        headers = dict(headers or {})
        if json_body is not None:
            data = json.dumps(json_body).encode()
            headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return Response(r.status, r.headers, r.read())
        except urllib.error.HTTPError as e:
            return Response(e.code, e.headers, e.read())

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, json_body={} if body is None else body, **kw)

    def ok(self, path, body=None):
        """POST в API, который должен пройти."""
        r = self.post(path, body)
        assert r.status == 200 and r.json().get("ok"), f"POST {path}: {r.status} {r.text}"
        return r.json()

    def login(self, password):
        """Форма входа, как login.html."""
        return self.request("POST", "/login", data=urllib.parse.urlencode(dict(password=password)).encode(),
                            headers={"Content-Type": "application/x-www-form-urlencoded"})

    def state(self):
        r = self.get("/api/state")
        assert r.status == 200, f"/api/state: {r.status} {r.text}"
        return r.json()

    def upload(self, path, data, filename):
        """Файл сырым телом с именем в X-Filename, как админка (запись в черновик, набор голосов)."""
        return self.request("POST", path, data=data, timeout=60, headers={
            "Content-Type": "application/octet-stream", "X-Filename": urllib.parse.quote(filename)})

    def test(self, text, login="tester", voice=None):
        """Тестовая активация (как кнопка «Тест» в админке): весь путь, кроме Twitch. Возвращает ID активации."""
        body = dict(text=text, login=login)
        if voice:
            body["voice"] = voice
        return self.ok("/api/test", body)["id"]


class StreamTTS:
    """Процесс stream-tts. Окружение очищено от переменных проекта: Client ID Twitch, пароль, серверы синтеза
    задаются только тестом. cosy_url — готовый (поддельный) cosyvoice-server (STREAM_TTS_TEST_SERVERS); None —
    stream-tts сам запускает свой сервер (уровень real) или серверы задаёт env."""

    def __init__(self, workdir, cosy_url, env=None):
        self.dir = workdir
        self.data = os.path.join(workdir, "data")
        self.cosy_url = cosy_url
        self.extra = env or {}
        self.port = None
        self.proc = None
        self.log_path = os.path.join(workdir, "stream-tts.log")
        self.tunnel_dir = os.path.join(workdir, "cloudflared")   # запуски поддельного cloudflared (fake_cloudflared.py)

    def env(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("STREAM_TTS_", "COSYVOICE_", "TWITCH_")) and k not in ("MODEL", "FFMPEG")}
        for d in ("models", "voices", "project", "runtime"):
            os.makedirs(os.path.join(self.dir, d), exist_ok=True)
        env.update(
            STREAM_TTS_PORT=str(self.port), STREAM_TTS_DATA=self.data,
            STREAM_TTS_MODELS=os.path.join(self.dir, "models"), STREAM_TTS_VOICES=os.path.join(self.dir, "voices"),
            # без voice-refs/ проекта: наборы и эффекты голосов задаёт тест
            STREAM_TTS_PROJECT=os.path.join(self.dir, "project"),
            STREAM_TTS_RUNTIME=os.path.join(self.dir, "runtime"),
            # туннель — всегда поддельный (fake_cloudflared.py): тесты не качают cloudflared и не ходят в Cloudflare
            STREAM_TTS_CLOUDFLARED=os.path.join(HERE, "fake_cloudflared.py"), FAKE_CLOUDFLARED_DIR=self.tunnel_dir,
            STREAM_TTS_TUNNEL_RETRY="0.5",
            STREAM_TTS_LANG="ru", PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        if self.cosy_url:
            env["STREAM_TTS_TEST_SERVERS"] = f"cosyvoice={self.cosy_url}"
        env.update(self.extra)
        return env

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def spawn(self):
        """Запустить, не дожидаясь готовности."""
        if self.port is None:
            self.port = free_port()
        self.log = open(self.log_path, "ab")
        self.proc = subprocess.Popen([sys.executable, "-m", "streamtts"], cwd=STREAM_TTS, env=self.env(),
                                     stdout=self.log, stderr=subprocess.STDOUT)
        return self

    def start(self, password=None, timeout=30, synth=True):
        """Запустить и дождаться, когда он ответит и увидит cosyvoice (synth=False — сервер синтеза не ждать:
        встроенный может ждать модель)."""
        self.spawn()
        api = Api(self.base)
        wait_for(lambda: self._up(api) and self._login(api, password) and (not synth or api.state()["cosy"]["ok"]),
                 timeout, f"запуск stream-tts\n{self.tail()}")
        return self

    def _up(self, api):
        if self.proc.poll() is not None:
            raise AssertionError(f"stream-tts завершился с кодом {self.proc.returncode}\n{self.tail()}")
        try:
            api.get("/login", timeout=2)
            return True
        except OSError:
            return False

    def _login(self, api, password):
        return not password or api.get("/api/state").status == 200 or api.login(password).status == 302 \
            and api.get("/api/state").status == 200

    def stop(self):
        """Остановка без on_cleanup: на Windows terminate — это TerminateProcess. Корректная остановка (награда на
        паузу) тестами не проверяется."""
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)
        if self.proc:
            self.log.close()
        self.proc = None

    def tail(self, n=40):
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-n:])
        except OSError:
            return ""

    @property
    def db_path(self):
        return os.path.join(self.data, "stream-tts.sqlite")

    def sql(self, query, *args):
        """Запрос к SQLite stream-tts (в процессе тоже открыта: WAL). Строки — словари. Не mode=ro: база в WAL без
        файла -shm (процесс остановлен) так не открывается — disk I/O error."""
        con = sqlite3.connect(self.db_path, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute(query, args).fetchall()]
        finally:
            con.close()

    def event(self, rid, timeout=10):
        """Итог активации из истории (SQLite stream-tts): outcome, reason и остальное. Ждёт, пока он появится."""
        def row():
            rows = self.sql("SELECT * FROM events WHERE redemption=?", rid)
            return rows[0] if rows else None
        return wait_for(row, timeout, f"итог активации {rid}")


class Played:
    def __init__(self, id):
        self.id = id
        self.chunks = []
        self.started_at = time.monotonic()   # пришёл start
        self.first_at = None    # первый кусок звука
        self.ended = False      # пришёл end: всё аудио отправлено
        self.stopped = False    # пришёл stop: фразу прервали

    @property
    def audio(self):
        return np.frombuffer(b"".join(self.chunks), "<i2").astype(np.float32) / 32768

    @property
    def seconds(self):
        return sum(len(c) for c in self.chunks) / 2 / SR


class Overlay:
    """Тестовый оверлей: WebSocket /overlay/ws, как overlay.html. Сообщает ready и, если auto_end, ended сразу после
    end (звук «проигран» мгновенно). Методы вызываются из потока теста."""

    def __init__(self, loop, base):
        self.loop, self.url = loop, base.replace("http", "ws", 1) + "/overlay/ws"
        self.active = None
        self.items = []         # Played по порядку начала
        self.auto_end = True
        self.settings = None    # последнее settings: пауза и громкость 0…1
        self.session = self.ws = self.reader = None

    def connect(self, ready=True):
        self.loop.run(self._connect())
        wait_for(lambda: self.active is not None, 5, "ответ оверлею: active/inactive")
        if ready:
            self.ready()
        return self

    async def _connect(self):
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.url)
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        cur = None
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                if cur:
                    if cur.first_at is None:
                        cur.first_at = time.monotonic()
                    cur.chunks.append(msg.data)
                continue
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            d = json.loads(msg.data)
            t = d.get("type")
            if t == "settings":
                self.settings = d
            elif t in ("active", "inactive"):
                self.active = t == "active"
            elif t == "start":
                cur = Played(d["id"])
                self.items.append(cur)
            elif t == "end" and cur and cur.id == d["id"]:
                cur.ended = True
                if self.auto_end:
                    await self.ws.send_json(dict(type="ended", id=d["id"]))
            elif t == "stop":
                for it in self.items:
                    if it.id == d["id"]:
                        it.stopped = True

    def ready(self, on=True):
        self.loop.run(self.ws.send_json(dict(type="ready", ready=on)))

    def ended(self, rid):
        self.loop.run(self.ws.send_json(dict(type="ended", id=rid)))

    def played(self, rid, timeout=10):
        """Фраза rid, дошедшая до конца (end)."""
        return wait_for(lambda: next((it for it in self.items if it.id == rid and it.ended), None), timeout,
                        f"фраза {rid} в оверлее")

    def close(self):
        async def _close():
            if self.ws:
                await self.ws.close()
            if self.reader:
                self.reader.cancel()
            if self.session:
                await self.session.close()
        self.loop.run(_close())


# --- разбор звука ---
def read_wav(data):
    """WAV PCM16 -> (float32, частота, каналы)."""
    with wave.open(io.BytesIO(data)) as w:
        assert w.getsampwidth() == 2, "ожидался PCM16"
        sr, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768
    return x, sr, ch


def make_wav(x, sr=SR, channels=1):
    """float32 (моно; при channels=2 тот же звук в оба канала) -> WAV PCM16."""
    pcm = (np.clip(np.repeat(x, channels) if channels > 1 else x, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def ffmpeg(args, data):
    """ffmpeg из PATH: stdin -> stdout."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", *args], input=data, capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    return p.stdout


def decode(data):
    """Любой звук (mp3 примеров и редактора) -> float32 24 кГц моно."""
    return np.frombuffer(ffmpeg(["-i", "-", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"], data), np.float32)


def sine(hz, seconds, amp=0.3, sr=SR):
    return (amp * np.sin(2 * np.pi * hz * np.arange(int(seconds * sr)) / sr)).astype(np.float32)


def dominant_hz(x):
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / SR)[np.argmax(spec)])


def voice_of(it):
    """Голос сыгранной фразы — по частоте синуса поддельного cosyvoice (fake_cosyvoice.VOICES)."""
    hz = dominant_hz(it.audio)
    return next(n for n, (f, _) in VOICES.items() if abs(f - hz) < 3)


def band_share(x, hz, width=30):
    """Доля энергии сигнала в полосе hz ± width."""
    spec = np.abs(np.fft.rfft(x)) ** 2
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return float(spec[(f > hz - width) & (f < hz + width)].sum() / spec.sum())
