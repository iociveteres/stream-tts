"""Статистика (/api/stats, stats.py) и история в SQLite (store.py): подсчёты, разбивка по часам, удаление старых
записей при запуске, миграция базы и настроек со старых версий.

Каждый тест — свой процесс (spawn) с чистой базой, чтобы считать точно. Записи прошлых часов и дней тест кладёт в
SQLite сам: stream-tts открывает её в режиме WAL, так что запись снаружи ему не мешает."""
import json, os, sqlite3, time

import pytest

from harness import Api, Overlay, wait_for


def stats(api, **q):
    r = api.get("/api/stats?" + "&".join(f"{k}={v}" for k, v in q.items()))
    assert r.status == 200, r.text
    return r.json()


def insert(server, *rows):
    """Строки events: (ts, login, outcome, reason, voice, test, source)."""
    for ts, login, outcome, reason, voice, test, source in rows:
        server.sql("INSERT INTO events(redemption, ts, user_id, login, voice, text_len, outcome, reason, wait_ms, "
                   "synth_ms, audio_ms, test, chat, source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   f"r{ts}-{login}", ts, login, login, voice, 10, outcome, reason, 100, 500, 1000, test,
                   int(source in ("chat", "command")), source)


def test_counts_after_activations(spawn, loop):
    """Сыгранные и возвращённые тестовые активации: total, played, refunded, топ голосов и зрителей, причины
    отказов, способы, ожидание и скорость синтеза. Без test=1 тестовые не считаются."""
    server, _ = spawn()
    api = Api(server.base)
    ov = Overlay(loop, server.base).connect()
    try:
        played = [api.test("первая фраза зрителя", login="usera", voice="alpha"),
                  api.test("вторая фраза зрителя", login="usera", voice="alpha"),
                  api.test("фраза другого зрителя", login="userb", voice="bravo")]
        for rid in played:
            ov.played(rid)
        api.ok("/api/ban", dict(login="userc"))
        refunds = [api.test("забаненный зритель", login="userc"), api.test("https://example.com", login="userd")]
        for rid in played + refunds:
            server.event(rid)
    finally:
        ov.close()
    s = stats(api, test=1)
    assert s["hours"] == 24
    assert s["total"] == dict(n=5, played=3, refunded=2)
    assert s["voices"] == [dict(voice="alpha", n=2), dict(voice="bravo", n=1)]
    assert s["viewers"][0] == dict(login="usera", n=2)
    assert sorted((v["login"], v["n"]) for v in s["viewers"][1:]) == [("userb", 1), ("userc", 1), ("userd", 1)]
    assert sorted((r["reason"], r["n"]) for r in s["reasons"]) == [("бан", 1), ("пустой текст", 1)]
    assert s["sources"] == [dict(source="test", n=3)]
    assert s["wait_ms"] > 0 and s["avg_audio_ms"] > 1000
    # синтез поддельного сервера — доли секунды на секунды звука
    assert 0 < s["synth_ratio"] < 1
    assert [sum(h["played"] for h in s["by_hour"]), sum(h["refunded"] for h in s["by_hour"])] == [3, 2]
    plain = stats(api)
    assert plain["total"]["n"] == 0 and plain["voices"] == [] and plain["synth_ratio"] is None


def test_by_hour_and_hours_limit(spawn):
    """by_hour — по часам (начало часа в секундах), окно — hours часов, hours ограничен 1..720."""
    server, _ = spawn()
    api = Api(server.base)
    now = time.time()
    rows = [(now - 600, "a", "played", None, "alpha", 0, "reward"),
            (now - 700, "b", "refund", "бан", None, 0, "reward"),
            (now - 70 * 60, "a", "played", None, "bravo", 0, "chat"),
            (now - 130 * 60, "c", "played", None, "alpha", 0, "command"),
            (now - 5 * 3600, "d", "played", None, "alpha", 0, "reward"),
            (now - 40 * 86400 + 10, "e", "played", None, "alpha", 0, "reward")]
    insert(server, *rows)
    s = stats(api, hours=3)
    want = {}
    for ts, _, outcome, *_ in rows[:4]:
        h = want.setdefault(int(ts // 3600) * 3600, dict(played=0, refunded=0))
        h["played" if outcome == "played" else "refunded"] += 1
    assert {h["hour"]: dict(played=h["played"], refunded=h["refunded"]) for h in s["by_hour"]} == want
    assert [h["hour"] for h in s["by_hour"]] == sorted(want)
    assert s["total"] == dict(n=4, played=3, refunded=1)
    assert sorted((x["source"], x["n"]) for x in s["sources"]) == [("chat", 1), ("command", 1), ("reward", 1)]
    assert stats(api, hours=6)["total"]["n"] == 5
    for asked, got in ((0, 1), (-5, 1), (1, 1), (720, 720), (100000, 720)):
        s = stats(api, hours=asked)
        assert s["hours"] == got and abs(s["since"] - (time.time() - got * 3600)) < 60, asked
    assert stats(api, hours=0)["total"]["n"] == 2
    # 40 дней назад: за пределом 720 часов
    assert stats(api, hours=720)["total"]["n"] == 5


def test_old_events_purged_on_start(spawn):
    """При запуске записи старше 30 дней удаляются (housekeeping), остальные остаются."""
    server, _ = spawn()
    server.stop()
    now = time.time()
    insert(server, (now - 31 * 86400, "old1", "played", None, "alpha", 0, "reward"),
           (now - 45 * 86400, "old2", "refund", "бан", None, 0, "reward"),
           (now - 29 * 86400, "recent", "played", None, "alpha", 0, "reward"))
    assert len(server.sql("SELECT * FROM events")) == 3
    again, _ = spawn(workdir=server.dir)
    wait_for(lambda: [r["login"] for r in again.sql("SELECT login FROM events")] == ["recent"], 10,
             "удаление старых записей")


OLD_SCHEMA = {
    # первая версия: без chat и source
    "v1": """CREATE TABLE users (user_id TEXT PRIMARY KEY, login TEXT, voice TEXT, badges TEXT, seen REAL);
CREATE TABLE bans (login TEXT PRIMARY KEY, ts REAL);
CREATE TABLE events (id INTEGER PRIMARY KEY, redemption TEXT, ts REAL, user_id TEXT, login TEXT, voice TEXT,
    text_len INTEGER, outcome TEXT, reason TEXT, wait_ms INTEGER, synth_ms INTEGER, audio_ms INTEGER,
    test INTEGER DEFAULT 0);""",
    # режим чата: chat есть, source ещё нет
    "v2": """CREATE TABLE users (user_id TEXT PRIMARY KEY, login TEXT, voice TEXT, badges TEXT, seen REAL);
CREATE TABLE bans (login TEXT PRIMARY KEY, ts REAL);
CREATE TABLE events (id INTEGER PRIMARY KEY, redemption TEXT, ts REAL, user_id TEXT, login TEXT, voice TEXT,
    text_len INTEGER, outcome TEXT, reason TEXT, wait_ms INTEGER, synth_ms INTEGER, audio_ms INTEGER,
    test INTEGER DEFAULT 0, chat INTEGER DEFAULT 0);""",
}


@pytest.mark.parametrize("version", sorted(OLD_SCHEMA))
def test_migration_from_old_version(spawn, loop, version):
    """База без колонок chat и source и settings.json с access и chat_all: колонки добавляются, способ старых
    записей восстанавливается по test и chat, общий уровень доступа и «все сообщения чата» переходят в sources."""
    server, _ = spawn(start=False)
    os.makedirs(server.data)
    now = time.time()
    con = sqlite3.connect(server.db_path)
    con.executescript(OLD_SCHEMA[version])
    con.execute("INSERT INTO users VALUES('u1', 'olduser', 'bravo', NULL, ?)", (now,))
    con.execute("INSERT INTO bans VALUES('oldtroll', ?)", (now,))
    cols = "redemption, ts, user_id, login, voice, text_len, outcome, reason, wait_ms, synth_ms, audio_ms, test"
    con.execute(f"INSERT INTO events({cols}) VALUES('r1', ?, 'u1', 'olduser', 'bravo', 5, 'played', NULL, 10, 100, "
                f"1000, 0)", (now - 60,))
    con.execute(f"INSERT INTO events({cols}) VALUES('r2', ?, 'u2', 'tester', 'alpha', 5, 'played', NULL, 10, 100, "
                f"1000, 1)", (now - 50,))
    if version == "v2":
        con.execute(f"INSERT INTO events({cols}, chat) VALUES('r3', ?, 'u3', 'chatter', 'alpha', 5, 'played', NULL, "
                    f"10, 100, 1000, 0, 1)", (now - 40,))
    con.commit()
    con.close()
    with open(os.path.join(server.data, "settings.json"), "w", encoding="utf-8") as f:
        json.dump(dict(access="subs", chat_all=True, queue_limit=5, enabled=True), f)

    server.start()
    api = Api(server.base)
    assert {"chat", "source"} <= {r["name"] for r in server.sql("PRAGMA table_info(events)")}
    st = api.state()
    src = st["settings"]["sources"]
    assert src["reward"] == dict(on=True, access="subs", allow=[])
    assert src["chat"] == dict(on=True, access="subs", allow=[])
    assert src["command"]["name"] == "!tts" and src["command"]["on"] is False
    assert st["settings"]["queue_limit"] == 5 and st["bans"] == ["oldtroll"]
    want = [("reward", 1), ("test", 1)] + ([("chat", 1)] if version == "v2" else [])
    assert sorted((x["source"], x["n"]) for x in stats(api, test=1)["sources"]) == sorted(want)
    # выбор голоса зрителя сохранился, новая запись идёт уже с source
    ov = Overlay(loop, server.base).connect()
    try:
        rid = api.test("после обновления", login="newbie", voice="alpha")
        ov.played(rid)
        assert server.event(rid)["source"] == "test"
    finally:
        ov.close()
    assert server.sql("SELECT voice FROM users WHERE user_id='u1'") == [dict(voice="bravo")]
    # старые ключи уходят из settings.json при первом сохранении
    api.ok("/api/settings", dict(queue_limit=6))
    with open(os.path.join(server.data, "settings.json"), encoding="utf-8") as f:
        saved = json.load(f)
    assert "access" not in saved and "chat_all" not in saved and saved["sources"]["chat"]["on"] is True
