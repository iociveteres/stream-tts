"""Обучение LoRA в espeech-server (POST /train и др., espeech/trainjob.py) с настоящей моделью: сервер отдельным
процессом (test_server.Server), реплики — образцы voice-refs/cosyvoice/wc3 (wc3-arthas, -jaina, -thrall, -tyrande), десятки
шагов. Пропускается без models/espeech или образцов. Правила без модели (деление на проверку, шаги по объёму,
признак остановки, multipart) — тоже здесь, они не пропускаются.
Запуск: espeech/.venv/Scripts/python -m pytest tests/espeech/test_train_api.py (~2 мин на видеокарте).

Проверяется: ошибки запроса (нет голоса, голос только в памяти, пустой набор, слишком длинная реплика, параметры),
старт и ход, 409 на второй старт, синтез во время обучения, остановка, прослушивание last / best / none, 409 на старт
с несохранёнными результатами, сохранение в файл голоса с резервной копией и синтез им после перечитывания реестра,
сброс; 30 реплик — отложенные по правилу и шаги по объёму, сброс во время обучения; выгрузка модели во время обучения (с checkpointing) — остановка с сохранённым снимком; нехватка видеопамяти
(потолок --vram-limit-mb) — ошибка oom, память вернулась, с checkpointing обучение идёт."""
import json, os, subprocess, sys, time, uuid

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
MODELS = os.path.join(ROOT, "models", "espeech")
REFS = [os.path.join(ROOT, "voice-refs", "cosyvoice", "wc3", f"wc3-{n}") for n in ("arthas", "jaina", "thrall", "tyrande")]
HAVE = (os.path.isfile(os.path.join(MODELS, "espeech_fp16.safetensors")) and
        all(os.path.isfile(r + ".wav") and os.path.isfile(r + ".txt") for r in REFS))
need_model = pytest.mark.skipif(not HAVE, reason="нет models/espeech или образцов voice-refs/cosyvoice/wc3")
PHRASE = "Проверка обучения голоса."


# --- без модели ---
def test_split_val_rule():
    from espeech import pairs as P
    items = lambda n, sec=4.0: [(f"r{i}", np.zeros(int(sec * P.SR), np.float32)) for i in range(n)]
    assert P.split_val(items(29))[1] == []                       # меньше 30 реплик — без проверки
    tr, val = P.split_val(items(30))
    assert len(val) == 5 and len(tr) == 25                        # 5 по 4 с = 20 с
    assert len(P.split_val(items(30, 3.0))[1]) == 7               # добирает до 20 с
    assert P.split_val(items(30, 1.0))[1] == []                   # 12 по 1 с < 20 с — без проверки
    assert len(P.split_val(items(80))[1]) == 8 and len(P.split_val(items(500))[1]) == 12
    assert len(P.split_val(items(3), n=1)[1]) == 1 and P.split_val(items(40), n=0)[1] == []
    assert P.split_val(items(2), n=5)[0]                          # хотя бы одна реплика остаётся обучению
    assert len(P.split_val([(x, "текст") for _, x in items(30)])[1]) == 5   # (звук, текст) — как у сервера
    a, b = P.split_val(items(40), seed=3), P.split_val(items(40), seed=3)
    assert [x[0] for x in a[1]] == [x[0] for x in b[1]]


def test_default_steps_and_overfit():
    from espeech import train as T
    sec = lambda s: int(s * 93.75)
    assert T.default_steps([sec(3.5)] * 18) == 1000               # 1,05 мин — 1000 на минуту
    assert T.default_steps([sec(3.5)] * 35) == 2000               # 2,04 мин
    assert T.default_steps([sec(2), sec(6)] * 30) == 3000         # 4 мин — потолок
    assert T.default_steps([sec(3.5)] * 200) == 3000              # 11,7 мин — потолок
    assert T.default_steps([sec(5)]) == 500                       # одна реплика — минимум
    assert T.epoch_batches([2812]) == 1 and T.epoch_batches([1000, 1000, 1000, 1001]) == 2
    v = lambda *xs: [(i * 100, x) for i, x in enumerate(xs)]
    assert T.overfit(v(0.59, 0.567, 0.568, 0.5705, 0.571)) == 400   # два замера подряд ≥ минимума + 0,003
    assert T.overfit(v(0.59, 0.567, 0.571, 0.568, 0.571)) is None   # между ними — ниже порога
    assert T.overfit(v(0.59, 0.567, 0.571, 0.565, 0.566)) is None   # новый минимум


def test_multipart():
    from espeech.wire import multipart
    b = "x" + uuid.uuid4().hex
    body = (f'--{b}\r\nContent-Disposition: form-data; name="voice"\r\n\r\nтест\r\n'
            f'--{b}\r\nContent-Disposition: form-data; name="audio"; filename="р 1.wav"\r\n'
            f'Content-Type: audio/wav\r\n\r\n').encode() + b"\r\n--\x00\r\n\r\n" + \
        (f'\r\n--{b}\r\nContent-Disposition: form-data; name="audio"; filename*=utf-8\'\'%D1%80.wav\r\n\r\nB'
         f'\r\n--{b}--\r\n').encode()
    assert multipart(f"multipart/form-data; boundary={b}", body) == [
        ("voice", None, "тест".encode()), ("audio", "р 1.wav", b"\r\n--\x00\r\n\r\n"), ("audio", "р.wav", b"B")]


# --- сервер ---
def wav_bytes_from(x):
    """float32 24 кГц → WAV PCM16 моно."""
    import io, wave
    from espeech import voices as V
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(V.to_int16(x).numpy().tobytes())
    return buf.getvalue()


def wav_bytes(path, sec=None, tile=1):
    cmd = ["ffmpeg", "-v", "error", "-i", path] + (["-t", str(sec)] if sec else []) + \
        ["-ac", "1", "-ar", "24000", "-f", "f32le", "-"]
    x = np.frombuffer(subprocess.run(cmd, check=True, capture_output=True).stdout, np.float32)
    return wav_bytes_from(np.tile(x, tile))


def form(fields, replicas):
    """→ (тело, Content-Type): поля и реплики (имя файла, WAV, текст) парами audio + text."""
    b = "----" + uuid.uuid4().hex
    parts = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()]
    for fn, data, text in replicas:
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="audio"; filename="{fn}"\r\n'
                     f'Content-Type: audio/wav\r\n\r\n'.encode() + data + b"\r\n")
        parts.append(f'--{b}\r\nContent-Disposition: form-data; name="text"\r\n\r\n{text}\r\n'.encode())
    return b"".join(parts) + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


@pytest.fixture(scope="module")
def replicas():
    return [(os.path.basename(r) + ".wav", wav_bytes(r + ".wav"), open(r + ".txt", encoding="utf-8").read().strip())
            for r in REFS]


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    from espeech import voices as V
    from test_server import Server
    d = tmp_path_factory.mktemp("espeech-train")
    voices = d / "voices"
    (voices / "wc3").mkdir(parents=True)
    ref = V.read_wav(wav_bytes(REFS[1] + ".wav", 6))
    V.save(str(voices / "wc3" / "jaina.espeech"), ref, "П+омни, Тр+алл!", meta=dict(title="Джайна"))
    s = Server(str(voices))
    yield s
    s.close()


def train(srv, replicas, **fields):
    body, ctype = form({k: str(v) for k, v in fields.items()}, replicas)
    st, data = srv.request("POST", "/train", body, {"Content-Type": ctype})
    return st, json.loads(data) if data[:1] == b"{" else data


def wait(srv, cond, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        d = srv.get("/train")
        if cond(d):
            return d
        assert d["state"] != "error", d
        time.sleep(0.2)
    raise TimeoutError(srv.get("/train"))


def listen(srv, adapter=None, voice="jaina"):
    return srv.tts(text=PHRASE, voice=voice, seed=4, **({"adapter": adapter} if adapter else {}))


def post(srv, path, body=None):
    st, data = srv.request("POST", path, body or {})
    return st, json.loads(data) if data[:1] == b"{" else data


@need_model
def test_errors(srv, replicas):
    assert srv.get("/status")["capabilities"]["train"] is True
    st, d = train(srv, replicas[:1], voice="nobody")
    assert st == 404 and d["error"] == "unknown_voice", d
    st, d = train(srv, replicas[:1])
    assert st == 400 and d["error"] == "no_voice", d
    st, d = train(srv, [], voice="jaina")
    assert st == 400 and d["error"] == "empty", d
    long = [("long.wav", wav_bytes(REFS[0] + ".wav", tile=3), "Очень длинная реплика.")]   # 34,6 с
    st, d = train(srv, replicas[:1] + long, voice="jaina")
    assert st == 400 and d["error"] == "bad_items" and "реплика 2 (long.wav): 34.6 с" in d["message"], d
    st, d = train(srv, [("x.wav", b"not a wav", "Текст.")], voice="jaina")
    assert st == 400 and d["error"] == "bad_items", d
    st, d = train(srv, replicas[:1], voice="jaina", steps=0)
    assert st == 400 and d["error"] == "bad_param", d
    body, ctype = form({"voice": "jaina"}, replicas[:1])
    body = body.replace(b'name="text"', b'name="txt"')
    st, data = srv.request("POST", "/train", body, {"Content-Type": ctype})
    assert st == 400 and json.loads(data)["error"] == "bad_pairs"

    # голос только в памяти (POST /speaker без /speaker/save): адаптеру некуда записаться
    body, ctype = form({"name": "mem"}, [("a.wav", wav_bytes(REFS[1] + ".wav", 6), "П+омни, Тр+алл!")])
    assert srv.request("POST", "/speaker?type=extract", body, {"Content-Type": ctype})[0] == 200
    st, d = train(srv, replicas[:1], voice="mem")
    assert st == 400 and d["error"] == "voice_not_saved", d
    assert srv.request("DELETE", "/speaker/mem")[0] == 200

    assert srv.request("POST", "/tts", dict(text=PHRASE, voice="jaina", adapter="bogus"))[0] == 400
    st, d = post(srv, "/tts", dict(text=PHRASE, voice="jaina", adapter="last"))
    assert st == 409 and d["error"] == "no_snapshot", d
    st, d = post(srv, "/train/save", dict(snapshot="last"))
    assert st == 409 and d["error"] == "no_snapshot", d
    d = srv.get("/train")
    assert d["state"] == "idle" and d["limits"]["max_sec"] == 30 and "vram_need_mb" in d["limits"]


@need_model
def test_train_listen_save(srv, replicas):
    from espeech import voices as V
    path = os.path.join(srv.voices, "wc3", "jaina.espeech")
    original = open(path, "rb").read()
    base = listen(srv)

    st, d = train(srv, replicas, voice="jaina", steps=90, val=1, seed=2)
    assert st == 200 and d["state"] == "running", d
    assert d["steps"] == 90 and d["data"]["train_items"] == 3 and d["data"]["val_items"] == 1, d
    assert d["val_history"][0]["step"] == 0 and d["best"]["step"] == 0
    st, d2 = train(srv, replicas, voice="jaina")
    assert st == 409 and d2["error"] == "busy", d2
    assert srv.get("/status")["training"]["state"] == "running"

    d = wait(srv, lambda d: d["step"] >= 3)
    assert d["loss"] is not None and d["s_per_step"] > 0 and d["eta_sec"] is not None
    if "peak_mb" in d["memory"]:
        assert d["memory"]["peak_mb"] > 0 and d["gpu"]["total_mb"] > 0
    assert np.array_equal(listen(srv), base)                       # синтез между шагами — как без обучения
    during = listen(srv, "last")
    assert len(during) > 12000
    s1 = srv.get("/train")["step"]
    assert wait(srv, lambda d: d["step"] > s1)                     # обучение продолжилось после синтеза

    st, d = post(srv, "/train/stop")
    assert st == 200 and d["state"] == "done" and d["finished"] == "stopped", d
    step = d["step"]
    assert 3 <= step < 90 and set(d["snapshots"]) == {"last", "best"}
    assert [v["step"] for v in d["val_history"]] == [0, step]     # проверка и на шаге остановки
    assert srv.get("/train")["step"] == step                       # после остановки шагов нет

    none, last, best = listen(srv, "none"), listen(srv, "last"), listen(srv, "best")
    assert np.array_equal(none, base)
    assert not (len(last) == len(none) and np.array_equal(last, none))
    assert np.array_equal(best, none if d["best"]["step"] == 0 else last), d["best"]

    st, d2 = train(srv, replicas, voice="jaina")
    assert st == 409 and d2["error"] == "unsaved", d2

    st, res = post(srv, "/train/save", dict(snapshot="last"))
    assert st == 200 and res["step"] == step and res["backup"] == path + ".bak", res
    assert open(res["backup"], "rb").read() == original
    ref, md, lora = V.load(path)
    assert md["title"] == "Джайна" and md["lora_steps"] == str(step) and len(lora) == 264 and "lora_date" in md
    assert "jaina" in srv.get("/speaker")
    assert np.array_equal(listen(srv), last)                       # реестр перечитал файл: голос с адаптером

    st, res = post(srv, "/train/save", dict(snapshot="best"))      # второе сохранение — та же резервная копия
    assert st == 200 and open(res["backup"], "rb").read() == original
    assert srv.get("/train")["saved"]["snapshot"] == "best"
    st, d = post(srv, "/train/discard")
    assert st == 200 and d["state"] == "idle" and d["snapshots"] == []
    assert post(srv, "/tts", dict(text=PHRASE, voice="jaina", adapter="best"))[0] == 409
    assert np.array_equal(listen(srv), best)                       # файл голоса — со снимком best


@need_model
def test_unload_stops_training(srv, replicas):
    st, d = train(srv, replicas[:2], voice="jaina", steps=2000, checkpointing=1)
    assert st == 200 and d["checkpointing"] and d["steps"] == 2000 and d["data"]["val_items"] == 0, d
    assert d["data"]["val_note"] and d["best"] is None
    wait(srv, lambda d: d["step"] >= 2)
    assert srv.request("POST", "/model/unload", {})[0] == 200
    d = srv.get("/train")
    assert d["state"] == "done" and d["finished"] == "unload" and d["snapshots"] == ["last"], d
    assert srv.request("POST", "/model/load", {})[0] == 200
    x = listen(srv, "last")                                        # снимок пережил перезагрузку модели
    assert len(x) > 12000
    assert post(srv, "/tts", dict(text=PHRASE, voice="jaina", adapter="best"))[0] == 409
    assert post(srv, "/train/discard")[1]["state"] == "idle"


@need_model
def test_auto_val_and_steps(srv, replicas):
    """30 реплик по 3 с: проверка по правилу (5 отложенных, 15 с < 20 — добирается до 7), шагов по объёму; сброс во
    время обучения."""
    from espeech import voices as V
    cuts = []
    for fn, data, _ in replicas:
        x = V.read_wav(data)
        cuts += [x[i:i + 3 * 24000] for i in range(0, len(x) - 3 * 24000, 3 * 24000)]
    many = [(f"c{i}.wav", wav_bytes_from(cuts[i % len(cuts)]), f"Реплика номер {i}.") for i in range(30)]
    st, d = train(srv, many, voice="jaina")
    assert st == 200 and d["state"] == "running", d
    assert d["data"]["val_items"] == 7 and d["data"]["train_items"] == 23 and d["data"]["val_sec"] >= 20, d["data"]
    assert d["data"]["steps_default"] and d["steps"] == 1200, d   # 23 × 3 с = 1,15 мин
    assert d["best"]["step"] == 0 and d["snapshots"] == ["last", "best"]
    st, d = post(srv, "/train/discard")
    assert st == 200 and d["state"] == "idle", d
    assert srv.get("/train")["state"] == "idle"


@need_model
def test_oom(tmp_path, replicas):
    """Потолок видеопамяти 1,4 ГБ (модель — 0,7): без checkpointing обучение берёт ~3 ГБ — ошибка oom с понятным
    текстом, видеопамять возвращается, процесс жив; с checkpointing (~1,1 ГБ) обучение идёт."""
    import torch
    from espeech import voices as V
    from test_server import Server
    if not torch.cuda.is_available():
        pytest.skip("без видеокарты")
    voices = tmp_path / "voices"
    voices.mkdir()
    V.save(str(voices / "jaina.espeech"), V.read_wav(wav_bytes(REFS[1] + ".wav", 6)), "П+омни, Тр+алл!")
    s = Server(str(voices), extra=("--vram-limit-mb", "1400"), log="oom.log")
    try:
        st, d = train(s, replicas, voice="jaina", steps=20, val=0)
        assert st == 200, d
        d = s.get("/train")
        deadline = time.time() + 60
        while d["state"] == "running" and time.time() < deadline:
            time.sleep(0.2)
            d = s.get("/train")
        assert d["state"] == "error" and d["error"] == "oom", d
        assert "экономию видеопамяти" in d["message"] and "GiB" not in d["message"], d
        assert s.get("/status")["memory"]["vram_mb"] < 1100       # память обучения вернулась
        assert len(listen(s)) > 12000                              # процесс жив и синтезирует
        st, d = train(s, replicas, voice="jaina", steps=4, val=0, checkpointing=1)   # ошибка не держит старт
        assert st == 200, d
        d = wait(s, lambda d: d["state"] == "done", 120)
        assert d["step"] == 4 and d["finished"] == "steps" and d["memory"]["peak_mb"] < 1400, d
    finally:
        s.close()
