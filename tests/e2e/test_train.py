"""Обучение LoRA голоса ESpeech (engines/espeech/train.py, /api/train*): набор реплик в данных stream-tts, обучение —
на сервере (POST /train и др.), прослушивание снимков, сохранение в голос, сброс, нехватка видеопамяти; перезапуски
встроенного сервера во время обучения и с несохранёнными результатами.

Поддельный espeech-server с обучением (FakeCosy(engine="espeech", train=True)): шаги идут сами, снимок узнаётся по
частоте синтеза (last — голос × 1,1, best — × 1,05). Сначала — готовый сервер (STREAM_TTS_TEST_SERVERS) в процессе
теста, его ручки меняются на ходу. Перезапуски — у встроенного сервера: stream-tts запускает тот же поддельный сервер отдельным
процессом как «python -m espeech» (пакет-заглушка и venv во временной папке), Docker-версию это не трогает."""
import os, site, subprocess, sys, urllib.parse, venv

import numpy as np
import pytest

from conftest import FAST_ENV, Ctx
from fake_cosyvoice import ADAPTER_HZ, VOICES, FakeCosy
from harness import SR, StreamTTS, decode, dominant_hz, make_app, make_wav, read_wav, sine, wait_for

E2E = os.path.dirname(os.path.abspath(__file__))
VOICE = "alpha"


def upload(api, data, filename, kind="item", voice=VOICE):
    return api.request("POST", "/api/train/upload", data=data, timeout=60, headers={
        "Content-Type": "application/octet-stream", "X-Filename": urllib.parse.quote(filename),
        "X-Voice": urllib.parse.quote(voice), "X-Kind": kind})


def add_items(api, n, sec=3.0, voice=VOICE, text="раз два три"):
    """n реплик 44,1 кГц стерео (stream-tts приводит к 24 кГц моно) с текстом. → [id]."""
    ids = []
    for i in range(n):
        r = upload(api, make_wav(sine(300 + i, sec, sr=44100), 44100, 2), f"line{i}.wav", voice=voice)
        assert r.status == 200 and r.json()["ok"], r.text
        ids.append(r.json()["item"]["id"])
    if text:
        api.ok("/api/train/items", dict(voice=voice, items=[dict(id=i, text=f"{text} {n}") for n, i in enumerate(ids)]))
    return ids


def train_set(api, voice=VOICE):
    r = api.get("/api/train?voice=" + urllib.parse.quote(voice))
    assert r.status == 200 and r.json()["ok"], r.text
    return r.json()


def status(api):
    r = api.get("/api/train/status")
    assert r.status == 200 and r.json()["ok"], r.text
    return r.json()["train"]


def until(api, cond, what, timeout=20):
    return wait_for(lambda: (lambda t: t if cond(t) else None)(status(api)), timeout, what)


def listen(api, adapter, phrase=0, voice=VOICE):
    return api.get(f"/api/train/synth?voice={voice}&adapter={adapter}&phrase={phrase}", timeout=30)


def hz_of(r):
    assert r.status == 200, r.text
    return dominant_hz(decode(r.body))


@pytest.fixture(scope="module")
def tr_shared(loop, tmp_path_factory):
    esp = loop.run(FakeCosy(engine="espeech", train=True).start())
    server = StreamTTS(str(tmp_path_factory.mktemp("train")), None,
                       dict(FAST_ENV, STREAM_TTS_TEST_SERVERS=f"espeech={esp.url}")).start()
    ctx = Ctx(loop, server, esp)
    ctx.idle(30)
    yield ctx
    server.stop()
    loop.run(esp.close())


@pytest.fixture
def tr(tr_shared):
    """Обучения нет, наборы пусты, ручки поддельного сервера — в исходном состоянии."""
    ctx = tr_shared
    ctx.api.ok("/api/train/discard")
    for v in VOICES:
        ctx.api.ok("/api/train/clear", dict(voice=v))
    ctx.idle()
    ctx.cosy.reset()
    yield ctx
    ctx.close()
    assert ctx.cosy.max_active <= 1, "к серверу синтеза шли параллельные синтезы"


def test_not_available_without_train(tts):
    """CosyVoice обучения не умеет: страница прячется, API отказывает."""
    assert not tts.api.state()["cosy"]["capabilities"].get("train")
    r = tts.api.get("/api/train?voice=alpha").json()
    assert not r["ok"] and r["error"] == "обучение LoRA есть только у модели ESpeech, сервер которой его поддерживает"


def test_set_items(tr):
    """Реплики файлами: звук приводится к 24 кГц моно PCM16, текст (пара «имя.txt») — следом; короче 0,3 с и длиннее
    30 с — отказ; удаление; набор переживает чтение заново, у каждого голоса — свой."""
    api = tr.api
    assert tr.api.state()["cosy"]["capabilities"]["train"]
    s = train_set(api)
    assert s["items"] == [] and s["total_sec"] == 0 and s["est_steps"] is None and s["train"]["state"] == "idle"
    a, b = add_items(api, 2, sec=4.0, text="")
    item = train_set(api)["items"][0]
    assert item["id"] == a and item["name"] == "line0.wav" and abs(item["sec"] - 4.0) < 0.05 and item["text"] == ""
    x, sr, ch = read_wav(api.get(f"/api/train/item?voice={VOICE}&id={a}").body)
    assert (sr, ch) == (SR, 1) and abs(len(x) - 4 * SR) < SR * 0.05 and abs(dominant_hz(x) - 300) < 3
    api.ok("/api/train/items", dict(voice=VOICE, items=[dict(id=b, text="при+вет, чат")]))
    s = train_set(api)
    assert [i["text"] for i in s["items"]] == ["", "при+вет, чат"] and abs(s["total_sec"] - 8.0) < 0.1
    assert s["est_steps"] == 500 and s["recommended_sec"] == 60

    r = upload(api, make_wav(sine(300, 0.2)), "short.wav")
    assert r.json()["error"] == "в файле нет звука или реплика короче 0,3 с"
    r = upload(api, make_wav(sine(300, 31)), "long.wav")
    assert r.json()["error"].startswith("реплика длиннее 30 с")
    r = upload(api, b"not audio" * 100, "bad.wav")
    assert r.json()["error"].startswith("не удалось прочитать звук")

    api.ok("/api/train/items/delete", dict(voice=VOICE, ids=[a]))
    assert [i["id"] for i in train_set(api)["items"]] == [b]
    assert api.get(f"/api/train/item?voice={VOICE}&id={a}").status == 404
    assert train_set(api, "bravo")["items"] == []
    assert api.get("/api/train?voice=nosuch").json()["error"] == "нет такого голоса"
    assert not tr.cosy.called("train")


def test_set_cut_from_recording(tr):
    """Запись для нарезки: реплика — копия отрезка (с затуханием на краях); удаление записи реплики не трогает."""
    api = tr.api
    rec = make_wav(np.concatenate([sine(500, 10, sr=44100), sine(650, 10, sr=44100)]), 44100, 2)
    r = upload(api, rec, "стрим.wav", kind="source")
    assert r.status == 200 and r.json()["ok"], r.text
    src = r.json()["source"]
    assert src["name"] == "стрим.wav" and abs(src["dur"] - 20) < 0.05
    x, sr, _ = read_wav(api.get(f"/api/train/source?voice={VOICE}&id={src['id']}").body)
    assert sr == SR and abs(len(x) - 20 * SR) < SR * 0.05
    r = api.ok("/api/train/cut", dict(voice=VOICE, src=src["id"], start=11, end=14.5, text="кусок"))
    it = r["item"]
    assert it["src"] == src["id"] and (it["start"], it["end"]) == (11, 14.5) and abs(it["sec"] - 3.5) < 0.01
    assert it["name"] == "стрим.wav 0:11.0–0:14.5" and it["text"] == "кусок"
    y, _, _ = read_wav(api.get(f"/api/train/item?voice={VOICE}&id={it['id']}").body)
    assert abs(dominant_hz(y) - 650) < 3 and abs(y[0]) < 1e-3
    assert api.post("/api/train/cut", dict(voice=VOICE, src=src["id"], start=1, end=1.2)).json()["error"] == \
        "реплика короче 0,3 с"
    assert api.post("/api/train/cut", dict(voice=VOICE, src=src["id"], start=0, end=40)).json()["ok"]   # до конца записи
    assert api.post("/api/train/cut", dict(voice=VOICE, src="nosuch0", start=0, end=2)).json()["error"] == \
        "нет такой записи"
    api.ok("/api/train/source/delete", dict(voice=VOICE, id=src["id"]))
    s = train_set(api)
    assert s["sources"] == [] and len(s["items"]) == 2 and "src" not in s["items"][0]
    assert api.get(f"/api/train/item?voice={VOICE}&id={it['id']}").status == 200


def test_train_stop_listen_save(tr):
    """Старт: на сервер уходит весь набор WAV PCM16 моно 24 кГц парами audio + text, без steps — шаги по объёму;
    ход; меньше 30 реплик — без проверки (пояснение); остановка; прослушивание «последний» и «без LoRA», «лучшего» нет;
    сохранение — голос звучит с адаптером, пример голоса синтезируется заново; сброс."""
    api, fake = tr.api, tr.cosy
    r = api.post("/api/train/start", dict(voice=VOICE))
    assert r.json()["error"] == "в наборе нет реплик"
    ids = add_items(api, 3, text="")
    api.ok("/api/train/items", dict(voice=VOICE, items=[dict(id=ids[0], text="раз")]))
    assert api.post("/api/train/start", dict(voice=VOICE)).json()["error"] == "нет текста у реплик: 2, 3"
    api.ok("/api/train/items", dict(voice=VOICE, items=[dict(id=i, text=f"  фраза\n{n}  ") for n, i in enumerate(ids)]))
    assert api.post("/api/train/start", dict(voice=VOICE, steps=0)).json()["error"] == "шагов — от 1 до 20000"

    fake.train_rate = 1
    t = api.ok("/api/train/start", dict(voice=VOICE, checkpointing=False))["train"]
    assert t["state"] == "running" and t["voice"] == VOICE and t["steps"] == 500
    call = fake.called("train")[-1]
    assert call["items"] == 3 and call["formats"] == [(SR, 1, 2)] * 3 and call["steps"] is None
    assert call["texts"] == ["фраза 0", "фраза 1", "фраза 2"] and call["checkpointing"] == "0"
    t = until(api, lambda t: t["step"] >= 50 and t["loss_history"], "хода обучения")
    assert t["val_text"].startswith("Без проверки: для неё нужно от 30 реплик") and not t["best"]
    assert t["limits"]["vram_need_mb"] == dict(default=2800, checkpointing=500) and not t["gpu"]["checkpointing_advised"]
    wait_for(lambda: (api.state()["cosy"]["training"] or {}).get("state") == "running", 10, "обучения в /api/state")

    # второй старт, пока идёт обучение, — отказ сервера, понятный текст
    r = api.post("/api/train/start", dict(voice=VOICE)).json()
    assert r["code"] == "busy" and r["error"] == "на сервере синтеза уже идёт обучение"
    # «последний» во время обучения — можно слушать
    assert abs(hz_of(listen(api, "last")) - VOICES[VOICE][0] * ADAPTER_HZ["last"]) < 3

    t = api.ok("/api/train/stop")["train"]
    assert t["state"] == "done" and t["finished"] == "stopped" and t["snapshots"] == ["last"]
    assert abs(hz_of(listen(api, "none")) - VOICES[VOICE][0]) < 3
    assert abs(hz_of(listen(api, "last")) - VOICES[VOICE][0] * ADAPTER_HZ["last"]) < 3
    n = len(fake.tts)
    assert listen(api, "last").status == 200 and len(fake.tts) == n   # после конца — из кэша
    r = listen(api, "best")
    assert r.status == 400 and r.text == "нет лучшего снимка: обучение без проверки"
    assert all(q.get("seed") == 7 for q in fake.tts if q.get("adapter"))   # seed примеров: снимки различаются только адаптером

    assert api.post("/api/train/save", dict(snapshot="best")).json()["error"] == "нет такого результата обучения"
    r = api.ok("/api/train/save", dict(snapshot="last"))
    assert r["voice"] == VOICE and r["snapshot"] == "last" and r["backup"].endswith(".espeech.bak")
    assert fake.trained == {VOICE: "last"}
    assert status(api)["saved"]["snapshot"] == "last"
    # пример голоса — заново, уже с адаптером
    wait_for(lambda: any(q["voice"] == VOICE and not q.get("adapter") and q["ts"] > fake.called("train_save")[-1]["ts"]
                         for q in fake.tts), 20, "нового примера голоса после сохранения")
    t = api.ok("/api/train/discard")["train"]
    assert t["state"] == "idle" and fake.called("train_discard")
    r = listen(api, "last")
    assert r.status == 400 and r.text == "нет результата обучения"


def test_validation_overfit_best(tr):
    """30 реплик — сервер откладывает проверку: лучший снимок и «пора остановиться»; «лучший» звучит своим снимком и
    сохраняется; шаги заданы вручную."""
    api, fake = tr.api, tr.cosy
    add_items(api, 30, sec=2.0)
    s = train_set(api)
    assert len(s["items"]) == 30 and s["est_steps"] == 800   # 25 × 2 с без проверки = 0,83 мин
    fake.train_overfit_at = 200
    fake.train_rate = 50
    api.ok("/api/train/start", dict(voice=VOICE, steps=700))
    assert fake.called("train")[-1]["steps"] == "700"
    t = until(api, lambda t: t["state"] == "done", "конца обучения")
    assert t["finished"] == "steps" and t["step"] == 700 and t["data"]["val_items"] == 5
    assert "val_text" not in t and t["best"]["step"] == 200 and t["overfit"] and t["overfit_step"] == 400
    assert [v["step"] for v in t["val_history"]] == list(range(0, 800, 100))
    assert sorted(t["snapshots"]) == ["best", "last"]
    assert abs(hz_of(listen(api, "best", 1)) - VOICES[VOICE][0] * ADAPTER_HZ["best"]) < 3
    api.ok("/api/train/save", dict(snapshot="best"))
    assert fake.called("train_save")[-1]["step"] == 200


def test_unsaved_restart_and_oom(tr):
    """Несохранённые результаты: новый старт — код unsaved, с discard — сначала сброс. Нехватка видеопамяти — текст
    с советом включить экономию; с экономией обучение идёт. Мало свободной видеопамяти — сервер советует экономию."""
    api, fake = tr.api, tr.cosy
    add_items(api, 2)
    fake.train_rate = 100
    api.ok("/api/train/start", dict(voice=VOICE, steps=300))
    until(api, lambda t: t["state"] == "done", "конца обучения")
    r = api.post("/api/train/start", dict(voice=VOICE)).json()
    assert r["code"] == "unsaved" and r["error"].startswith("есть несохранённые результаты прошлого обучения")
    fake.train_oom = True
    fake.train_free_mb = 2000
    t = api.ok("/api/train/start", dict(voice=VOICE, discard=True))["train"]
    assert fake.called("train_discard") and t["gpu"]["checkpointing_advised"]
    t = until(api, lambda t: t["state"] == "error", "ошибки oom")
    assert t["error"] == "oom" and t["text"] == ("Не хватило видеопамяти: закройте игры и программы на видеокарте и "
                                                 "запустите обучение снова — или включите экономию видеопамяти")
    t = api.ok("/api/train/start", dict(voice=VOICE, checkpointing=True))["train"]
    assert fake.called("train")[-1]["checkpointing"] == "1" and t["checkpointing"]
    t = until(api, lambda t: t["state"] == "done", "обучения с экономией видеопамяти")
    assert not t["error"] and t["step"] == 500


# --- перезапуски встроенного сервера ---
def fake_espeech(root):
    """Пакет-заглушка espeech (python -m espeech запускает fake_cosyvoice.serve) и venv рядом: запуск ESpeech
    (engines/espeech/launch.py) ищет пакет вверх от интерпретатора. Пакеты — из окружения тестов."""
    pkg = os.path.join(root, "espeech")
    os.makedirs(pkg)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    open(os.path.join(pkg, "server.py"), "w").close()
    with open(os.path.join(pkg, "__main__.py"), "w", encoding="utf-8") as f:
        f.write("import asyncio, site, sys\n"
                f"sys.path.insert(0, {E2E!r})\n"
                f"for p in {site.getsitepackages()!r}:\n    site.addsitedir(p)\n"
                "import fake_cosyvoice\nasyncio.run(fake_cosyvoice.serve(sys.argv[1:]))\n")
    env_dir = os.path.join(root, ".venv")
    venv.create(env_dir, with_pip=False)
    return os.path.join(env_dir, "Scripts", "python.exe") if os.name == "nt" else os.path.join(env_dir, "bin", "python")


@pytest.fixture(scope="module")
def managed(loop, tmp_path_factory):
    wd = str(tmp_path_factory.mktemp("train-managed"))
    python = fake_espeech(os.path.join(wd, "fake"))
    os.makedirs(os.path.join(wd, "models", "espeech"))
    open(os.path.join(wd, "models", "espeech", "espeech_fp16.safetensors"), "wb").close()
    server = StreamTTS(wd, None, dict(FAST_ENV, ESPEECH_SERVER=python, FAKE_TRAIN_RATE="1")).start()
    ctx = Ctx(loop, server, None)
    wait_for(lambda: (lambda c: c["ok"] and c["runtime"]["state"] == "running")(ctx.api.state()["cosy"]), 60,
             f"встроенного поддельного espeech-server\n{server.tail()}")
    yield ctx
    server.stop()


def restarted(api, pid, what):
    return wait_for(lambda: (lambda c: c["ok"] and c["runtime"]["state"] == "running" and c["runtime"]["pid"] != pid
                             and c["capabilities"].get("train"))(api.state()["cosy"]), 60, what)


def test_restart_guard(managed):
    """Во время обучения действия с перезапуском встроенного сервера отказывают (устройство, выгрузка модели,
    наборы голосов, проверка скорости); с несохранёнными результатами — спрашивают (confirm), с force — перезапуск, и
    результатов больше нет; сохранённые результаты перезапуску не мешают. Сервер упал во время обучения — страница
    узнаёт, что результаты пропали."""
    api = managed.api
    c = api.state()["cosy"]
    assert c["engine"] == "espeech" and c["runtime"]["pid"] and c["capabilities"]["train"]
    add_items(api, 2)
    api.ok("/api/train/start", dict(voice=VOICE, steps=20000))
    until(api, lambda t: t["state"] == "running" and t["step"] > 0, "хода обучения")
    for path, body in (("/api/backend", dict(backend="cpu")), ("/api/models/unload", dict(id="tts")),
                       ("/api/voicesets/delete", dict(name="wc3")), ("/api/bench", dict(all=True)),
                       ("/api/cuda/download", {}), ("/api/backend", dict(backend="cpu", force=True))):
        r = api.post(path, body)
        assert r.status == 409 and not r.json()["confirm"], (path, r.text)
        assert r.json()["error"].startswith(f"Идёт обучение LoRA голоса {VOICE}: перезапуск сервера синтеза его прервёт")
    assert api.post("/api/bench", dict(all=False)).json()["error"].startswith("идёт обучение LoRA")
    assert api.state()["cosy"]["runtime"]["prefer"] == "auto"

    api.ok("/api/train/stop")
    r = api.post("/api/backend", dict(backend="cpu"))
    assert r.status == 409 and r.json()["confirm"]
    assert r.json()["error"].startswith(f"Результаты обучения LoRA голоса {VOICE} не сохранены в голос")
    pid = api.state()["cosy"]["runtime"]["pid"]
    api.ok("/api/backend", dict(backend="cpu", force=True))
    restarted(api, pid, "перезапуска сервера на процессоре")
    t = status(api)
    assert t["state"] == "idle" and t["lost"] is None   # потеря подтверждена

    # сохранённое — перезапуск без вопросов
    api.ok("/api/train/start", dict(voice=VOICE, steps=20000))
    until(api, lambda t: t["step"] > 0, "хода обучения")
    api.ok("/api/train/stop")
    api.ok("/api/train/save", dict(snapshot="last"))
    pid = api.state()["cosy"]["runtime"]["pid"]
    api.ok("/api/backend", dict(backend="auto"))
    restarted(api, pid, "перезапуска сервера после сохранения")
    assert status(api)["lost"] is None

    # сервер упал во время обучения: «результаты пропали»
    api.ok("/api/train/start", dict(voice=VOICE, steps=20000))
    until(api, lambda t: t["step"] > 0, "хода обучения")
    pid = api.state()["cosy"]["runtime"]["pid"]
    subprocess.run(["taskkill", "/F", "/PID", str(pid)] if os.name == "nt" else ["kill", "-9", str(pid)],
                   capture_output=True)
    restarted(api, pid, "перезапуска упавшего сервера")
    t = until(api, lambda t: t["lost"], "признака «результаты пропали»")
    assert t["lost"] == VOICE and t["state"] == "idle"
    api.ok("/api/train/discard")
    assert status(api)["lost"] is None


# Каждый POST /api/* — в одной из групп; новый роут без группы роняет test_every_route_guarded.
# Перезапускают или останавливают встроенный сервер: во время обучения — 409 от _restart_guard (тело — такое, с
# которым действие дошло бы до перезапуска).
RESTARTS = {"/api/backend": dict(backend="cpu"), "/api/engine": dict(engine="espeech"),
            "/api/models/unload": dict(id="tts"), "/api/bench": dict(all=True),
            "/api/voicesets/delete": dict(name="wc3"), "/api/voicesets/import": None,   # None — пустой zip сырым телом
            "/api/cuda/download": {}, "/api/espeech/download": {}, "/api/espeech/delete": {},
            "/api/package/delete": "restart"}
# Сервер не трогают: после всех запросов (тело — {} или указанное) тот же процесс сервера и обучение идёт.
KEEPS = {"/api/settings": {}, "/api/source": {}, "/api/voice": {}, "/api/demos/rebuild": {},
         "/api/twitch/client_id": {}, "/api/twitch/mode": {}, "/api/twitch/logout": {}, "/api/reward": {},
         "/api/reward/delete": {}, "/api/enabled": dict(on=True), "/api/skip": {}, "/api/clear": {}, "/api/remove": {},
         "/api/test": {}, "/api/ban": {}, "/api/profanity": {}, "/api/model/cancel": {},
         "/api/model/use": {},       # перезапуск — только когда выбрана CosyVoice, а обучение — у ESpeech
         "/api/models/load": dict(id="tts"), "/api/accent": {}, "/api/accents": {}, "/api/accents/delete": {},
         "/api/draft": {}, "/api/draft/accent": {}, "/api/draft/source/delete": {}, "/api/draft/save": {},
         "/api/draft/discard": {}, "/api/draft/upload": None, "/api/update/dismiss": {}}
# Не проверяются здесь: свои ручки обучения (/api/train/*, выше) и запросы в сеть. Загрузка модели и пакетов
# перезапускает сервер только у CosyVoice.
SKIPS = ("/api/train/", "/api/ui-edit", "/api/twitch/login", "/api/update/check", "/api/model/download",
         "/api/package/download")


def post_routes():
    """POST /api/* приложения stream-tts — из самого приложения (web.make_app), а не списком в тесте."""
    web, app = make_app()
    return web, sorted({r.resource.canonical for r in app.router.routes()
                        if r.method == "POST" and r.resource.canonical.startswith("/api/")})


def send(api, web, path, body):
    if path in web.UPLOADS:
        return api.request("POST", path, data=b"", headers={"Content-Type": web.UPLOADS[path][-1]})
    return api.post(path, body)


def test_every_route_guarded(managed):
    """Ни один роут админки не перезапускает встроенный сервер во время обучения LoRA молча: обучение и его
    результаты живут в процессе сервера (AGENTS.md, _restart_guard в web/common.py). Роуты берутся из приложения, так что
    перенос роута между модулями web/ или новый роут без проверки этот тест заметит."""
    web, routes = post_routes()
    unknown = [p for p in routes if p not in RESTARTS and p not in KEEPS and not p.startswith(SKIPS)]
    assert not unknown, (f"новые роуты: {unknown} — в RESTARTS (перезапускают сервер: нужен _restart_guard), "
                         "в KEEPS (не трогают его) или в SKIPS (запрос в сеть)")
    gone = [p for p in (*RESTARTS, *KEEPS) if p not in routes]
    assert not gone, f"роутов больше нет: {gone}"
    from streamtts import downloads   # sys.path — из make_app
    restart_pkg = next(pid for pid, p in downloads.PACKAGES.items() if p.get("restart"))

    api = managed.api
    add_items(api, 2)
    api.ok("/api/train/start", dict(voice=VOICE, steps=20000))
    t = until(api, lambda t: t["state"] == "running" and t["step"] > 0, "хода обучения")
    pid = api.state()["cosy"]["runtime"]["pid"]
    for path, body in RESTARTS.items():
        r = send(api, web, path, dict(id=restart_pkg) if body == "restart" else body)
        assert r.status == 409 and r.json()["error"].startswith("Идёт обучение LoRA"), (path, r.status, r.text)
    for path, body in KEEPS.items():
        r = send(api, web, path, body)
        assert r.status < 500, (path, r.status, r.text)
    step = t["step"]
    t = until(api, lambda t: t["step"] > step + 5, "хода обучения после запросов")   # и отложенные (смена модели)
    assert t["state"] == "running" and api.state()["cosy"]["runtime"]["pid"] == pid
    api.ok("/api/train/stop")
    api.ok("/api/train/discard")
