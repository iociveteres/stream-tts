"""Модели синтеза (engines/): выбор CosyVoice / ESpeech при двух внешних серверах (TTS_SERVERS), настройки модели
по схеме params из /status, голоса без файла для текущей модели, свои примеры у каждой модели, «как прочитает».

Два поддельных сервера: cosyvoice (alpha, bravo, charlie) и espeech (alpha, charlie — у bravo нет .espeech)."""
import pytest

from conftest import FAST_ENV, Ctx
from fake_cosyvoice import VOICES, FakeCosy
from harness import StreamTTS, dominant_hz, wait_for

ESPEECH_VOICES = {k: VOICES[k] for k in ("alpha", "charlie")}


def voice_of(it):
    hz = dominant_hz(it.audio)
    return next(n for n, (f, _) in VOICES.items() if abs(f - hz) < 3)


class Two(Ctx):
    def __init__(self, loop, server, cosy, esp):
        super().__init__(loop, server, cosy)
        self.esp = esp

    def use(self, engine):
        self.api.ok("/api/engine", dict(engine=engine))
        wait_for(lambda: (lambda c: c["engine"] == engine and c["ok"] and not c["switching"])(self.api.state()["cosy"]),
                 20, f"смены модели на {engine}")
        self.idle()


@pytest.fixture(scope="module")
def two_shared(loop, tmp_path_factory):
    cosy = loop.run(FakeCosy().start())
    esp = loop.run(FakeCosy(engine="espeech", voices=ESPEECH_VOICES).start())
    server = StreamTTS(str(tmp_path_factory.mktemp("engines")), None,
                       dict(FAST_ENV, TTS_SERVERS=f"cosyvoice={cosy.url},espeech={esp.url}")).start()
    ctx = Two(loop, server, cosy, esp)
    ctx.idle()
    yield ctx
    server.stop()
    loop.run(cosy.close())
    loop.run(esp.close())


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
    yield ctx
    ctx.close()
    assert ctx.cosy.max_active <= 1 and ctx.esp.max_active <= 1, "к серверу синтеза шли параллельные запросы"


def test_engines_listed(two):
    c = two.api.state()["cosy"]
    assert c["engine"] == "cosyvoice" and c["title"] == "CosyVoice3"
    assert [(e["id"], e["available"]) for e in c["engines"]] == [("cosyvoice", True), ("espeech", True)]
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
        [("espeech_steps", "nfe_steps", 16, [16, 32, 12])]
    assert p[0]["title"] == "Шагов синтеза"
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
    two.cosy.reset()
    two.use("cosyvoice")
    assert not [r for r in two.cosy.tts if r["text"] == demo]
    assert all(v["demo"] for v in two.api.state()["voices"])


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


def test_switch_unknown_model(two):
    r = two.api.post("/api/engine", dict(engine="nope"))
    assert not r.json()["ok"]
    assert two.api.state()["cosy"]["engine"] == "cosyvoice"


def test_engine_survives_restart(loop, tmp_path):
    """Выбранная модель — в settings.json: после перезапуска stream-tts работает та же."""
    cosy = loop.run(FakeCosy().start())
    esp = loop.run(FakeCosy(engine="espeech", voices=ESPEECH_VOICES).start())
    env = dict(FAST_ENV, TTS_SERVERS=f"cosyvoice={cosy.url},espeech={esp.url}")
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
