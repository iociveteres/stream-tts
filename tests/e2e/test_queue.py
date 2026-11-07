"""Очередь озвучки через тестовую активацию (/api/test, кнопка «Тест» в админке): приём и отказы, фильтры, синтез
целиком и стримингом, громкость, обрезка, пропуск и очистка, оверлей.

Голос фразы узнаётся по частоте синуса поддельного cosyvoice (fake_cosyvoice.VOICES)."""
import sys, time

import pytest

from fake_cosyvoice import VOICES
from harness import STREAM_TTS, band_share, dominant_hz, wait_for

sys.path.insert(0, STREAM_TTS)
from streamtts import audio  # noqa: E402

LONG = ("Это длинная фраза для проверки стриминга: она заметно длиннее полутора кусков по сто пятьдесят токенов, "
        "поэтому звук идёт по частям.")


def texts(cosy):
    return [r["text"] for r in cosy.tts]


def refund(tts, rid, reason):
    e = tts.event(rid)
    assert (e["outcome"], e["reason"]) == ("refund", reason), e
    return e


# --- основной путь ---
def test_phrase_plays_in_voice(tts):
    ov = tts.overlay()
    rid = tts.api.test("Привет, это проверка голоса", login="viewer1", voice="bravo")
    it = ov.played(rid)
    assert abs(dominant_hz(it.audio) - VOICES["bravo"][0]) < 3
    assert it.seconds > 1
    e = tts.event(rid)
    assert e["outcome"] == "played" and e["voice"] == "bravo" and e["source"] == "test" and e["audio_ms"] > 1000
    assert texts(tts.cosy) == ["Привет, это проверка голоса"]
    assert tts.cosy.tts[0]["stream"] is False   # короткая фраза — целиком


def test_queue_order(tts):
    """Без оверлея фразы копятся; подключился — играют по порядку, каждая своим голосом."""
    plan = [("первая фраза в очереди", "alpha"), ("вторая фраза в очереди", "bravo"),
            ("третья фраза в очереди", "charlie")]
    ids = [tts.api.test(text, login=f"order{i}", voice=v) for i, (text, v) in enumerate(plan)]
    wait_for(lambda: len(tts.api.state()["engine"]["queue"]) == 3, 5, "три фразы в очереди")
    ov = tts.overlay()
    for rid, (_, voice) in zip(ids, plan):
        assert abs(dominant_hz(ov.played(rid).audio) - VOICES[voice][0]) < 3
    assert [it.id for it in ov.items] == ids


@pytest.mark.parametrize("voice", ["alpha", "charlie"])
def test_loudness_normalized(tts, voice):
    """Целиком синтезированная фраза доводится до −16 LUFS, как бы громко ни говорил голос."""
    ov = tts.overlay()
    it = ov.played(tts.api.test("Проверка громкости синтеза целиком", voice=voice))
    assert abs(audio.loudness(it.audio) - (-16)) < 1


# --- отказы при приёме ---
def test_disabled(tts):
    tts.api.ok("/api/enabled", dict(on=False))
    refund(tts, tts.api.test("не прозвучит", voice="alpha"), "озвучка выключена")
    assert not tts.cosy.tts


def test_banned(tts):
    tts.api.ok("/api/ban", dict(login="troll"))
    refund(tts, tts.api.test("не прозвучит", login="Troll", voice="alpha"), "бан")
    assert not tts.cosy.tts


def test_empty_after_filters(tts):
    refund(tts, tts.api.test("https://example.com www.test.ru", voice="alpha"), "пустой текст")
    assert not tts.cosy.tts


def test_queue_full(tts):
    tts.api.ok("/api/settings", dict(queue_limit=2))
    ids = [tts.api.test(f"фраза номер {i}", login=f"full{i}", voice="alpha") for i in range(3)]
    refund(tts, ids[2], "очередь полна")
    assert [q["id"] for q in tts.api.state()["engine"]["queue"]] == ids[:2]


# --- фильтры и текст ---
def test_links_stripped(tts):
    ov = tts.overlay()
    ov.played(tts.api.test("привет www.example.com и https://x.io/abc мир", voice="alpha"))
    assert texts(tts.cosy) == ["привет и мир"]


def test_profanity_beep(tts):
    """Мат вырезается: синтез кусками, на месте слова — тон 800 Гц, в журнале ***."""
    ov = tts.overlay()
    it = ov.played(tts.api.test("ну блять и ладно", login="potty", voice="alpha"))
    assert texts(tts.cosy) == ["ну", "и ладно"]
    assert "potty [alpha]: ну *** и ладно" in [j["text"] for j in tts.api.state()["engine"]["journal"]]
    assert band_share(it.audio, 800) > 0.01
    assert band_share(it.audio, VOICES["alpha"][0]) > 0.5


def test_no_beep_without_profanity(tts):
    ov = tts.overlay()
    it = ov.played(tts.api.test("ну и ладно, всё хорошо", voice="alpha"))
    assert band_share(it.audio, 800) < 0.001


def test_profanity_filter_off(tts):
    tts.api.ok("/api/settings", dict(filter_profanity=False))
    ov = tts.overlay()
    ov.played(tts.api.test("ну блять и ладно", voice="alpha"))
    assert texts(tts.cosy) == ["ну блять и ладно"]


def test_read_nick(tts):
    tts.api.ok("/api/settings", dict(read_nick=True))
    ov = tts.overlay()
    ov.played(tts.api.test("всем привет", login="Vasya", voice="alpha"))
    assert texts(tts.cosy) == ["Vasya говорит: всем привет"]


def test_max_chars(tts):
    tts.api.ok("/api/settings", dict(max_chars=10))
    ov = tts.overlay()
    ov.played(tts.api.test("раз два три четыре пять", voice="alpha"))
    assert texts(tts.cosy) == ["раз два тр"]


# --- синтез ---
def test_streaming_long_phrase(tts):
    """Длинная фраза идёт стримингом, chunk_tokens передаётся в запросе; звук приходит целиком."""
    ov = tts.overlay()
    it = ov.played(tts.api.test(LONG, voice="bravo"))
    (req,) = tts.cosy.tts
    assert req["stream"] is True and req["chunk_tokens"] == 150
    assert abs(it.seconds - len(LONG) / 12) < 0.5
    assert abs(dominant_hz(it.audio) - VOICES["bravo"][0]) < 3


def test_chunk_tokens_every_request(tts):
    """Сервер помнит chunk_tokens прошлого запроса, поэтому stream-tts передаёт его каждый раз."""
    tts.api.ok("/api/settings", dict(chunk_tokens=50))
    ov = tts.overlay()
    text = "Средняя фраза длиннее полутора кусков по пятьдесят токенов."
    for i in range(2):
        ov.played(tts.api.test(text, login=f"chunk{i}", voice="alpha"))
    assert [(r["stream"], r["chunk_tokens"]) for r in tts.cosy.tts] == [(True, 50), (True, 50)]


def test_streaming_off(tts):
    tts.api.ok("/api/settings", dict(streaming=False))
    ov = tts.overlay()
    ov.played(tts.api.test(LONG, voice="alpha"))
    assert tts.cosy.tts[0]["stream"] is False


@pytest.mark.parametrize("text", ["раз два три четыре пять", LONG], ids=["whole", "stream"])
def test_hard_cut(tts, text):
    """Звук длиннее 1,8× расчётного обрезается; стриминг при обрезке останавливает синтез."""
    tts.cosy.length_factor = 4
    ov = tts.overlay()
    it = ov.played(tts.api.test(text, voice="alpha"))
    limit = max(2.0, 1.8 * len(text) / 12)
    assert limit - 0.6 < it.seconds <= limit + 0.01
    if text is LONG:
        assert tts.cosy.stops >= 1


def test_synth_error_refunds_and_queue_goes_on(tts):
    tts.cosy.fail = "сломай"
    ov = tts.overlay()
    bad = tts.api.test("сломай синтез", login="err1", voice="alpha")
    good = tts.api.test("а эта фраза прозвучит", login="err2", voice="bravo")
    refund(tts, bad, "ошибка синтеза")
    ov.played(good)
    assert bad not in [it.id for it in ov.items]


# --- управление очередью ---
def test_skip_during_synth(tts):
    """Пропуск во время синтеза: /tts/stop серверу, возврат, фраза не звучит."""
    tts.cosy.hold = True
    ov = tts.overlay()
    rid = tts.api.test("долгий синтез, который пропустят", voice="alpha")
    wait_for(lambda: tts.cosy.active == 1, 5, "синтез начат")
    tts.api.ok("/api/skip")
    refund(tts, rid, "!ttsskip")
    assert tts.cosy.stops >= 1
    wait_for(lambda: tts.cosy.tts[0]["done"], 5, "синтез остановлен")
    assert tts.cosy.tts[0]["stopped"]
    assert not ov.items


def test_skip_during_playback(tts):
    """Пропуск во время воспроизведения: оверлей получает stop."""
    ov = tts.overlay()
    ov.auto_end = False   # «играет», пока не скажем ended
    rid = tts.api.test("эту фразу прервут на середине", voice="alpha")
    ov.played(rid)
    tts.api.ok("/api/skip")
    refund(tts, rid, "!ttsskip")
    wait_for(lambda: ov.items[0].stopped, 5, "stop в оверлее")


def test_remove_from_queue(tts):
    ids = [tts.api.test(f"фраза {i} для удаления", login=f"rm{i}", voice="alpha") for i in range(2)]
    tts.api.ok("/api/remove", dict(id=ids[1]))
    refund(tts, ids[1], "!ttsskip")
    assert [q["id"] for q in tts.api.state()["engine"]["queue"]] == ids[:1]
    assert tts.api.post("/api/remove", dict(id="nope")).status == 400


def test_clear(tts):
    ids = [tts.api.test(f"фраза {i} в очереди", login=f"clr{i}", voice="alpha") for i in range(3)]
    tts.api.ok("/api/clear")
    for rid in ids:
        refund(tts, rid, "!ttsclear")
    assert not tts.api.state()["engine"]["queue"]


def test_disable_clears_queue(tts):
    ids = [tts.api.test(f"фраза {i} до выключения", login=f"off{i}", voice="alpha") for i in range(2)]
    tts.api.ok("/api/enabled", dict(on=False))
    for rid in ids:
        refund(tts, rid, "озвучка выключена")


# --- оверлей ---
def test_last_overlay_plays(tts):
    """Играет последний подключившийся оверлей, прежний получает inactive; ушёл новый — прежний снова активен."""
    first = tts.overlay()
    second = tts.overlay(ready=False)
    wait_for(lambda: first.active is False, 5, "inactive первому оверлею")
    rid = tts.api.test("кто меня сыграет", voice="alpha")
    time.sleep(0.5)
    assert not first.items and not second.items   # активный оверлей ещё не готов
    second.ready()
    second.played(rid)
    assert not first.items
    second.close()
    wait_for(lambda: first.active is True, 5, "active первому оверлею")


def test_overlay_ended_only_current(tts):
    """ended с чужим id фразу не завершает: оверлей открыт без пароля, а раньше любой id пропускал фразу и копился."""
    ov = tts.overlay()
    ov.auto_end = False
    rid = tts.api.test("фраза до настоящего конца", voice="alpha")
    ov.played(rid)
    ov.ended("test-bogus")
    time.sleep(0.5)
    assert [q["id"] for q in tts.api.state()["engine"]["queue"]] == [rid]
    ov.ended(rid)
    wait_for(lambda: tts.event(rid)["outcome"] == "played", 5, "played после настоящего ended")


def test_overlay_lost_mid_phrase(tts):
    """Оверлей отключился, не доиграв: фраза остаётся в очереди и звучит в следующем."""
    ov = tts.overlay()
    ov.auto_end = False
    rid = tts.api.test("фраза, которую не доиграли", voice="alpha")
    ov.played(rid)
    ov.close()
    time.sleep(0.5)
    assert [q["id"] for q in tts.api.state()["engine"]["queue"]] == [rid]
    again = tts.overlay()
    again.played(rid)
    assert tts.event(rid)["outcome"] == "played"
    assert len(tts.cosy.tts) == 1   # второй раз — тот же звук, без нового синтеза

