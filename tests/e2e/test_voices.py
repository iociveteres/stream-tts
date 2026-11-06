"""Голоса: голос по хешу ника, включение и выключение, новые и удалённые голоса на сервере, страница голосов и zip,
поправка громкости, примеры (смена фразы, живая фраза прерывает пример, «послушать»), группы по наборам, эффекты post.

Голос фразы узнаётся по частоте синуса поддельного cosyvoice (fake_cosyvoice.VOICES)."""
import hashlib, io, json, os, re, shutil, sys, threading, zipfile

import pytest

from fake_cosyvoice import VOICES
from harness import STREAM_TTS, band_share, decode, dominant_hz, voice_of, wait_for

sys.path.insert(0, STREAM_TTS)
from streamtts import audio  # noqa: E402

DEMO_SEED = 7   # voices.DEMO_SEED
LONG = ("Это длинная фраза для проверки стриминга: она заметно длиннее полутора кусков по сто пятьдесят токенов, "
        "поэтому звук идёт по частям.")


def by_hash(login, pool):
    """Как voices.by_hash: голос зрителя, который ещё не выбирал голос."""
    pool = sorted(pool)
    return pool[int.from_bytes(hashlib.sha1(login.lower().encode()).digest()[:8], "big") % len(pool)]


def voices(tts):
    return {v["name"]: v for v in tts.api.state()["voices"]}


def sections(page):
    """Страница голосов: [(заголовок раздела, [голоса])]."""
    out = []
    for m in re.finditer(r"<section><h2>(.*?) <span>\d+</span></h2>(.*?)</section>", page, re.S):
        out.append((m[1], re.findall(r'<li data-name="([^"]+)"', m[2])))
    return out


@pytest.fixture
def project(tts):
    """Папка проекта процесса (STREAM_TTS_PROJECT): voice-refs/ и effects/ теста убираются после него."""
    root = os.path.join(tts.server.dir, "project")
    yield root
    for d in ("voice-refs", "effects", "tools"):
        shutil.rmtree(os.path.join(root, d), ignore_errors=True)


def manifest(root, set_name, data, model="cosyvoice"):
    os.makedirs(os.path.join(root, "voice-refs", model, set_name), exist_ok=True)
    with open(os.path.join(root, "voice-refs", model, set_name, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


# --- голос зрителя ---
def test_voice_by_nick_hash(tts):
    """Без !ttssf и без voice зритель получает голос по хешу ника и сохраняет его (users.voice)."""
    ov = tts.overlay()
    login = "hashviewer"
    want = by_hash(login, VOICES)
    for text in ("первая фраза без выбора голоса", "вторая фраза тем же голосом"):
        rid = tts.api.test(text, login=login)
        assert voice_of(ov.played(rid)) == want
        assert tts.event(rid)["voice"] == want
    assert tts.server.sql("SELECT voice FROM users WHERE user_id=?", f"test:{login}") == [dict(voice=want)]


def test_disabled_voice_rehashed(tts):
    """Голос зрителя выключили — новый по хешу среди включённых; выключены все — возврат «нет голосов»."""
    ov = tts.overlay()
    login = "rehashviewer"
    first = by_hash(login, VOICES)
    rid = tts.api.test("фраза до выключения голоса", login=login)
    assert voice_of(ov.played(rid)) == first
    tts.api.ok("/api/voice", dict(name=first, enabled=False))
    rest = [v for v in VOICES if v != first]
    rid = tts.api.test("фраза после выключения голоса", login=login)
    assert voice_of(ov.played(rid)) == by_hash(login, rest)
    assert tts.server.sql("SELECT voice FROM users WHERE user_id=?", f"test:{login}")[0]["voice"] == by_hash(login, rest)
    tts.api.ok("/api/voice", dict(names=rest, enabled=False))
    rid = tts.api.test("голосов не осталось", login=login)
    e = tts.event(rid)
    assert (e["outcome"], e["reason"]) == ("refund", "нет голосов")
    assert tts.api.post("/api/test", dict(text="выключенным голосом", voice="alpha")).json()["ok"] is False


# --- список голосов ---
def test_server_voice_added_and_removed(tts):
    """Новый голос на сервере появляется в списке и получает пример; удалённый пропадает со страницы."""
    phrase = tts.api.state()["settings"]["demo_phrase"]
    tts.cosy.voices["delta"] = (550.0, 0.2)
    try:
        wait_for(lambda: voices(tts).get("delta", {}).get("demo"), 10, "голос delta с примером")
        demo = [r for r in tts.cosy.tts if r["voice"] == "delta"]
        assert [(r["text"], r["seed"], r["stream"]) for r in demo] == [(phrase, DEMO_SEED, False)]
        assert voices(tts)["delta"]["enabled"]
        assert 'data-name="delta"' in tts.api.get("/voices/").text
        mp3 = tts.api.get("/voices/demos/delta.mp3")
        assert mp3.status == 200 and abs(dominant_hz(decode(mp3.body)) - 550) < 5
        ov = tts.overlay()
        assert abs(dominant_hz(ov.played(tts.api.test("новым голосом", voice="delta")).audio) - 550) < 3
    finally:
        tts.cosy.voices.pop("delta", None)
    wait_for(lambda: "delta" not in voices(tts), 10, "голос delta пропал из списка")
    assert 'data-name="delta"' not in tts.api.get("/voices/").text
    assert tts.api.get("/voices/demos/delta.mp3").status == 404
    assert tts.api.post("/api/test", dict(text="удалённым голосом", voice="delta")).json()["ok"] is False


def test_enable_disable_all(tts):
    """«Выключить все» и «Включить все» (names): страница голосов и zip — только включённые голоса."""
    tts.api.ok("/api/voice", dict(names=["alpha", "bravo"], enabled=False))
    assert {n: v["enabled"] for n, v in voices(tts).items()} == dict(alpha=False, bravo=False, charlie=True)
    page = tts.api.get("/voices/").text
    assert re.findall(r'<li data-name="([^"]+)"', page) == ["charlie"]
    z = zipfile.ZipFile(io.BytesIO(tts.api.get("/voices.zip").body))
    assert sorted(z.namelist()) == ["demos/charlie.mp3", "index.html"]
    assert 'data-name="charlie"' in z.read("index.html").decode() and "!ttssf charlie" in z.read("index.html").decode()
    tts.api.ok("/api/voice", dict(names=list(VOICES), enabled=True))
    assert all(v["enabled"] for v in voices(tts).values())
    z = zipfile.ZipFile(io.BytesIO(tts.api.get("/voices.zip").body))
    assert sorted(z.namelist()) == ["demos/alpha.mp3", "demos/bravo.mp3", "demos/charlie.mp3", "index.html"]
    assert all(len(z.read(f"demos/{n}.mp3")) > 1000 for n in VOICES)
    r = tts.api.post("/api/voice", dict(names=["alpha", "nobody"], enabled=False))
    assert r.status == 400 and r.json()["error"] == "нет такого голоса"
    assert voices(tts)["alpha"]["enabled"]


# --- громкость ---
@pytest.mark.parametrize("streaming", [False, True], ids=["whole", "stream"])
@pytest.mark.parametrize("gain", [6, -6])
def test_gain_db(tts, gain, streaming):
    """Поправка gain_db: фраза громче или тише ровно на столько дБ (целиком и стримингом)."""
    text = LONG if streaming else "Проверка ручной поправки громкости голоса"
    ov = tts.overlay()
    base = audio.loudness(ov.played(tts.api.test(text, voice="alpha")).audio)
    tts.api.ok("/api/voice", dict(name="alpha", gain_db=gain))
    louder = audio.loudness(ov.played(tts.api.test(text, voice="alpha")).audio)
    assert [r["stream"] for r in tts.cosy.tts] == [streaming] * 2
    assert abs(base - (-16)) < 1
    assert abs(louder - base - gain) < 0.5, (base, louder)


def test_gain_db_limits(tts):
    tts.api.ok("/api/voice", dict(name="bravo", gain_db=50))
    assert voices(tts)["bravo"]["gain_db"] == 20
    tts.api.ok("/api/voice", dict(name="bravo", gain_db=-50))
    assert voices(tts)["bravo"]["gain_db"] == -20


# --- примеры ---
def test_demo_phrase_change_rebuilds_demos(tts):
    """Новая фраза примеров: все примеры синтезируются заново этой фразой и с тем же seed."""
    old = tts.api.state()["settings"]["demo_phrase"]
    new = "Новая фраза для проверки примеров голосов"
    try:
        tts.api.ok("/api/settings", dict(demo_phrase=new))
        assert not any(v["demo"] for v in voices(tts).values())
        tts.idle()
        assert sorted((r["voice"], r["seed"], r["stream"]) for r in tts.cosy.tts if r["text"] == new) == \
            [(v, DEMO_SEED, False) for v in sorted(VOICES)]
        assert all(r["text"] == new for r in tts.cosy.tts)
        page = tts.api.get("/voices/").text
        assert new in page and old not in page
        mp3 = tts.api.get("/voices/demos/bravo.mp3").body
        assert abs(len(decode(mp3)) / 24000 - len(new) / 12) < 0.2   # длина звука — по новой фразе
    finally:
        tts.api.ok("/api/settings", dict(demo_phrase=old))
        tts.idle()


def test_live_phrase_preempts_demo(tts):
    """Живая фраза не ждёт фоновый пример: его синтез останавливается (/tts/stop), фраза звучит первой, а пример
    досинтезируется потом."""
    phrase = tts.api.state()["settings"]["demo_phrase"]
    ov = tts.overlay()
    tts.cosy.hold = phrase
    tts.api.ok("/api/demos/rebuild")
    wait_for(lambda: tts.cosy.tts, 5, "пример на синтезе")
    demo = tts.cosy.tts[0]
    rid = tts.api.test("живая фраза важнее примера", voice="bravo")
    ov.played(rid)
    assert demo["stopped"] and tts.cosy.stops == 1
    assert tts.cosy.tts[1]["text"] == "живая фраза важнее примера"
    tts.cosy.hold = False
    tts.idle()
    redo = [r for r in tts.cosy.tts[2:] if r["voice"] == demo["voice"]]
    assert len(redo) == 1 and redo[0]["text"] == phrase and not redo[0]["stopped"]
    assert sorted(r["voice"] for r in tts.cosy.tts if r["text"] == phrase and not r["stopped"]) == sorted(VOICES)
    assert [it.id for it in ov.items] == [rid]


def test_preview_priority(tts):
    """«Послушать» (/api/preview) отдаёт mp3; к серверу его пускают после живой фразы, но раньше фоновых примеров.

    Пример alpha держится на синтезе, «послушать» charlie и живая фраза ждут; живая фраза останавливает пример и идёт
    первой, за ней «послушать», и только потом примеры. Пример charlie уже готов: второй раз его не синтезируют."""
    phrase = tts.api.state()["settings"]["demo_phrase"]
    ov = tts.overlay()
    tts.cosy.hold = phrase
    tts.api.ok("/api/demos/rebuild")
    wait_for(lambda: tts.cosy.tts, 5, "пример на синтезе")
    assert tts.cosy.tts[0]["voice"] == "alpha"
    got = {}
    th = threading.Thread(target=lambda: got.update(r=tts.api.get("/api/preview?name=charlie", timeout=30)))
    th.start()
    try:
        wait_for(lambda: tts.api.state()["cosy"]["waiting"] == 1, 5, "«послушать» в очереди к серверу")
        rid = tts.api.test("живая фраза раньше всех", voice="bravo")
        ov.played(rid)
        wait_for(lambda: len(tts.cosy.tts) >= 3, 5, "«послушать» на синтезе")
        tts.cosy.hold = False
    finally:
        tts.cosy.hold = False
        th.join(30)
    r = got["r"]
    assert r.status == 200 and r.headers["Content-Type"] == "audio/mpeg"
    assert abs(dominant_hz(decode(r.body)) - VOICES["charlie"][0]) < 5
    tts.idle()
    order = [(t["voice"], t["text"] == phrase, t["stopped"]) for t in tts.cosy.tts]
    assert order[:3] == [("alpha", True, True), ("bravo", False, False), ("charlie", True, False)]
    assert sorted(order[3:]) == [("alpha", True, False), ("bravo", True, False)]
    assert tts.api.get("/api/preview?name=nobody").status == 404


# --- страница голосов: группы по наборам ---
def test_voice_groups(tts, project):
    """manifest.json в voice-refs/<модель>/<набор>/ проекта: разделы по наборам (источникам) в порядке GROUPS из
    папок обеих моделей, голоса без набора — «Другие»."""
    assert sections(tts.api.get("/voices/").text) == [("Другие", ["alpha", "bravo", "charlie"])]
    manifest(project, "dota2", {"bravo": {}})
    manifest(project, "wc3", {"charlie": {}})
    assert sections(tts.api.get("/voices/").text) == [
        ("Warcraft III", ["charlie"]), ("Dota 2", ["bravo"]), ("Другие", ["alpha"])]
    manifest(project, "morrowind", {"alpha": {}}, model="espeech")
    assert sections(tts.api.get("/voices/").text) == [
        ("Warcraft III", ["charlie"]), ("Morrowind", ["alpha"]), ("Dota 2", ["bravo"])]


# --- эффекты post ---
EFFECT = '''"""Тестовый эффект: добавляет тон с частотой из первого аргумента, аргументы — в last.json рядом."""
import json, os

import numpy as np


def apply(x, args):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "last.json"), "w", encoding="utf-8") as f:
        json.dump(args, f, ensure_ascii=False)
    t = np.arange(len(x)) / 24000
    return x + np.abs(x).max() * np.sin(2 * np.pi * float(args[0]) * t)
'''


def test_post_effect(tts, project):
    """post из manifest.json: эффект из effects/ применяется к фразе ({text} — текст фразы), голос с эффектом
    синтезируется целиком, даже длинная фраза."""
    os.makedirs(os.path.join(project, "effects"))
    with open(os.path.join(project, "effects", "testfx.py"), "w", encoding="utf-8") as f:
        f.write(EFFECT)
    manifest(project, "fx", {"bravo": {"post": ["effects/testfx.py", "600", "{text}"]}})
    assert voices(tts)["bravo"]["post"] == "effects/testfx.py 600 {text}"
    ov = tts.overlay()
    it = ov.played(tts.api.test(LONG, voice="bravo"))
    assert [r["stream"] for r in tts.cosy.tts] == [False]
    assert band_share(it.audio, 600) > 0.2 and band_share(it.audio, VOICES["bravo"][0]) > 0.2
    with open(os.path.join(project, "effects", "last.json"), encoding="utf-8") as f:
        assert json.load(f) == ["600", LONG]
    assert abs(audio.loudness(it.audio) - (-16)) < 1   # громкость выравнивается после эффекта


def test_post_outside_effects_rejected(tts, project):
    """Скрипт не из effects/*.py (manifest.json приходит и с наборами из zip) не запускается: ошибка синтеза."""
    os.makedirs(os.path.join(project, "tools"))
    marker = os.path.join(project, "tools", "ran")
    with open(os.path.join(project, "tools", "evil.py"), "w", encoding="utf-8") as f:
        f.write(f"open({marker!r}, 'w').close()\ndef apply(x, args):\n    return x\n")
    manifest(project, "evil", {"charlie": {"post": ["tools/evil.py"]},
                               "alpha": {"post": ["effects/../tools/evil.py"]}})
    tts.overlay()   # ошибку синтеза разбирает проигрыватель: без оверлея фраза ждёт в очереди
    for voice in ("charlie", "alpha"):
        e = tts.event(tts.api.test("эффект не из effects", voice=voice))
        assert (e["outcome"], e["reason"]) == ("refund", "ошибка синтеза")
    assert not os.path.exists(marker)
