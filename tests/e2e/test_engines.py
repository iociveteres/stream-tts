"""Модели синтеза (engines/): выбор CosyVoice / ESpeech / Supertonic 3 при готовых серверах (STREAM_TTS_TEST_SERVERS),
настройки модели по схеме params из /status, голоса без файла для текущей модели, свои примеры у каждой модели
(страница голосов называет модель), «как прочитает»; запуск встроенного supertonic-server (пакет-заглушка) и отказ
редактора голоса у Supertonic.

Три поддельных сервера: cosyvoice (alpha, bravo, charlie), espeech (alpha, charlie — у bravo нет .espeech) и
supertonic (alpha, bravo)."""
import asyncio, os, sys

import pytest

from conftest import FAST_ENV, Ctx
from fake_cosyvoice import VOICES, FakeCosy, fake_python
from harness import STREAM_TTS, StreamTTS, voice_of, wait_for
from test_editor import add_source, set_variants, synth, variant

sys.path.insert(0, STREAM_TTS)
from streamtts import paths  # noqa: E402
from streamtts.engines.espeech import launch  # noqa: E402
from streamtts.engines.supertonic import launch as st_launch  # noqa: E402
from streamtts.runtime import Unavailable  # noqa: E402

ESPEECH_VOICES = {k: VOICES[k] for k in ("alpha", "charlie")}
SUPERTONIC_VOICES = {k: VOICES[k] for k in ("alpha", "bravo")}
ST_REFUSAL = ("модель Supertonic 3 не делает голос из записи: у неё только готовые голоса. Чтобы сделать голос, "
              "выберите другую модель на вкладке «Сервер»")


class Two(Ctx):
    def __init__(self, loop, server, cosy, esp, st=None):
        super().__init__(loop, server, cosy)
        self.esp, self.st = esp, st

    def use(self, engine):
        self.api.ok("/api/engine", dict(engine=engine))
        wait_for(lambda: (lambda c: c["engine"] == engine and c["ok"] and not c["switching"])(self.api.state()["cosy"]),
                 20, f"смены модели на {engine}")
        self.idle()


@pytest.fixture(scope="module")
def two_shared(loop, tmp_path_factory):
    cosy = loop.run(FakeCosy().start())
    esp = loop.run(FakeCosy(engine="espeech", voices=ESPEECH_VOICES).start())
    st = loop.run(FakeCosy(engine="supertonic", voices=SUPERTONIC_VOICES).start())
    servers = f"cosyvoice={cosy.url},espeech={esp.url},supertonic={st.url}"
    server = StreamTTS(str(tmp_path_factory.mktemp("engines")), None,
                       dict(FAST_ENV, STREAM_TTS_TEST_SERVERS=servers)).start()
    ctx = Two(loop, server, cosy, esp, st)
    ctx.idle()
    yield ctx
    server.stop()
    for fake in (cosy, esp, st):
        loop.run(fake.close())


@pytest.fixture
def two(two_shared):
    """Модель CosyVoice, очередь пуста; записи запросов обоих серверов — заново."""
    ctx = two_shared
    if ctx.api.state()["cosy"]["engine"] != "cosyvoice":
        ctx.use("cosyvoice")
    ctx.api.ok("/api/settings", dict(espeech_steps=16, streaming=True))
    ctx.idle()
    ctx.cosy.reset()
    ctx.esp.reset()
    ctx.st.reset()
    yield ctx
    ctx.close()
    assert all(f.max_active <= 1 for f in (ctx.cosy, ctx.esp, ctx.st)), "к серверу синтеза шли параллельные запросы"


def test_engines_listed(two):
    c = two.api.state()["cosy"]
    assert c["engine"] == "cosyvoice" and c["title"] == "CosyVoice3"
    assert [(e["id"], e["available"]) for e in c["engines"]] == \
        [("cosyvoice", True), ("espeech", True), ("supertonic", True)]
    assert c["params"] == [] and not c["capabilities"].get("accent")   # у cosyvoice-server схемы нет


def test_switch_model(two):
    """Смена модели: голоса — с сервера ESpeech, озвучка идёт на него, запросы с nfe_steps и без chunk_tokens;
    обратно — снова CosyVoice. Выбор в settings.json переживает перезапуск."""
    two.use("espeech")
    s = two.api.state()
    assert sorted(v["name"] for v in s["voices"]) == ["alpha", "charlie"]
    ov = two.overlay()
    rid = two.api.test("Фраза моделью ESpeech.", voice="charlie")
    assert voice_of(ov.played(rid)) == "charlie"
    req = two.esp.tts[-1]
    assert req["voice"] == "charlie" and req["nfe_steps"] == 16 and "chunk_tokens" not in req
    assert not [r for r in two.cosy.tts if r["text"].startswith("Фраза моделью")]
    two.use("cosyvoice")
    assert sorted(v["name"] for v in two.api.state()["voices"]) == sorted(VOICES)
    rid = two.api.test("Фраза снова моделью CosyVoice.", voice="bravo")
    assert voice_of(ov.played(rid)) == "bravo"
    assert "nfe_steps" not in two.cosy.tts[-1]


def test_model_params(two):
    """Настройки модели — по схеме params из /status: число шагов в каждом запросе, значение не из схемы — отказ."""
    two.use("espeech")
    p = two.api.state()["cosy"]["params"]
    assert [(x["key"], x["param"], x["value"], x["choices"]) for x in p] == \
        [("espeech_steps", "nfe_steps", 16, [12, 16, 32])]
    assert p[0]["title"] == "Качество синтеза"
    two.api.ok("/api/settings", dict(espeech_steps=32))
    assert two.api.state()["cosy"]["params"][0]["value"] == 32
    r = two.api.post("/api/settings", dict(espeech_steps=20))
    assert not r.json()["ok"]
    assert two.api.state()["cosy"]["params"][0]["value"] == 32
    ov = two.overlay()
    rid = two.api.test("Фраза с тридцатью двумя шагами.", voice="alpha")
    ov.played(rid)
    assert two.esp.tts[-1]["nfe_steps"] == 32


def test_voice_without_file_keeps_choice(two):
    """У bravo нет файла ESpeech: зритель с bravo получает голос по хешу среди голосов модели, но его выбор
    (users.voice) остаётся и снова работает после возврата к CosyVoice. !ttssf bravo — недоступен."""
    ov = two.overlay()
    login = "bravofan"
    rid = two.api.test("Выбираю голос браво.", login=login, voice="bravo")
    assert voice_of(ov.played(rid)) == "bravo"
    two.use("espeech")
    rid = two.api.test("Фраза без файла для ESpeech.", login=login)
    got = voice_of(ov.played(rid))
    assert got in ESPEECH_VOICES
    assert two.server.sql("SELECT voice FROM users WHERE user_id=?", f"test:{login}") == [dict(voice="bravo")]
    assert "bravo" not in [v["name"] for v in two.api.state()["voices"]]
    two.use("cosyvoice")
    rid = two.api.test("И снова браво.", login=login)
    assert voice_of(ov.played(rid)) == "bravo"


def test_demos_per_model(two):
    """Примеры голосов у каждой модели свои: после смены синтезируются сервером новой модели, при возврате —
    прежние, без повторного синтеза."""
    demo = "Новая фраза примеров для проверки моделей."
    two.api.ok("/api/settings", dict(demo_phrase=demo))
    two.idle()   # примеры CosyVoice с новой фразой
    two.esp.reset()
    two.use("espeech")
    assert sorted(r["voice"] for r in two.esp.tts if r["text"] == demo) == ["alpha", "charlie"]
    assert all(v["demo"] for v in two.api.state()["voices"])
    page = two.api.get("/voices/").text   # страница зрителей называет модель, чьи это голоса и примеры
    assert "<b>ESpeech</b>" in page and 'data-name="bravo"' not in page
    two.cosy.reset()
    two.use("cosyvoice")
    assert not [r for r in two.cosy.tts if r["text"] == demo]
    assert all(v["demo"] for v in two.api.state()["voices"])
    assert "<b>CosyVoice3</b>" in two.api.get("/voices/").text


def test_espeech_stream_threshold(two):
    """ESpeech режет фразу на куски сам и стримит с 6 с расчётного звука (72 символа), CosyVoice — с полутора кусков
    по chunk_tokens (~108 символов): фраза в 80 символов у CosyVoice целиком, у ESpeech потоком; короткая — целиком."""
    mid = "Средняя фраза для проверки порога стриминга, длиннее порога модели ESpeech тут."
    assert 72 < len(mid) < 108
    ov = two.overlay()
    rid = two.api.test(mid, voice="alpha")
    ov.played(rid)
    assert two.cosy.tts[-1]["stream"] is False
    two.use("espeech")
    for text, stream in ((mid, True), ("Короткая.", False)):
        rid = two.api.test(text, voice="alpha")
        ov.played(rid)
        assert two.esp.tts[-1]["text"] == text and two.esp.tts[-1]["stream"] is stream


def test_accent(two):
    """«Как прочитает»: текст после фильтров через POST /accent; у CosyVoice — отказ, кнопки нет, словаря ударений и
    кнопки «Ударения» в редакторе тоже."""
    r = two.api.post("/api/accent", dict(text="Привет, 2 мира"))
    assert not r.json()["ok"]
    assert two.api.get("/api/accents").json() == dict(ok=False, error="у модели CosyVoice3 нет словаря ударений")
    d = two.api.get("/api/draft").json()["engine"]
    assert (d["id"], d["min"], d["max"], d["packages"], d["accent"]) == ("cosyvoice", 2, 30, ["frontend", "music"], False)
    two.use("espeech")
    assert two.api.get("/api/draft").json()["engine"]["id"] == "espeech"
    assert two.api.state()["cosy"]["capabilities"]["accent"]
    r = two.api.ok("/api/accent", dict(text="Привет, 2 мира https://example.com"))
    assert r["normalized"] == "Привет, два мира" and r["accented"] == "Пр+ивет, дв+а м+ира"
    assert [c["text"] for c in two.esp.called("accent")] == ["Привет, 2 мира"]   # ссылка вырезана фильтром


def test_supertonic(two):
    """Supertonic 3: голоса — с его сервера, настройки модели по схеме (шаги и скорость речи) — в каждом запросе, без
    полей других моделей; значение не из схемы — отказ. Голоса из записи у модели нет: кнопки редактора нет."""
    two.use("supertonic")
    two.api.ok("/api/settings", dict(supertonic_steps=8, supertonic_speed=1.05))
    s = two.api.state()
    c = s["cosy"]
    assert c["title"] == "Supertonic 3" and sorted(v["name"] for v in s["voices"]) == ["alpha", "bravo"]
    assert not c["capabilities"]["extract"] and not c["capabilities"]["accent"]
    assert [(x["key"], x["param"], x["value"], x["choices"]) for x in c["params"]] == [
        ("supertonic_steps", "total_step", 8, [5, 8, 12, 16]),
        ("supertonic_speed", "speed", 1.05, [0.9, 1.0, 1.05, 1.15, 1.3])]
    assert [x["title"] for x in c["params"]] == ["Качество синтеза", "Скорость речи"]
    ov = two.overlay()
    rid = two.api.test("Фраза моделью Supertonic.", voice="bravo")
    assert voice_of(ov.played(rid)) == "bravo"
    req = two.st.tts[-1]
    assert (req["voice"], req["total_step"], req["speed"]) == ("bravo", 8, 1.05)
    assert "chunk_tokens" not in req and "nfe_steps" not in req
    two.api.ok("/api/settings", dict(supertonic_steps=16, supertonic_speed=1.3))
    for bad in (dict(supertonic_steps=7), dict(supertonic_speed=1.2)):
        assert not two.api.post("/api/settings", bad).json()["ok"]
    assert [x["value"] for x in two.api.state()["cosy"]["params"]] == [16, 1.3]
    rid = two.api.test("Фраза с шестнадцатью шагами.", voice="alpha")
    ov.played(rid)
    assert (two.st.tts[-1]["total_step"], two.st.tts[-1]["speed"]) == (16, 1.3)
    two.api.ok("/api/settings", dict(supertonic_steps=8, supertonic_speed=1.05))


def test_supertonic_stream_threshold(two):
    """Supertonic стримит с 10 с расчётного звука (120 символов): короче фраза пришла бы одним куском текста, а целиком
    она ещё и выравнивается по громкости."""
    mid = "Средняя фраза для проверки порога стриминга Supertonic: длиннее порога ESpeech, но короче своего."
    long = mid + " А эта уже длиннее ста двадцати символов и идёт потоком."
    assert 72 < len(mid) < 120 < len(long)
    two.use("supertonic")
    ov = two.overlay()
    for text, stream in ((mid, False), (long, True)):
        rid = two.api.test(text, voice="alpha")
        ov.played(rid)
        assert two.st.tts[-1]["text"] == text and two.st.tts[-1]["stream"] is stream


def test_supertonic_editor_refuses(two):
    """Редактор голоса у Supertonic 3 отказывает понятно: черновик знает, что голоса из записи нет (админка показывает
    подсказку вместо редактора), синтез варианта и сохранение — отказ без запросов к серверу; черновик остаётся."""
    two.use("supertonic")
    api = two.api
    api.ok("/api/draft/discard")
    try:
        d = api.get("/api/draft").json()["engine"]
        assert (d["id"], d["title"], d["extract"], d["packages"]) == ("supertonic", "Supertonic 3", False, [])
        src = add_source(api)
        set_variants(api, variant(src, "a", 1, 12))
        r = synth(api, variant="a")
        assert r.status >= 400 and r.text == ST_REFUSAL
        r = api.post("/api/draft/save", dict(variant="a", name="Fresh", set="mine")).json()
        assert r == dict(ok=False, error=ST_REFUSAL)
        assert not two.st.calls and not [x for x in two.st.tts if x["voice"].startswith("_draft-")]
        assert [v["id"] for v in api.get("/api/draft").json()["variants"]] == ["a"]
        two.use("cosyvoice")
        assert api.get("/api/draft").json()["engine"]["extract"]
    finally:
        api.ok("/api/draft/discard")


def test_supertonic_launch(tmp_path, monkeypatch):
    """Запуск supertonic-server: пакет — вверх от интерпретатора SUPERTONIC_SERVER (PYTHONPATH), без файлов модели —
    Unavailable с папкой модели (причина видна в админке), с моделью — python -m supertonic с папками модели и голосов.
    Устройство одно — процессор, выбирать нечего."""
    root = tmp_path / "srv"
    (root / "supertonic").mkdir(parents=True)
    (root / "supertonic" / "server.py").write_text("")
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.write_text("")
    models, voices = tmp_path / "models", tmp_path / "voices"
    monkeypatch.setattr(paths, "MODELS", str(models))
    monkeypatch.setattr(paths, "VOICES", str(voices))
    monkeypatch.setenv("SUPERTONIC_SERVER", str(root / ".venv"))
    assert st_launch.find_server() == str(python)
    L = st_launch.Launcher(str(python))
    assert L.env()["PYTHONPATH"] == str(root)
    with pytest.raises(Unavailable, match="нет модели Supertonic 3"):
        asyncio.run(L.args("127.0.0.1", 5))
    for f in st_launch.MODEL_FILES:
        p = models / "supertonic" / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
    (voices / "supertonic").mkdir(parents=True)
    for v in ("F1", "M1"):
        (voices / "supertonic" / f"{v}.supertonic").write_text("{}")
    assert asyncio.run(L.args("127.0.0.1", 5)) == [
        str(python), "-m", "supertonic", "--models", str(models / "supertonic"), "--voices", str(voices),
        "--host", "127.0.0.1", "--port", "5"]
    p = L.public()
    assert (p["devices"], p["backends"], p["model"], p["voices"], L.DEVICE_KEY) == (["cpu"], [], "Supertonic 3", 2, "")


def test_supertonic_managed(loop, tmp_path):
    """Встроенный supertonic-server (пакет-заглушка, python -m supertonic): без модели ждёт с причиной, модель
    появилась — запускается сам, голоса — файлы .supertonic, фраза звучит; выбор устройства отказывает."""
    python = fake_python(str(tmp_path / "fake"), "supertonic", "supertonic")
    wd = tmp_path / "wd"
    (wd / "voices" / "supertonic").mkdir(parents=True)
    for v in ("F1", "M1"):
        (wd / "voices" / "supertonic" / f"{v}.supertonic").write_text("{}")
    # Модель — заранее: из исходников stream-tts находит и espeech/.venv проекта, и первой доступной была бы ESpeech
    (wd / "data").mkdir()
    (wd / "data" / "settings.json").write_text('{"engine": "supertonic"}', encoding="utf-8")
    server = StreamTTS(str(wd), None, dict(FAST_ENV, SUPERTONIC_SERVER=python)).start(synth=False)
    ctx = Ctx(loop, server, None)
    try:
        rt = wait_for(lambda: (lambda c: c["runtime"]["state"] == "waiting" and c)(ctx.api.state()["cosy"]), 30,
                      f"ожидания модели\n{server.tail()}")
        assert rt["engine"] == "supertonic" and "нет модели Supertonic 3" in rt["runtime"]["error"]
        assert rt["runtime"]["devices"] == ["cpu"]
        for f in st_launch.MODEL_FILES:
            p = wd / "models" / "supertonic" / f
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}")
        wait_for(lambda: (lambda c: c["ok"] and c["runtime"]["state"] == "running")(ctx.api.state()["cosy"]), 60,
                 f"запуска поддельного supertonic-server\n{server.tail()}")
        s = ctx.api.state()
        assert sorted(v["name"] for v in s["voices"]) == ["F1", "M1"]
        assert s["cosy"]["runtime"]["model"] == "Supertonic 3" and s["cosy"]["runtime"]["backend"] == "CPU"
        ov = ctx.overlay()
        ov.played(ctx.api.test("Встроенный Supertonic.", voice="F1"))
        r = ctx.api.post("/api/backend", dict(backend="cpu")).json()
        assert r == dict(ok=False, error="у модели Supertonic 3 устройство не выбирается")
    finally:
        ctx.close()
        server.stop()


def test_switch_unknown_model(two):
    r = two.api.post("/api/engine", dict(engine="nope"))
    assert not r.json()["ok"]
    assert two.api.state()["cosy"]["engine"] == "cosyvoice"


def test_device_choice():
    """--device сервера ESpeech: auto отдаём torch (nvcuda.dll бывает и без рабочего драйвера — тогда «Found no NVIDIA
    driver» и пустые голоса), без CUDA в бэкендах — cpu. Устройство из /status уточняет догадку админки."""
    assert launch.device("auto", True) == "auto"
    assert launch.device("auto", False) == "cpu"
    assert launch.device("cuda", True) == "cuda"
    assert launch.device("cuda", False) == "cpu"
    assert launch.device("cpu", True) == "cpu"

    L = launch.Launcher("python.exe")
    L.backend = "CUDA0"
    L.sync(dict(device="cpu"))     # auto на машине без рабочего CUDA
    assert L.backend == "CPU"
    L.sync(dict(device="cuda"))
    assert L.backend == "CUDA0"
    L.sync({})                     # сервер ещё не сказал — оставляем как было
    assert L.backend == "CUDA0"


def test_engine_survives_restart(loop, tmp_path):
    """Выбранная модель — в settings.json: после перезапуска stream-tts работает та же."""
    cosy = loop.run(FakeCosy().start())
    esp = loop.run(FakeCosy(engine="espeech", voices=ESPEECH_VOICES).start())
    env = dict(FAST_ENV, STREAM_TTS_TEST_SERVERS=f"cosyvoice={cosy.url},espeech={esp.url}")
    server = StreamTTS(str(tmp_path), None, env).start()
    try:
        ctx = Two(loop, server, cosy, esp)
        ctx.use("espeech")
        server.stop()
        server = StreamTTS(str(tmp_path), None, env).start()
        assert Ctx(loop, server, cosy).api.state()["cosy"]["engine"] == "espeech"
    finally:
        server.stop()
        loop.run(cosy.close())
        loop.run(esp.close())
