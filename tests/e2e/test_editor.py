"""Редактор голоса (editor.py, /api/draft*): записи, варианты образца, синтез ячеек, сохранение голоса, черновик.

Поддельный cosyvoice с ONNX-фронтендом (фикстура editor): голос из образца звучит основной частотой образца. Запись
теста — два тона, 500 Гц в первой половине и 650 Гц во второй, поэтому варианты из разных половин различаются по
звуку синтеза."""
import os, re, threading

import numpy as np

from conftest import EDITOR_VOICES
from fake_cosyvoice import FRONTEND, VOICES
from harness import SR, decode, dominant_hz, ffmpeg, make_wav, read_wav, sine, wait_for

DEMO_SEED = 7
HALF = 18.0   # с: граница тонов записи


def recording():
    """36 с: 500 Гц, потом 650 Гц; 44,1 кГц стерео — stream-tts переводит в 24 кГц моно."""
    return make_wav(np.concatenate([sine(500, HALF, sr=44100), sine(650, HALF, sr=44100)]), 44100, 2)


def upload(api, data, filename="запись.wav"):
    return api.upload("/api/draft/upload", data, filename)


def add_source(api, data=None, filename="запись.wav"):
    r = upload(api, data or recording(), filename)
    assert r.status == 200 and r.json()["ok"], r.text
    return r.json()["source"]


def set_variants(api, *variants, **extra):
    r = api.post("/api/draft", dict(variants=list(variants), **extra))
    assert r.status == 200, r.text
    return r.json()


def variant(src, vid, start, end, text="раз два три", **kw):
    return dict(dict(id=vid, src=src["id"], start=start, end=end, text=text, denoise=False), **kw)


def synth(api, phrase=0, variant=None, voice=None):
    q = f"phrase={phrase}" + (f"&variant={variant}" if variant else "") + (f"&voice={voice}" if voice else "")
    return api.get(f"/api/draft/synth?{q}", timeout=30)


def draft_voices(state):
    return [v["name"] for v in state["voices"] if v["name"].startswith("_draft-")]


# --- записи ---
def test_upload_converts_to_24k_mono(editor):
    """WAV 44,1 кГц стерео и mp3 48 кГц через ffmpeg становятся 24 кГц моно PCM16."""
    src = add_source(editor.api)
    assert src["name"] == "запись.wav" and abs(src["dur"] - 2 * HALF) < 0.05
    x, sr, ch = read_wav(editor.api.get(f"/api/draft/source/{src['id']}.wav").body)
    assert (sr, ch) == (SR, 1) and abs(len(x) - 2 * HALF * SR) < SR * 0.05
    assert abs(dominant_hz(x[:SR * 10]) - 500) < 2 and abs(dominant_hz(x[-SR * 10:]) - 650) < 2

    mp3 = ffmpeg(["-f", "wav", "-i", "-", "-ar", "48000", "-ac", "2", "-c:a", "libmp3lame", "-f", "mp3", "-"],
                 make_wav(sine(300, 5.0)))
    src2 = add_source(editor.api, mp3, "clip.mp3")
    assert abs(src2["dur"] - 5.0) < 0.1
    x, sr, ch = read_wav(editor.api.get(f"/api/draft/source/{src2['id']}.wav").body)
    assert (sr, ch) == (SR, 1) and abs(dominant_hz(x) - 300) < 3
    d = editor.api.get("/api/draft").json()
    assert [s["id"] for s in d["sources"]] == [src["id"], src2["id"]]
    assert editor.api.get("/api/draft/source/nosuch.wav").status == 404


def test_upload_errors_and_limit(editor):
    """Битый файл и слишком короткая запись — понятная ошибка; записей не больше MAX_SOURCES (10)."""
    r = upload(editor.api, b"definitely not audio" * 100, "broken.wav")
    assert r.status == 400 and r.json()["error"].startswith("не удалось прочитать звук"), r.text
    r = upload(editor.api, make_wav(sine(500, 1.0)), "short.wav")
    assert r.json()["error"] == "в файле нет звука или он короче 2 с"
    short = make_wav(sine(500, 2.5))
    for i in range(10):
        add_source(editor.api, short, f"s{i}.wav")
    r = upload(editor.api, short, "s10.wav")
    assert r.status == 400 and r.json()["error"] == "записей в черновике не больше 10"
    assert len(editor.api.get("/api/draft").json()["sources"]) == 10
    sid = editor.api.get("/api/draft").json()["sources"][0]["id"]
    editor.api.ok("/api/draft/source/delete", dict(id=sid))
    add_source(editor.api, short, "again.wav")


# --- варианты образца ---
def test_variant_sample(editor):
    """Вариант — отрезок записи с затуханием по краям, по желанию с шумоподавлением; /api/draft/sample отдаёт его."""
    src = add_source(editor.api)
    set_variants(editor.api, variant(src, "a", 1, 6), variant(src, "b", 20, 26, denoise=True))
    x, sr, ch = read_wav(editor.api.get("/api/draft/sample?variant=a").body)
    assert (sr, ch) == (SR, 1) and abs(len(x) - 5 * SR) <= 1
    assert abs(dominant_hz(x) - 500) < 2 and abs(x[0]) < 0.01 and abs(x[-1]) < 0.01
    y, _, _ = read_wav(editor.api.get("/api/draft/sample?variant=b").body)
    assert abs(len(y) - 6 * SR) <= 1 and abs(dominant_hz(y) - 650) < 2
    assert editor.api.get("/api/draft/sample?variant=zzz").status == 404
    # отрезок за концом записи укорачивается до её конца, перевёрнутый и чужая запись — ошибка
    d = set_variants(editor.api, variant(src, "a", 30, 99))["draft"]
    assert d["variants"][0]["end"] == 2 * HALF
    for bad, err in ((variant(src, "a", 6, 1), "неверный отрезок"),
                     (dict(variant(src, "a", 1, 6), src="nosuch"), "неверный вариант"),
                     (dict(variant(src, "a", 1, 6), id="../x"), "неверный вариант")):
        r = editor.api.post("/api/draft", dict(variants=[bad]))
        assert r.status == 400 and r.json()["error"] == err


def test_sample_checks_before_synth(editor):
    """Отрезок 2–30 с и непустая расшифровка: иначе ошибка, к серверу синтеза запросов нет."""
    src = add_source(editor.api)
    set_variants(editor.api, variant(src, "short", 1, 2.5), variant(src, "long", 0, 31),
                 variant(src, "notext", 1, 6, text="  "))
    r = synth(editor.api, variant="short")
    assert r.status == 400 and r.text == "образец 1.5 с: нужно от 2 до 30 с"
    assert synth(editor.api, variant="long").text == "образец 31.0 с: нужно от 2 до 30 с"
    assert synth(editor.api, variant="notext").text == "нет расшифровки образца"
    assert synth(editor.api, variant="nosuch").status == 400
    assert synth(editor.api, phrase=99, variant="short").text == "нет такой фразы"
    assert not editor.cosy.calls and not editor.cosy.tts


# --- синтез ячеек ---
def test_synth_cell(editor):
    """Синтез ячейки: фронтенд грузится перед первым извлечением, вариант регистрируется как _draft-<ключ>,
    звучит своим образцом и тем же seed, что примеры; изменённый вариант регистрируется заново, а прежний
    выгружается; повтор — из кэша; служебные голоса в список голосов не попадают."""
    api, cosy = editor.api, editor.cosy
    phrases = api.state()["settings"]["test_phrases"]
    src = add_source(api)
    set_variants(api, variant(src, "a", 1, 6, text="раз   два\nтри"), variant(src, "b", 20, 26))

    r = synth(api, variant="a")
    assert r.status == 200 and r.headers["Content-Type"] == "audio/mpeg", r.text
    assert abs(dominant_hz(decode(r.body)) - 500) < 5
    calls = [c["call"] for c in cosy.calls]
    assert calls == ["frontend_load", "extract"]
    assert cosy.calls[0]["body"] == FRONTEND
    ex = cosy.called("extract")[0]
    assert re.fullmatch(r"_draft-[0-9a-f]{12}", ex["name"]) and ex["text"] == "раз два три"
    assert (ex["sr"], ex["channels"], ex["width"]) == (SR, 1, 2) and abs(ex["frames"] - 5 * SR) <= 1
    assert [(t["voice"], t["text"], t["seed"], t["stream"]) for t in cosy.tts] == \
        [(ex["name"], phrases[0], DEMO_SEED, False)]

    # второй вариант: фронтенд уже загружен, звук — другой половины записи
    assert abs(dominant_hz(decode(synth(api, variant="b").body)) - 650) < 5
    assert [c["call"] for c in cosy.calls] == ["frontend_load", "extract", "extract"]
    # повтор — из кэша
    n = len(cosy.tts)
    assert synth(api, variant="a").status == 200 and len(cosy.tts) == n
    assert api.get("/api/draft").json()["variants"][0]["ready"] == [0]

    # изменённый вариант: новое имя, прежнее выгружено
    set_variants(api, variant(src, "a", 1, 6, text="другая расшифровка"), variant(src, "b", 20, 26))
    assert synth(api, variant="a").status == 200
    new = cosy.called("extract")[-1]["name"]
    assert new != ex["name"] and new in cosy.voices and ex["name"] not in cosy.voices
    assert [c["name"] for c in cosy.called("delete")] == [ex["name"]]
    assert api.get("/api/draft").json()["variants"][0]["ready"] == [0]

    # список голосов перечитан после регистрации: служебных в нём нет
    lists = cosy.lists
    wait_for(lambda: cosy.lists >= lists + 2, 5, "список голосов перечитан")
    assert draft_voices(api.state()) == [] and "_draft-" not in api.get("/voices/").text


def test_compare_voice(editor):
    """Голос для сравнения: та же тестовая фраза готовым голосом, тем же seed; из кэша при повторе."""
    api, cosy = editor.api, editor.cosy
    phrases = api.state()["settings"]["test_phrases"]
    r = synth(api, phrase=1, voice="bravo")
    assert r.status == 200 and abs(dominant_hz(decode(r.body)) - VOICES["bravo"][0]) < 5
    assert [(t["voice"], t["text"], t["seed"]) for t in cosy.tts] == [("bravo", phrases[1], DEMO_SEED)]
    assert synth(api, phrase=1, voice="bravo").status == 200 and len(cosy.tts) == 1
    d = set_variants(api, compare="bravo")["draft"]
    assert d["compare"] == "bravo" and d["compare_ready"] == [1]
    assert synth(api, voice="nobody").text == "нет такого голоса"
    assert not cosy.calls   # фронтенд для сравнения не нужен


# --- сохранение ---
def test_save_voice(editor):
    """Сохранение с внешним сервером: имя проверяется, .gguf — в COSYVOICE_VOICES на машине сервера, набор — в
    <данные>/sets/<набор>/manifest.json, образец — в <данные>/samples/; голос сразу в списке и в своём разделе."""
    api, cosy = editor.api, editor.cosy
    src = add_source(api)
    set_variants(api, variant(src, "a", 1, 6, text="  образец  голоса "))
    for name, set_name, err in (("голос", "my", "имя голоса: латиница, цифры, «-», «_» и «.», до 64 символов"),
                                ("my voice", "my", "имя голоса"), ("-dash", "my", "имя голоса"),
                                ("x" * 65, "my", "имя голоса"), ("okname", "!!!", "укажите набор"),
                                ("alpha", "my", "голос alpha уже есть: выберите другое имя")):
        r = api.post("/api/draft/save", dict(variant="a", name=name, set=set_name))
        assert r.status == 400 and r.json()["error"].startswith(err), (name, r.text)
    assert not cosy.called("extract") and not cosy.called("save")

    r = api.post("/api/draft/save", dict(variant="a", name="Fresh.Voice_1", set="My Set"))
    assert r.status == 200, r.text
    path = f"{EDITOR_VOICES}/Fresh.Voice_1.gguf"
    assert r.json() == dict(ok=True, name="Fresh.Voice_1", set="my-set", path=path)
    assert [c["name"] for c in cosy.called("extract")] == ["Fresh.Voice_1"]
    assert [(c["name"], c["path"]) for c in cosy.called("save")] == [("Fresh.Voice_1", path)]
    data = editor.server.data
    with open(os.path.join(data, "sets", "my-set", "manifest.json"), encoding="utf-8") as f:
        assert f.read().count("Fresh.Voice_1") == 1
    x, sr, ch = read_wav(open(os.path.join(data, "samples", "Fresh.Voice_1.wav"), "rb").read())
    assert (sr, ch) == (SR, 1) and abs(len(x) - 5 * SR) <= 1
    with open(os.path.join(data, "samples", "Fresh.Voice_1.txt"), encoding="utf-8") as f:
        assert f.read() == "образец голоса\n"
    s = api.state()
    assert "Fresh.Voice_1" in [v["name"] for v in s["voices"]] and not draft_voices(s)
    assert "my-set" in api.get("/api/draft").json()["sets"]
    assert re.search(r"<h2>MY-SET <span>1</span></h2><ul><li data-name=\"Fresh.Voice_1\"", api.get("/voices/").text)
    editor.idle()   # пример нового голоса — его голосом
    r = api.post("/api/draft/save", dict(variant="a", name="fresh.voice_1", set="other"))
    assert r.json()["error"] == "голос fresh.voice_1 уже есть: выберите другое имя"


def test_save_server_error_unregisters(editor):
    """/speaker/save не удался: ошибка видна, голос не остаётся зарегистрированным на сервере и не появляется в
    списке."""
    api, cosy = editor.api, editor.cosy
    src = add_source(api)
    set_variants(api, variant(src, "a", 1, 6))
    cosy.fail_save = True
    r = api.post("/api/draft/save", dict(variant="a", name="Broken_1", set="x"))
    assert r.status == 400 and r.json()["error"].startswith("сервер синтеза: cosyvoice 500"), r.text
    assert [c["name"] for c in cosy.called("delete")] == ["Broken_1"] and "Broken_1" not in cosy.voices
    assert "Broken_1" not in [v["name"] for v in api.state()["voices"]]
    assert not os.path.exists(os.path.join(editor.server.data, "samples", "Broken_1.wav"))


# --- черновик ---
def test_draft_survives_restart_and_discard(spawn):
    """Черновик (записи, варианты, синтез) переживает перезапуск; «Удалить черновик» выгружает служебные голоса."""
    server, cosy = spawn(frontend=True)
    from harness import Api
    api = Api(server.base)
    src = add_source(api)
    set_variants(api, variant(src, "a", 1, 6), variant(src, "b", 20, 26), name="future", set="mine")
    assert synth(api, variant="a").status == 200 and synth(api, variant="b").status == 200
    before = api.get("/api/draft").json()
    drafts = sorted(n for n in cosy.voices if n.startswith("_draft-"))
    assert len(drafts) == 2
    server.stop()

    again, _ = spawn(workdir=server.dir, cosy=cosy)
    api = Api(again.base)
    after = api.get("/api/draft").json()
    for k in ("sources", "variants", "name", "set"):
        assert after[k] == before[k], k
    assert [v["ready"] for v in after["variants"]] == [[0], [0]]
    n = len(cosy.tts)
    assert synth(api, variant="b").status == 200 and len(cosy.tts) == n   # синтез из кэша на диске
    # изменённый после перезапуска вариант: прежний служебный голос тоже выгружается
    old_a = cosy.called("extract")[0]["name"]
    set_variants(api, variant(src, "a", 2, 7), variant(src, "b", 20, 26))
    assert synth(api, variant="a").status == 200
    assert [c["name"] for c in cosy.called("delete")] == [old_a]
    drafts = sorted(n for n in cosy.voices if n.startswith("_draft-"))
    assert len(drafts) == 2 and old_a not in drafts

    cosy.calls.clear()
    api.ok("/api/draft/discard")
    assert sorted(c["name"] for c in cosy.called("delete")) == drafts
    assert not any(n.startswith("_draft-") for n in cosy.voices)
    d = api.get("/api/draft").json()
    assert d["sources"] == [] and d["variants"] == [] and d["name"] == ""
    assert not os.path.exists(os.path.join(again.data, "draft"))
    assert set(cosy.voices) == set(VOICES)


def test_no_frontend(tts):
    """Сервер без ONNX-фронтенда (404 на /frontend/model): черновик показывает это, синтез и сохранение — понятная
    ошибка без запросов извлечения."""
    api = tts.api
    try:
        src = add_source(api)
        d = set_variants(api, variant(src, "a", 1, 6))["draft"]
        assert d["frontend"] is False and d["managed"] is False
        r = synth(api, variant="a")
        assert r.status == 400 and r.text == "на сервере синтеза нет фронтенда для создания голосов: скачайте пакет"
        r = api.post("/api/draft/save", dict(variant="a", name="nofront", set="x"))
        assert r.json()["error"] == "на сервере синтеза нет фронтенда для создания голосов: скачайте пакет"
        assert not tts.cosy.calls and not tts.cosy.tts
    finally:
        api.ok("/api/draft/discard")


def test_editor_does_not_interrupt_live(editor):
    """Синтез редактора (приоритет «послушать») ждёт живую фразу на синтезе и не останавливает её."""
    api, cosy = editor.api, editor.cosy
    src = add_source(api)
    set_variants(api, variant(src, "a", 1, 6))
    ov = editor.overlay()
    cosy.hold = "живая"
    rid = api.test("живая фраза до редактора", voice="alpha")
    wait_for(lambda: cosy.tts, 5, "живая фраза на синтезе")
    got = {}
    th = threading.Thread(target=lambda: got.update(r=synth(api, variant="a")))
    th.start()
    try:
        wait_for(lambda: api.state()["cosy"]["waiting"] == 1, 5, "синтез редактора в очереди к серверу")
        assert not cosy.calls and cosy.stops == 0
    finally:
        cosy.hold = False
        th.join(30)
    ov.played(rid)
    assert got["r"].status == 200 and cosy.stops == 0
    live, cell = cosy.tts[0], cosy.tts[1]
    assert live["text"] == "живая фраза до редактора" and not live["stopped"]
    assert cosy.called("extract")[0]["ts"] >= live["ts"] and cell["voice"].startswith("_draft-")


def test_music_needs_package(editor):
    """«Убрать музыку» без пакета htdemucs: черновик говорит, что недоступно, синтез такого варианта — ошибка."""
    src = add_source(editor.api)
    d = set_variants(editor.api, variant(src, "m", 1, 6, music=True))["draft"]
    assert d["music"] is False and d["variants"][0]["music"] is True
    r = synth(editor.api, variant="m")
    assert r.status == 400 and r.text == "для «убрать музыку» скачайте пакет «Очистка образца от музыки»"
    assert editor.api.get("/api/draft/sample?variant=m").status == 404
    assert not editor.cosy.calls


def test_frontend_unload_and_reload(editor):
    """Фронтенд в «Моделях в памяти»: после извлечения загружен, кнопка выгружает его, следующее извлечение снова
    грузит."""
    api, cosy = editor.api, editor.cosy
    src = add_source(api)
    set_variants(api, variant(src, "a", 1, 6), variant(src, "b", 20, 26))
    assert synth(api, variant="a").status == 200
    fe = lambda: next((m for m in api.state()["models"] if m["id"] == "frontend"), None)
    assert fe()["loaded"] is True and fe()["unload"] is True
    api.ok("/api/models/unload", dict(id="frontend"))
    assert cosy.called("frontend_unload") and not cosy.frontend_loaded
    assert fe()["loaded"] is False
    assert synth(api, variant="b").status == 200
    assert [c["call"] for c in cosy.calls] == ["frontend_load", "extract", "frontend_unload", "frontend_load",
                                               "extract"]
