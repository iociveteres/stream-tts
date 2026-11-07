"""espeech-server с настоящей моделью: отдельный процесс на свободном порту со своей папкой голосов.
Пропускается без models/espeech (tools/espeech/prepare.py) или образца voice-refs/cosyvoice/wc3/wc3-jaina.wav (+ .txt).
Запуск: espeech/.venv/Scripts/python -m pytest tests/espeech (~1 мин; видеокарта, если есть, иначе CPU — дольше).

Голос создаётся через API (POST /speaker?type=extract → /speaker/save), из него — голоса с адаптером LoRA: нулевой
(звук должен совпасть с голосом без LoRA бит в бит — ветка считается, но ничего не меняет) и случайный (звук другой)."""
import http.client, json, os, socket, subprocess, sys, threading, time, urllib.parse, uuid

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
MODELS = os.path.join(ROOT, "models", "espeech")
REF = os.path.join(ROOT, "voice-refs", "cosyvoice", "wc3", "wc3-jaina")
LONG = ("Спасибо за подписку, друг! Сегодня мы пройдём кампанию нежити до конца. Если повезёт, то и бонусные "
        "миссии успеем. Пять! Четыре! Три! Два! Один! Пуск!")

pytestmark = pytest.mark.skipif(not (os.path.isfile(os.path.join(MODELS, "espeech_fp16.safetensors")) and
                                     os.path.isfile(REF + ".wav")), reason="нет models/espeech или образца")


class Server:
    def __init__(self, voices, user_accents="", extra=(), log="server.log"):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()
        self.voices, self.user_accents = voices, user_accents
        self.log = open(os.path.join(voices, "..", log), "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-m", "espeech", "--models", MODELS, "--voices", voices,
                                      "--port", str(self.port), "--user-accents", user_accents, *extra], cwd=ROOT, stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 180
        while time.time() < deadline:
            try:
                if self.get("/status")["model_loaded"]:
                    return
            except OSError:
                pass
            assert self.proc.poll() is None, "сервер завершился"
            time.sleep(0.5)
        raise TimeoutError("модель не загрузилась")

    def request(self, method, path, body=None, headers=None, timeout=300):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        if isinstance(body, dict):
            body, headers = json.dumps(body), {"Content-Type": "application/json"}
        c.request(method, path, body, headers or {})
        r = c.getresponse()
        return r.status, r.read()

    def get(self, path):
        st, data = self.request("GET", path, timeout=5)
        assert st == 200, data
        return json.loads(data)

    def tts(self, **body):
        st, data = self.request("POST", "/tts", body)
        assert st == 200, data
        assert data[:4] == b"RIFF"
        return np.frombuffer(data[44:], "<i2")

    def close(self):
        self.proc.terminate()
        self.proc.wait(30)
        self.log.close()


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    d = tmp_path_factory.mktemp("espeech")
    voices = d / "voices"
    voices.mkdir()
    s = Server(str(voices), str(d / "user-accents.json"))
    yield s
    s.close()


@pytest.fixture(scope="module")
def jaina(srv):
    """Голос из образца через API, сохранённый в файл; рядом — копии с нулевым и случайным адаптером LoRA."""
    import torch
    from espeech import voices as V
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", REF + ".wav", "-ac", "1", "-ar", "24000", "-f", "wav", "-"],
                         check=True, capture_output=True).stdout
    text = open(REF + ".txt", encoding="utf-8").read().strip()
    b = "----" + uuid.uuid4().hex
    parts = [(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n').encode() + v + b"\r\n"
             for k, v in (("name", b"jaina"), ("text", text.encode()))]
    parts.append(f'--{b}\r\nContent-Disposition: form-data; name="audio"; filename="a.wav"\r\n'
                 f'Content-Type: audio/wav\r\n\r\n'.encode() + pcm + b"\r\n")
    body = b"".join(parts) + f"--{b}--\r\n".encode()
    st, data = srv.request("POST", "/speaker?type=extract", body, {"Content-Type": f"multipart/form-data; boundary={b}"})
    assert st == 200, data
    assert "+" in json.loads(data)["text"]   # ударения поставил сервер
    st, _ = srv.request("POST", "/speaker?type=extract", body, {"Content-Type": f"multipart/form-data; boundary={b}"})
    assert st == 409
    path = os.path.join(srv.voices, "wc3", "jaina.espeech")
    st, data = srv.request("POST", "/speaker/save", dict(name="jaina", path="wc3/jaina.espeech"))
    assert st == 200, data
    assert srv.request("DELETE", "/speaker/jaina")[0] == 200   # из памяти; остаётся голос из файла
    ref, md, _ = V.load(path)
    g = torch.Generator().manual_seed(0)
    for name, scale in (("jaina-lora0", 0.0), ("jaina-lora", 0.02)):
        lora = {}
        for i in range(22):
            for t, (inf, outf) in dict(q=(1024, 1024), k=(1024, 1024), v=(1024, 1024), o=(1024, 1024),
                                       ff1=(1024, 2048), ff2=(2048, 1024)).items():
                lora[f"lora.{i}.{t}.A"] = torch.randn(16, inf, generator=g) * 0.02
                lora[f"lora.{i}.{t}.B"] = torch.randn(outf, 16, generator=g) * scale
        V.save(os.path.join(srv.voices, name + ".espeech"), ref, md["text"], float(md["tempo"]),
               dict(lora_rank=16, lora_alpha=16), lora)
    assert {"jaina", "jaina-lora0", "jaina-lora"} <= set(srv.get("/speaker"))
    return "jaina"


def test_status(srv):
    s = srv.get("/status")
    assert s["engine"] == "espeech" and s["api_version"] == 1
    assert s["params"]["nfe_steps"]["choices"] == [12, 16, 32]
    assert s["capabilities"]["stream"] and "espeech" in s["capabilities"]["voice_formats"]


def test_accent(srv):
    st, data = srv.request("POST", "/accent", dict(text="Внимание всем! Донат 500р"))
    d = json.loads(data)
    assert st == 200 and d["normalized"] == "Внимание всем! Донат пятьсот рублей"
    assert "вс+ем" in d["accented"] and "+" in d["accented"].split()[-1]


def test_accent_again(srv):
    """Повторная разметка не меняет текст, ударение, поставленное вручную, остаётся (раньше normalize делал из
    «з+амок» «з плюс амок»)."""
    accent = lambda t: json.loads(srv.request("POST", "/accent", dict(text=t))[1])["accented"]
    once = accent("Старый замок стоит на горе. Донат 500р")
    assert accent(once) == once and "плюс" not in once, once
    word = once.split()[1]
    manual = once.replace(word, "зам+ок" if word == "з+амок" else "з+амок", 1)
    assert accent(manual) == manual


def test_whole_and_stream(srv, jaina):
    x = srv.tts(text=LONG, voice=jaina, seed=3)
    assert 6 < len(x) / 24000 < 20 and np.abs(x).max() > 3000
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=120)
    t = time.perf_counter()
    c.request("POST", "/tts", json.dumps(dict(text=LONG, voice=jaina, seed=3, stream=True)),
              {"Content-Type": "application/json"})
    r = c.getresponse()
    assert r.status == 200 and r.getheader("Transfer-Encoding") == "chunked"
    buf, first = b"", None
    while chunk := r.read1(1 << 16):
        buf += chunk
        if first is None and len(buf) > 44:
            first = time.perf_counter() - t
    total = time.perf_counter() - t
    y = np.frombuffer(buf[44:], "<i2")
    assert buf[:4] == b"RIFF" and len(y) == len(x)   # тот же seed — те же куски
    assert first < total * 0.7, (first, total)        # первый кусок пришёл задолго до конца


def test_lora(srv, jaina):
    base = srv.tts(text="Проверка адаптера голоса.", voice=jaina, seed=5)
    zero = srv.tts(text="Проверка адаптера голоса.", voice="jaina-lora0", seed=5)
    rand = srv.tts(text="Проверка адаптера голоса.", voice="jaina-lora", seed=5)
    again = srv.tts(text="Проверка адаптера голоса.", voice=jaina, seed=5)   # адаптер выключился
    assert np.array_equal(base, zero) and np.array_equal(base, again)
    assert not (len(rand) == len(base) and np.array_equal(rand, base))


def test_errors(srv, jaina):
    assert srv.request("POST", "/tts", dict(text="Привет", voice="nobody"))[0] == 404
    assert srv.request("POST", "/tts", dict(text="Привет", voice=jaina, nfe_steps=20))[0] == 400
    assert srv.request("DELETE", "/speaker/nobody")[0] == 404
    assert srv.request("GET", "/nowhere")[0] == 404


def test_steps(srv, jaina):
    for n in (12, 32):
        assert len(srv.tts(text="Шаги синтеза.", voice=jaina, seed=1, nfe_steps=n)) > 12000


def test_stop(srv, jaina):
    res = {}

    def run():
        t = time.perf_counter()
        res["x"] = srv.tts(text=LONG * 3, voice=jaina, seed=1)
        res["t"] = time.perf_counter() - t
    th = threading.Thread(target=run)
    th.start()
    time.sleep(1.5)
    assert srv.request("POST", "/tts/stop", {})[0] == 200
    th.join(60)
    assert res["t"] < 3.0, res["t"]                    # остановился, а не досинтезировал фразу
    assert len(srv.tts(text="После остановки.", voice=jaina, seed=1)) > 12000   # следующий синтез не задет


def test_delete_hides_until_file_changes(srv, jaina):
    path = os.path.join(srv.voices, "jaina-lora.espeech")
    assert srv.request("DELETE", "/speaker/jaina-lora")[0] == 200
    assert "jaina-lora" not in srv.get("/speaker")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert "jaina-lora" in srv.get("/speaker")


def test_memory_stable(srv, jaina):
    """Смена голоса и адаптера не копит память (раньше: +0,4 МБ на смену адаптера, ~2 МБ на запрос)."""
    for i in range(6):
        srv.tts(text=f"Прогрев {i}.", voice=(jaina, "jaina-lora")[i % 2], seed=i)
    before = srv.get("/status")["memory"]
    for i in range(30):
        srv.tts(text=f"Смена голоса номер {i}.", voice=(jaina, "jaina-lora", "jaina-lora0")[i % 3], seed=i)
    after = srv.get("/status")["memory"]
    if "uss_mb" in before:
        assert after["uss_mb"] - before["uss_mb"] < 30, (before, after)
    if "vram_mb" in before:
        assert after["vram_mb"] - before["vram_mb"] < 64, (before, after)


def test_save_relative_path(srv, jaina):
    """Относительный path — от папки --voices (так сохраняет голос stream-tts: «<набор>/<имя>.espeech»)."""
    st, data = srv.request("POST", "/speaker/save", dict(name=jaina, path="rel/jaina-rel.espeech"))
    assert st == 200, data
    path = os.path.join(srv.voices, "rel", "jaina-rel.espeech")
    try:
        assert os.path.isfile(path) and "jaina-rel" in srv.get("/speaker")
        assert srv.request("POST", "/speaker/save", dict(name=jaina, path="../out.espeech"))[0] == 400
    finally:
        os.remove(path)


def test_save_only_inside_voices(srv, jaina):
    """Абсолютный путь и не .espeech — 400: иначе запрос записал бы голос куда угодно (автозагрузка, чужие файлы)."""
    outside = os.path.join(os.path.dirname(srv.voices), "outside.espeech")
    for p in (outside, os.path.join(srv.voices, "abs.espeech"), "rel/jaina.bat"):
        assert srv.request("POST", "/speaker/save", dict(name=jaina, path=p))[0] == 400, p
    assert not os.path.exists(outside) and not os.path.exists(os.path.join(srv.voices, "abs.espeech"))


def test_refuses_browser(srv, jaina):
    """Запрос из браузера (Origin: чужая страница или DNS rebinding) — 403; JSON без application/json — 415:
    text/plain браузер шлёт с чужой страницы без проверки CORS."""
    body = json.dumps(dict(name=jaina, path="rel/evil.espeech"))
    st, _ = srv.request("POST", "/speaker/save", body,
                        {"Content-Type": "application/json", "Origin": "http://evil.example"})
    assert st == 403
    assert srv.request("GET", "/speaker", headers={"Origin": "http://evil.example"})[0] == 403
    assert srv.request("POST", "/speaker/save", body, {"Content-Type": "text/plain"})[0] == 415
    assert not os.path.exists(os.path.join(srv.voices, "rel", "evil.espeech"))


def test_user_accents(srv, jaina):
    """Словарь пользователя перекрывает омограф («замок» решает turbo3.1), меняет звук и пишется в файл
    --user-accents."""
    accent = lambda t: json.loads(srv.request("POST", "/accent", dict(text=t))[1])["accented"]
    assert srv.get("/accents") == {"words": []}
    text = "Старый замок стоит на горе."
    before = accent(text)
    word = before.split()[1]
    assert word in ("з+амок", "зам+ок"), before
    other = "зам+ок" if word == "з+амок" else "з+амок"
    x0 = srv.tts(text=text, voice=jaina, seed=2)

    st, data = srv.request("POST", "/accents", dict(accented=other.upper()))
    assert st == 200 and json.loads(data)["word"] == "замок", data
    assert srv.get("/accents") == {"words": [dict(word="замок", accented=other)]}
    assert accent(text).split()[1] == other
    assert accent("Замок на горе.").split()[0] == other.capitalize()
    x1 = srv.tts(text=text, voice=jaina, seed=2)
    assert not (len(x0) == len(x1) and np.array_equal(x0, x1))   # синтез прочитал по словарю

    saved = json.load(open(srv.user_accents, encoding="utf-8"))
    assert saved["words"] == {"замок": other}
    for bad in ("замок", "zam+ok", "два слова", "з+", "з++амок"):
        assert srv.request("POST", "/accents", dict(accented=bad))[0] == 400, bad
    assert srv.request("DELETE", "/accents/" + urllib.parse.quote("замок"))[0] == 200
    assert srv.request("DELETE", "/accents/" + urllib.parse.quote("замок"))[0] == 404
    assert accent(text) == before and srv.get("/accents") == {"words": []}
