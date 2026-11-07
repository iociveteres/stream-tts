"""supertonic-server с настоящей моделью (CPU): отдельный процесс на свободном порту, голоса — пресеты модели,
скопированные во временную папку как <набор>/<имя>.supertonic. Пропускается без models/supertonic (раскладка HF
Supertone/supertonic-3; в worktree берётся папка основной копии репозитория, путь можно задать SUPERTONIC_MODELS).
Запуск из корня проекта: stream-tts/.venv/Scripts/python -m pytest tests/supertonic (~40 с).

Контракт API — docs/supertonic.md: /status, голоса, WAV целиком и потоком, seed, остановка, один синтез за раз при
отвечающем /status, ошибки 400/404/501, keep-alive дольше 2 с."""
import http.client, io, json, os, shutil, socket, subprocess, sys, threading, time, wave

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)


def find_models():
    if os.environ.get("SUPERTONIC_MODELS"):
        return os.environ["SUPERTONIC_MODELS"]
    here = os.path.join(ROOT, "models", "supertonic")
    if os.path.isdir(here):
        return here
    r = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=ROOT,
                       capture_output=True, text=True)
    return os.path.join(os.path.dirname(r.stdout.strip()), "models", "supertonic") if r.returncode == 0 else here


MODELS = find_models()
SHORT = "Спасибо за подписку, друг! Сегодня проходим кампанию до конца."
LONG = ("Спасибо за подписку, друг! Сегодня мы пройдём кампанию нежити до конца. Если повезёт, то и бонусные "
        "миссии успеем. Пять! Четыре! Три! Два! Один! Пуск!")


def _have_model():
    from supertonic import model as M
    return not M.missing(MODELS)


pytestmark = pytest.mark.skipif(not _have_model(), reason=f"нет модели в {MODELS}")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self, voices, log):
        self.port, self.voices = free_port(), voices
        self.log = open(log, "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-m", "supertonic", "--models", MODELS, "--voices", voices,
                                      "--port", str(self.port)], cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                if self.get("/status")["model_loaded"]:
                    return
            except OSError:
                pass
            assert self.proc.poll() is None, "сервер завершился"
            time.sleep(0.2)
        raise TimeoutError("модель не загрузилась")

    def conn(self, timeout=120):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)

    def request(self, method, path, body=None, timeout=120, headers=None):
        c = self.conn(timeout)
        c.request(method, path, json.dumps(body) if isinstance(body, dict) else body,
                  headers or ({"Content-Type": "application/json"} if isinstance(body, dict) else {}))
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def get(self, path):
        st, data = self.request("GET", path, timeout=5)
        assert st == 200, data
        return json.loads(data)

    def tts(self, **body):
        st, data = self.request("POST", "/tts", dict(dict(voice="M1", total_step=5), **body))
        assert st == 200, data.decode("utf-8", "replace")
        return data

    def close(self):
        self.proc.terminate()
        self.proc.wait(30)
        self.log.close()


def pcm(data):
    """WAV из ответа → int16; формат — PCM16 моно 24 кГц."""
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000)
        return np.frombuffer(w.readframes(w.getnframes()), "<i2")


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    d = tmp_path_factory.mktemp("supertonic")
    voices = d / "voices"
    (voices / "supertonic").mkdir(parents=True)
    for name in ("M1", "F1", "M2"):
        shutil.copy(os.path.join(MODELS, "voice_styles", name + ".json"), voices / "supertonic" / f"{name}.supertonic")
    shutil.copy(os.path.join(MODELS, "voice_styles", "F2.json"), voices / "F2.supertonic")   # без набора
    (voices / "supertonic" / "broken.supertonic").write_text("{}", encoding="utf-8")
    s = Server(str(voices), str(d / "server.log"))
    yield s
    s.close()


def test_status(srv):
    s = srv.get("/status")
    assert {k: s[k] for k in ("engine", "api_version", "model_loaded", "device")} == \
        dict(engine="supertonic", api_version=1, model_loaded=True, device="cpu")
    assert s["capabilities"] == dict(stream=True, extract=False, accent=False, train=False,
                                     voice_formats=["supertonic"])
    p = s["params"]
    assert set(p) == {"total_step", "speed"}
    assert {k: p["total_step"][k] for k in ("type", "choices", "default", "title")} == \
        dict(type="choice", choices=[5, 8, 12, 16], default=8, title="Качество синтеза")
    assert {k: p["speed"][k] for k in ("type", "choices", "default", "title")} == \
        dict(type="choice", choices=[0.9, 1.0, 1.05, 1.15, 1.3], default=1.05, title="Скорость речи")
    assert p["total_step"]["help"] and p["speed"]["help"]


def test_speakers(srv):
    assert srv.get("/speaker") == ["F1", "F2", "M1", "M2", "broken"]
    assert srv.request("DELETE", "/speaker/M2")[0] == 200
    assert "M2" not in srv.get("/speaker")            # забыт, файл на месте
    path = os.path.join(srv.voices, "supertonic", "M2.supertonic")
    assert os.path.isfile(path)
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert "M2" in srv.get("/speaker")                # файл изменился — голос вернулся
    assert srv.request("DELETE", "/speaker/nobody")[0] == 404
    st, data = srv.request("POST", "/tts", dict(text=SHORT, voice="broken"))
    assert st == 500 and "broken" in data.decode()


def test_unsupported(srv):
    for path in ("/speaker?type=extract", "/speaker/save"):
        st, data = srv.request("POST", path, b"x")
        assert st == 501 and json.loads(data)["error"] == "unsupported"


def test_errors(srv):
    assert srv.request("POST", "/tts", dict(text=SHORT, voice="nobody"))[0] == 404
    for bad in (dict(text=""), dict(total_step=0), dict(total_step=2.5), dict(speed="быстро"), dict(speed=5),
                dict(lang="xx"), dict(seed=-1), dict(seed=True)):
        st, data = srv.request("POST", "/tts", dict(dict(text=SHORT, voice="M1"), **bad))
        assert st == 400, (bad, st, data)
    assert srv.request("POST", "/tts", b"[]", headers={"Content-Type": "application/json"})[0] == 400


def test_refuses_browser(srv):
    """Запрос из браузера (Origin) — 403, JSON без application/json — 415: так espeech.wire отсекает чужие страницы и
    DNS rebinding у сервера без пароля."""
    body = json.dumps(dict(text=SHORT, voice="M1"))
    assert srv.request("POST", "/tts", body, headers={"Content-Type": "application/json",
                                                       "Origin": "http://evil.example"})[0] == 403
    assert srv.request("POST", "/tts", body, headers={"Content-Type": "text/plain"})[0] == 415


def test_whole_wav(srv):
    x = pcm(srv.tts(text=SHORT, seed=1))
    sec = len(x) / 24000
    assert 2 < sec < 10, sec
    assert np.abs(x).max() > 3000                         # речь, а не тишина
    # поля cosyvoice и пустая после нормализации фраза не ломают запрос
    assert len(pcm(srv.tts(text="😀", mode="zero_shot", chunk_tokens=10))) == 2400


def test_seed(srv):
    a, b, c = (srv.tts(text=SHORT, seed=s) for s in (7, 7, 8))
    assert a == b
    assert a != c


def test_stress_marks_ignored(srv):
    assert srv.tts(text="Старый з+амок на г+оре.", seed=3) == srv.tts(text="Старый замок на горе.", seed=3)


def test_stream(srv):
    """Заголовок с неизвестной длиной, затем куски по мере синтеза: первое предложение приходит заметно раньше
    конца ответа. Поток длиной совпадает с куском по паузе GAP_SEC между кусками."""
    c = srv.conn()
    c.request("POST", "/tts", json.dumps(dict(text=LONG, voice="F1", total_step=5, stream=True, seed=1)),
              {"Content-Type": "application/json"})
    t0 = time.time()
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Transfer-Encoding") == "chunked"
    arrivals, data = [], b""
    while True:
        part = r.read1(1 << 20)
        if not part:
            break
        data += part
        arrivals.append((time.time() - t0, len(data)))
    c.close()
    assert data[:4] == b"RIFF" and data[40:44] == b"\xff\xff\xff\xff"
    with wave.open(io.BytesIO(data[:44] + b"")) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000)
    first_audio = next(t for t, n in arrivals if n > 44)
    total = arrivals[-1][0]
    assert first_audio < 0.6 * total, arrivals
    assert len({n for _, n in arrivals}) >= 3            # заголовок и не меньше двух кусков звука
    assert (len(data) - 44) / 2 / 24000 > 8


def test_stop(srv):
    """Остановка посреди потока: ответ завершается сразу, звука меньше, чем у полной фразы."""
    result = {}

    def run():
        c = srv.conn()
        c.request("POST", "/tts", json.dumps(dict(text=LONG * 2, voice="M1", total_step=16, stream=True)),
                  {"Content-Type": "application/json"})
        r = c.getresponse()
        result["data"] = r.read()
        result["end"] = time.time()
        c.close()

    t = threading.Thread(target=run)
    t.start()
    time.sleep(1.5)
    assert srv.request("POST", "/tts/stop", b"")[0] == 200
    stopped = time.time()
    t.join(30)
    assert result["end"] - stopped < 1.0, result["end"] - stopped
    assert (len(result["data"]) - 44) / 2 / 24000 < 10       # полная фраза — больше 20 с
    assert len(pcm(srv.tts(text="Дальше синтез идёт.")))     # следующий запрос стоп не задевает


def test_one_at_a_time(srv):
    """Второй /tts ждёт первый, а /status в это время отвечает сразу."""
    ends = {}

    def run(key, text):
        srv.tts(text=text, total_step=8)
        ends[key] = time.time()

    a = threading.Thread(target=run, args=("a", LONG))
    a.start()
    time.sleep(0.3)
    b = threading.Thread(target=run, args=("b", "Короткая фраза."))
    b.start()
    time.sleep(0.2)
    t = time.time()
    assert srv.get("/status")["model_loaded"]
    assert time.time() - t < 0.5
    assert srv.get("/speaker")
    assert "a" not in ends                                 # первый ещё синтезируется
    a.join(60)
    b.join(60)
    assert ends["b"] > ends["a"]


def test_keepalive(srv):
    """Соединение, простоявшее 3 с (stream-tts держит до 2 с), сервер не закрыл."""
    c = srv.conn()
    for i in range(2):
        if i:
            time.sleep(3)
        c.request("GET", "/status")
        r = c.getresponse()
        assert r.status == 200
        r.read()
    c.close()


def test_no_model(tmp_path):
    r = subprocess.run([sys.executable, "-m", "supertonic", "--models", str(tmp_path), "--port", str(free_port())],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert r.returncode != 0
    assert "onnx/vector_estimator.onnx" in r.stderr
