"""Twitch-приложение stream-tts на поддельном Twitch (fake_twitch.py, фикстура twitch).

Вход по Device Code Flow и выход, неверный Client ID, вход из сохранённого токена после перезапуска с возвратом
зависших погашений; награда из админки (создание, правка, удаление, ошибки Twitch, пауза по состоянию озвучки);
погашение → фраза в очереди → FULFILLED или CANCELED с ответом зрителю; команда и сообщения чата через EventSub;
переподключение EventSub (session_reconnect, обрыв с догоном пропущенных погашений, тишина без keepalive); истёкший
токен (обновление по refresh-токену, вход истёк, доступ отозван).

Уведомления одного соединения EventSub stream-tts разбирает по очереди: то, что должно быть проигнорировано,
проверяется после следующего уведомления, которое дошло (барьер), без пауз наугад."""
import json, os

from fake_twitch import CHAT, REDEMPTION
from harness import wait_for


def queue(t):
    return [q["id"] for q in t.api.state()["engine"]["queue"]]


def journal(t):
    return [j["text"] for j in t.api.state()["engine"]["journal"]]


def status(t, rid, want, timeout=10):
    wait_for(lambda: t.fake.status(rid) == want, timeout, f"статус {want} у погашения {rid}")


def token(t):
    with open(os.path.join(t.ctx.server.data, "token.json"), encoding="utf-8") as f:
        return json.load(f)


def barrier(t, ov):
    """Погашение, которое дойдёт до оверлея: всё, что Twitch прислал раньше, уже разобрано."""
    rid = t.redeem("barrier", "барьер")
    ov.played(rid)


# --- вход ---
def test_login_logout(twitch):
    """Выход отзывает токен и стирает его; вход — код для twitch.tv/activate, после ввода — канал и EventSub."""
    s = twitch.state()
    assert s["login"] == "testchan" and not s["error"]
    assert set(s["scopes"]) == {"channel:manage:redemptions", "user:read:chat", "user:write:chat"}
    old = token(twitch)
    assert old["client_id"] == "test-client" and old["user_id"] == "1000"

    twitch.api.ok("/api/twitch/logout")
    assert twitch.state()["state"] == "logged_out"
    assert twitch.fake.revoked == [old["access_token"]]
    assert not os.path.exists(os.path.join(twitch.ctx.server.data, "token.json"))
    wait_for(lambda: not twitch.fake.sessions, 5, "закрытия EventSub")

    twitch.api.ok("/api/twitch/login")
    s = twitch.state()
    assert s["state"] == "device" and s["device"]["code"].startswith("ABCD")
    assert s["device"]["uri"] == "https://www.twitch.tv/activate"
    wait_for(lambda: twitch.fake.polls >= 2, 5, "опроса /token, пока код не введён")
    assert twitch.state()["state"] == "device"
    twitch.fake.authorize()
    twitch.connected()
    assert token(twitch)["access_token"] != old["access_token"]


def test_bad_client_id(twitch):
    """Чужой Client ID Twitch не принимает: сохранённый не меняется, вход нужен заново."""
    r = twitch.api.post("/api/twitch/client_id", dict(client_id="wrong"))
    assert r.status == 400 and "Twitch не принял Client ID" in r.json()["error"]
    s = twitch.state()
    assert s["client_id"] == "test-client" and s["state"] == "logged_out" and s["error"]


def test_restart_keeps_login_and_refunds_leftovers(twitch):
    """После перезапуска вход — из сохранённого токена, а погашения, оставшиеся без итога, возвращаются (Helix —
    страницами по 50, статусы — пачками по 50)."""
    reward = twitch.reward()
    server = twitch.ctx.server
    server.stop()   # как падение: без паузы награды
    wait_for(lambda: not twitch.fake.sessions, 5, "обрыва EventSub")
    left = [twitch.redeem(f"viewer{i}", "фраза", deliver=False) for i in range(51)]
    server.start()
    twitch.connected()
    assert not twitch.fake.devices, "вход без кода: токен сохранён"
    for rid in left:
        status(twitch, rid, "CANCELED")
    assert [len(ids) for st, ids in twitch.fake.statuses] == [50, 1]
    rows = server.sql("SELECT outcome, reason FROM events WHERE redemption IN (%s)" % ",".join("?" * 51), *left)
    assert len(rows) == 51 and {(r["outcome"], r["reason"]) for r in rows} == {("refund", "остались после перезапуска")}
    assert any("возвращены баллы за 51" in j for j in journal(twitch))
    assert twitch.state()["reward"]["id"] == reward["id"]
    twitch.ctx.idle(30)


# --- награда ---
def test_reward_create_edit_delete(twitch):
    reward = twitch.reward(title="Озвучка голосом", cost=300, cooldown=30, prompt="Напишите текст")
    assert (reward["title"], reward["cost"], reward["prompt"]) == ("Озвучка голосом", 300, "Напишите текст")
    assert reward["is_user_input_required"] and not reward["is_paused"]
    assert reward["is_global_cooldown_enabled"] and reward["global_cooldown_seconds"] == 30
    s = twitch.state()["reward"]
    assert s["exists"] and s["id"] == reward["id"] and not s["paused"]
    sub = next(x for x in twitch.fake.subs if x["type"] == REDEMPTION)
    assert sub["condition"] == dict(broadcaster_user_id="1000", reward_id=reward["id"])

    edited = twitch.reward(title="Т" * 60, cost=0, cooldown=0)
    assert list(twitch.fake.rewards) == [reward["id"]]
    assert edited["title"] == "Т" * 45 and edited["cost"] == 1 and not edited["is_global_cooldown_enabled"]
    assert ("PATCH", "/helix/channel_points/custom_rewards") in twitch.fake.calls

    twitch.api.ok("/api/reward/delete")
    assert not twitch.fake.rewards
    s = twitch.state()["reward"]
    assert not s["exists"] and not s["id"]


def test_reward_errors(twitch):
    """Ошибки Twitch при создании награды — с подсказкой."""
    twitch.fake.affiliate = False
    r = twitch.api.post("/api/reward", dict(title="Озвучка", cost=100))
    assert r.status == 400 and "только у компаньонов и партнёров" in r.json()["error"]

    twitch.fake.affiliate = True
    twitch.fake.rewards["other"] = dict(id="other", title="Чужая", cost=5, prompt="", is_paused=False)
    r = twitch.api.post("/api/reward", dict(title="Чужая", cost=100))
    assert r.status == 400 and "Награда с таким названием уже есть" in r.json()["error"]
    assert not twitch.state()["reward"]["id"]


def test_reward_paused(twitch):
    """Награда на паузе, пока озвучка выключена или способ «награда» отключён."""
    reward = twitch.reward()
    twitch.api.ok("/api/enabled", dict(on=False))
    assert reward["is_paused"] and twitch.state()["reward"]["paused"]
    twitch.api.ok("/api/enabled", dict(on=True))
    assert not reward["is_paused"]
    twitch.api.ok("/api/source", dict(source="reward", on=False))
    assert reward["is_paused"]
    twitch.api.ok("/api/source", dict(source="reward", on=True))
    assert not reward["is_paused"]


# --- погашения ---
def test_redemption_played(twitch):
    twitch.reward()
    ov = twitch.ctx.overlay()
    rid = twitch.redeem("Viewer", "привет с наградой")
    ov.played(rid)
    status(twitch, rid, "FULFILLED")
    assert [r["text"] for r in twitch.ctx.cosy.tts] == ["привет с наградой"]
    e = twitch.ctx.event(rid)
    assert (e["outcome"], e["login"], e["source"]) == ("played", "viewer", "reward")


def test_redemption_refunded_with_reply(twitch):
    """Пустой после фильтров текст — возврат баллов и ответ зрителю в чат; ответ, не пропущенный Twitch, —
    ошибкой у настройки."""
    twitch.reward()
    rid = twitch.redeem("Viewer", "https://example.com")
    status(twitch, rid, "CANCELED")
    e = twitch.ctx.event(rid)
    assert (e["outcome"], e["reason"]) == ("refund", "пустой текст")
    wait_for(lambda: twitch.fake.chat, 5, "ответа в чат")
    assert twitch.fake.chat == [dict(broadcaster_id="1000", sender_id="1000",
                                     message="@Viewer после фильтров озвучивать нечего, баллы вернутся")]
    assert not twitch.state()["reply_error"]

    twitch.fake.drop_reason = "AutoMod"
    rid = twitch.redeem("Other", "")
    status(twitch, rid, "CANCELED")
    wait_for(lambda: "AutoMod" in twitch.state()["reply_error"], 5, "ошибки ответа в чат")


def test_redemption_skipped(twitch):
    """Пропуск фразы на синтезе — CANCELED."""
    twitch.reward()
    twitch.ctx.cosy.hold = True
    rid = twitch.redeem("Viewer", "длинная фраза")
    wait_for(lambda: twitch.ctx.cosy.active, 5, "синтеза")
    twitch.api.ok("/api/skip")
    twitch.ctx.cosy.hold = False
    status(twitch, rid, "CANCELED")
    assert (twitch.ctx.event(rid)["reason"]) == "!ttsskip"


def test_duplicate_message(twitch):
    """Повтор уведомления (тот же message_id) не обрабатывается."""
    twitch.reward()
    ov = twitch.ctx.overlay()
    first = twitch.redeem("Viewer", "первая", message_id="dup")
    again = twitch.redeem("Viewer", "повтор", message_id="dup")
    barrier(twitch, ov)
    assert ov.played(first)
    assert twitch.fake.status(again) == "UNFULFILLED"
    assert [r["text"] for r in twitch.ctx.cosy.tts] == ["первая", "барьер"]


# --- чат через EventSub ---
def test_chat_command(twitch):
    """Команда озвучки из EventSub; отказ по уровню доступа — ответом на сообщение с командой."""
    twitch.api.ok("/api/source", dict(source="command", on=True, access="subs", user_cd=0, global_cd=0))
    ov = twitch.ctx.overlay()
    mid = twitch.say("Sub", "!tts привет из чата", badges=("subscriber",))
    ov.played(f"command-{mid}")

    mid = twitch.say("Viewer", "!tts а я без подписки")
    wait_for(lambda: twitch.fake.chat, 5, "ответа в чат")
    assert twitch.fake.chat == [dict(broadcaster_id="1000", sender_id="1000", reply_parent_message_id=mid,
                                     message="озвучка доступна только сабам, VIP и модерам")]
    assert [r["text"] for r in twitch.ctx.cosy.tts] == ["привет из чата"]
    assert not twitch.fake.statuses, "у команды нет погашений"


def test_chat_all_messages(twitch):
    """«Все сообщения чата»: сообщение, отправленное вместе с нашей наградой, читается только как погашение."""
    reward = twitch.reward()
    twitch.api.ok("/api/source", dict(source="chat", on=True, access="all"))
    ov = twitch.ctx.overlay()
    twitch.say("Viewer", "текст награды", reward_id=reward["id"])
    mid = twitch.say("Viewer", "просто сообщение")
    ov.played(f"chat-{mid}")
    assert [r["text"] for r in twitch.ctx.cosy.tts] == ["просто сообщение"]


def test_chat_mod_command(twitch):
    """Служебные команды модера приходят тем же путём: !ttsoff выключает озвучку и ставит награду на паузу."""
    reward = twitch.reward()
    twitch.say("Mod", "!ttsoff", badges=("moderator",))
    wait_for(lambda: reward["is_paused"], 5, "паузы награды")
    assert not twitch.api.state()["engine"]["enabled"]
    twitch.say("TestChan", "!ttson")   # стример — без бейджа модера, по ID канала
    wait_for(lambda: not reward["is_paused"], 5, "снятия паузы")


# --- переподключение EventSub ---
def test_session_reconnect(twitch):
    """session_reconnect: новое соединение до закрытия старого, подписки переехали (409 — не ошибка)."""
    twitch.reward()
    old = set(twitch.fake.sessions)
    twitch.run(twitch.fake.session_reconnect())
    wait_for(lambda: len(twitch.fake.sessions) == 1 and set(twitch.fake.sessions) != old, 5, "новой сессии EventSub")
    assert twitch.fake.connections == 2
    assert {s["type"] for s in twitch.fake.subs} == {CHAT, REDEMPTION}
    ov = twitch.ctx.overlay()
    rid = twitch.redeem("Viewer", "после переноса")
    ov.played(rid)
    s = twitch.state()
    assert s["ws"] == "connected" and not s["error"]


def test_drop_catches_up(twitch):
    """Обрыв EventSub: переподключение, подписки заново, погашения за время обрыва догоняются через Helix."""
    twitch.reward()
    ov = twitch.ctx.overlay()
    twitch.run(twitch.fake.drop())
    rid = twitch.redeem("Viewer", "пока не было связи", deliver=False)
    twitch.connected()
    ov.played(rid)
    status(twitch, rid, "FULFILLED")
    assert twitch.fake.connections == 2


def test_keepalive_timeout(twitch):
    """Тишина дольше keepalive_timeout + 5 с — соединение считается мёртвым и открывается заново."""
    twitch.fake.keepalive = 1
    twitch.run(twitch.fake.drop())
    wait_for(lambda: twitch.fake.connections == 2 and twitch.fake.sessions, 5, "переподключения")
    twitch.connected()
    twitch.fake.mute = True
    wait_for(lambda: twitch.fake.connections == 3, 10, "переподключения после тишины")
    twitch.fake.mute = False
    twitch.connected()


# --- токен ---
def test_token_refresh(twitch):
    """Access-токен истёк: Helix отвечает 401, stream-tts обновляет токен по refresh-токену и повторяет запрос."""
    reward = twitch.reward()
    old = token(twitch)
    twitch.fake.expire_tokens()
    twitch.api.ok("/api/enabled", dict(on=False))
    assert reward["is_paused"]
    assert twitch.fake.refreshes == 1
    new = token(twitch)
    assert new["access_token"] != old["access_token"] and new["refresh_token"] != old["refresh_token"]
    assert new["login"] == "testchan"
    assert twitch.state()["state"] == "ok"


def test_token_expired(twitch):
    """Refresh-токен не принят — вход истёк: EventSub закрыт, награда недоступна до нового входа."""
    twitch.reward()
    twitch.fake.refresh_ok = False
    twitch.fake.expire_tokens()
    r = twitch.api.post("/api/reward", dict(title="Озвучка", cost=200))
    assert r.status == 400
    s = twitch.state()
    assert s["state"] == "expired" and "Вход в Twitch истёк" in s["error"]
    wait_for(lambda: not twitch.fake.sessions and twitch.state()["ws"] == "off", 5, "закрытия EventSub")

    twitch.fake.refresh_ok = True
    twitch.api.ok("/api/twitch/login")
    twitch.fake.authorize()
    twitch.connected()


def test_access_revoked(twitch):
    """Стример отозвал доступ приложения (revocation authorization_revoked) — вход истёк."""
    twitch.run(twitch.fake.revoke_access())
    wait_for(lambda: twitch.state()["state"] == "expired", 5, "истёкшего входа")
    wait_for(lambda: not twitch.fake.sessions, 5, "закрытия EventSub")
