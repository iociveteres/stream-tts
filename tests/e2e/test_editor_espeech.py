"""Редактор голоса с моделью ESpeech (engines/espeech/editor.py) и словарь ударений пользователя (/api/accents).

Поддельный espeech-server (FakeCosy(engine="espeech")) — единственный сервер синтеза: голос из образца без
ONNX-фронтенда, текст образца — разметкой POST /accent («+» перед первой гласной слова), темп — полем tempo, файл
голоса — путь «<набор>/<имя>.espeech» от папки голосов сервера. Запись — та же, что в test_editor.py: 500 Гц в
первой половине, 650 Гц во второй."""
import os

import pytest

from conftest import FAST_ENV, Ctx
from fake_cosyvoice import FakeCosy
from harness import SR, StreamTTS, decode, dominant_hz, read_wav
from test_editor import HALF, add_source, set_variants, synth, variant

HOP = 256   # отсчётов на кадр мела ESpeech: темп — кадров на байт текста образца


def tempo_of(seconds, accented):
    text = accented.strip()
    text = text + " " if text.endswith(".") else text + ". "
    return (int(seconds * SR) // HOP) / len(text.encode())


@pytest.fixture(scope="module")
def esp_shared(loop, tmp_path_factory):
    esp = loop.run(FakeCosy(engine="espeech").start())
    server = StreamTTS(str(tmp_path_factory.mktemp("editor-espeech")), None,
                       dict(FAST_ENV, STREAM_TTS_TEST_SERVERS=f"espeech={esp.url}")).start()
    ctx = Ctx(loop, server, esp)
    ctx.idle(30)
    yield ctx
    server.stop()
    loop.run(esp.close())


@pytest.fixture
def esp(esp_shared):
    """Модель ESpeech, черновик и словарь ударений пусты."""
    ctx = esp_shared
    ctx.api.ok("/api/draft/discard")
    ctx.idle()
    ctx.cosy.reset()
    yield ctx
    ctx.close()
    assert ctx.cosy.max_active <= 1, "к серверу синтеза шли параллельные запросы"


def test_espeech_draft_limits(esp):
    """Черновик знает модель: образец 4–12 с (совет 6–7), пакеты — только очистка от музыки, есть ударения;
    отрезок вне пределов — ошибка до запросов к серверу."""
    assert not esp.api.state()["cosy"]["capabilities"].get("frontend")
    d = esp.api.get("/api/draft").json()
    assert d["engine"] == dict(id="espeech", title="ESpeech", min=4.0, max=12.0, ok=[6, 7], packages=["music"],
                               accent=True)
    src = add_source(esp.api)
    set_variants(esp.api, variant(src, "short", 1, 4.5), variant(src, "long", 0, 13))
    assert synth(esp.api, variant="short").text == "образец 3.5 с: нужно от 4 до 12 с"
    assert synth(esp.api, variant="long").text == "образец 13.0 с: нужно от 4 до 12 с"
    assert not esp.cosy.calls and not esp.cosy.tts


def test_espeech_accent_button(esp):
    """«Ударения» у расшифровки: разметка модели (POST /accent) без фильтров озвучки — это не сообщение чата."""
    r = esp.api.ok("/api/draft/accent", dict(text="  раз 2\nтри https://example.com  "))
    assert r["accented"] == "р+аз дв+а тр+и https://example.com" and r["normalized"] == "раз два три https://example.com"
    assert [c["text"] for c in esp.cosy.called("accent")] == ["раз 2 три https://example.com"]
    assert esp.api.post("/api/draft/accent", dict(text=" ")).json()["error"] == "нет расшифровки образца"


def test_espeech_synth_with_tempo(esp):
    """Вариант регистрируется без фронтенда, текст — с ударениями, темп — по отрезку для темпа (кадров на байт его
    размеченной расшифровки); без отрезка поля tempo нет. Изменённый отрезок для темпа — новый ключ и регистрация."""
    api, fake = esp.api, esp.cosy
    src = add_source(api)
    tempo = dict(start=0, end=HALF, text="раз два три четыре пять шесть")
    set_variants(api, variant(src, "a", 1, 7.5, tempo=tempo), variant(src, "b", 20, 26.5, text="р+аз дв+а"))
    r = synth(api, variant="a")
    assert r.status == 200, r.text
    assert abs(dominant_hz(decode(r.body)) - 500) < 5
    ex = fake.called("extract")
    assert [c["call"] for c in fake.calls] == ["accent", "accent", "extract"]   # текст образца и текст для темпа
    assert ex[0]["name"].startswith("_draft-") and ex[0]["text"] == "р+аз дв+а тр+и"
    want = tempo_of(HALF, "р+аз дв+а тр+и ч+етыре п+ять ш+есть")
    assert abs(float(ex[0]["tempo"]) - want) < 1e-6
    assert not fake.called("frontend_load")
    d = api.get("/api/draft").json()
    assert d["variants"][0]["tempo"] == tempo and d["variants"][0]["ready"] == [0]

    # текст с «+» — как есть, без запроса /accent; темп — по самому образцу (сервер считает сам)
    fake.reset()
    assert synth(api, variant="b").status == 200
    assert [c["call"] for c in fake.calls] == ["extract"]
    assert fake.called("extract")[0]["text"] == "р+аз дв+а" and fake.called("extract")[0]["tempo"] is None

    # другой отрезок для темпа — другой голос черновика, прежний выгружается
    fake.reset()
    set_variants(api, variant(src, "a", 1, 7.5, tempo=dict(tempo, end=HALF - 4)), variant(src, "b", 20, 26.5,
                                                                                           text="р+аз дв+а"))
    assert synth(api, variant="a").status == 200
    new = fake.called("extract")[0]
    assert new["name"] != ex[0]["name"] and ex[0]["name"] in [c["name"] for c in fake.called("delete")]
    assert abs(float(new["tempo"]) - tempo_of(HALF - 4, "р+аз дв+а тр+и ч+етыре п+ять ш+есть")) < 1e-6

    for bad, err in ((dict(start=5, end=2, text="x"), "неверный отрезок для темпа"),):
        r = api.post("/api/draft", dict(variants=[variant(src, "a", 1, 7.5, tempo=bad)]))
        assert r.status == 400 and r.json()["error"] == err
    set_variants(api, variant(src, "a", 1, 7.5, tempo=dict(start=0, end=HALF, text=" ")))
    assert synth(api, variant="a").text == "нет расшифровки отрезка для темпа"
    set_variants(api, variant(src, "a", 1, 7.5, tempo=dict(start=0, end=2 * HALF, text="раз")))   # 36 с — можно
    assert synth(api, variant="a").status == 200


def test_espeech_save(esp):
    """Сохранение: голос — «<набор>/<имя>.espeech» от папки голосов сервера, набор — в её manifest.json, образец и
    размеченный текст — в <данные>/samples/espeech/. Голос другой модели с тем же именем получает файл ESpeech только
    в своём наборе."""
    api, fake = esp.api, esp.cosy
    src = add_source(api)
    set_variants(api, variant(src, "a", 1, 7.5, tempo=dict(start=0, end=HALF, text="раз два три четыре")))
    r = api.ok("/api/draft/save", dict(variant="a", name="Esp_Voice", set="My Set"))
    assert r["set"] == "my-set" and r["path"] == "my-set/Esp_Voice.espeech"
    assert [(c["name"], c["path"]) for c in fake.called("save")] == [("Esp_Voice", "my-set/Esp_Voice.espeech")]
    ex = [c for c in fake.called("extract") if c["name"] == "Esp_Voice"][0]
    assert ex["text"] == "р+аз дв+а тр+и" and ex["tempo"]
    data = esp.server.data
    assert os.path.isfile(os.path.join(esp.server.dir, "voices", "my-set", "manifest.json"))
    x, sr, _ = read_wav(open(os.path.join(data, "samples", "espeech", "Esp_Voice.wav"), "rb").read())
    assert sr == SR and abs(len(x) - 6.5 * SR) <= 1
    assert open(os.path.join(data, "samples", "espeech", "Esp_Voice.txt"), encoding="utf-8").read() == "р+аз дв+а тр+и\n"
    assert "Esp_Voice" in [v["name"] for v in api.state()["voices"]]
    assert api.post("/api/draft/save", dict(variant="a", name="esp_voice", set="x")).json()["error"] == \
        "голос esp_voice уже есть: выберите другое имя"

    # у CosyVoice есть голос hero в наборе wc3 (voices/wc3/hero.gguf): ESpeech — только туда же
    os.makedirs(os.path.join(esp.server.dir, "voices", "wc3"), exist_ok=True)
    open(os.path.join(esp.server.dir, "voices", "wc3", "hero.gguf"), "wb").close()
    r = api.post("/api/draft/save", dict(variant="a", name="hero", set="other"))
    assert r.json()["error"] == "голос hero другой модели лежит в наборе wc3: сохраните в тот же набор"
    assert api.ok("/api/draft/save", dict(variant="a", name="hero", set="wc3"))["path"] == "wc3/hero.espeech"


def test_user_accents(esp):
    """Словарь ударений: слово перекрывает разметку модели в «Как прочитает» и в синтезе; неверное слово — отказ;
    удаление возвращает разметку модели."""
    api, fake = esp.api, esp.cosy
    assert esp.api.state()["cosy"]["capabilities"]["user_accents"]
    assert api.get("/api/accents").json() == dict(ok=True, words=[])
    assert api.ok("/api/accent", dict(text="Старый замок"))["accented"] == "Ст+арый з+амок"
    r = api.ok("/api/accents", dict(accented="ЗАМ+ОК"))
    assert r["word"] == "замок" and r["words"] == [dict(word="замок", accented="зам+ок")]
    assert api.ok("/api/accent", dict(text="Старый Замок"))["accented"] == "Ст+арый Зам+ок"
    ov = esp.overlay()
    rid = api.test("Старый замок на горе.", voice="alpha")
    ov.played(rid)
    assert "зам+ок" in fake.tts[-1]["accented"]
    assert api.post("/api/accents", dict(accented="zamok")).json()["error"] == \
        "одно слово русскими буквами, ударение — «+» перед гласной"
    assert api.ok("/api/accents/delete", dict(word="замок"))["words"] == []
    assert api.post("/api/accents/delete", dict(word="замок")).json()["error"] == "нет такого слова"
    assert api.ok("/api/accent", dict(text="Старый замок"))["accented"] == "Ст+арый з+амок"
