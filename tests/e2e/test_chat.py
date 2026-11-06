"""Режим «Только чтение чата»: чат канала через анонимный IRC (поддельный, fake_irc.py).

Подключение и «канал не найден», команда озвучки с уровнями доступа, кулдаунами и списком «Всегда доступно»,
«все сообщения чата», !ttssf, команды модера, удаление сообщений модером (CLEARMSG, CLEARCHAT), что из сообщения
читается, переподключение по RECONNECT и по тишине.

Сообщение, которое stream-tts должен проигнорировать, проверяется после chat.sync(): к этому моменту оно уже
обработано."""
import time

import pytest

from fake_cosyvoice import VOICES
from harness import dominant_hz, wait_for


def texts(tts):
    return [r["text"] for r in tts.cosy.tts]


def queue(tts):
    return [q["id"] for q in tts.api.state()["engine"]["queue"]]


def journal(tts):
    return [j["text"] for j in tts.api.state()["engine"]["journal"]]


def rejected(tts, login, reason):
    return f"{login}: сообщение чата не озвучено ({reason})" in journal(tts)


def removed(tts, rid, reason):
    e = tts.event(rid)
    assert (e["outcome"], e["reason"]) == ("refund", reason), e


def command(tts, **kw):
    """Способ «команда»: включён, по умолчанию всем и без кулдаунов."""
    tts.api.ok("/api/source", dict(dict(source="command", on=True, access="all", user_cd=0, global_cd=0), **kw))


def all_messages(tts, **kw):
    tts.api.ok("/api/source", dict(dict(source="chat", on=True, access="all"), **kw))


# --- подключение ---
def test_connected(chat, irc):
    s = chat.state()
    assert s["state"] == "ok" and s["channel"] == "testchan" and not s["error"]
    assert irc.joins[-1] == "testchan"


def test_channel_not_found(chat, tts, irc):
    """Нет ROOMSTATE после JOIN — «канал не найден», без повторов до смены канала."""
    tts.api.ok("/api/twitch/mode", dict(mode="chat", channel="nosuchchan"))
    wait_for(lambda: chat.state()["state"] == "not_found", 6, "канал не найден")
    assert "nosuchchan" in chat.state()["error"]
    time.sleep(1.5)
    assert irc.joins.count("nosuchchan") == 1
    tts.api.ok("/api/twitch/mode", dict(mode="chat", channel="testchan"))
    chat.connected()


def test_channel_input(chat, tts):
    """Канал можно вставить ссылкой; не похожее на ник отклоняется."""
    tts.api.ok("/api/twitch/mode", dict(mode="chat", channel="https://www.twitch.tv/TestChan/"))
    chat.connected()
    assert chat.state()["channel"] == "testchan"
    r = tts.api.post("/api/twitch/mode", dict(mode="chat", channel="не ник!"))
    assert r.status == 400 and not r.json()["ok"]


# --- команда озвучки ---
def test_command_plays(chat, tts):
    command(tts)
    ov = tts.overlay()
    mid = chat.say("cmd_viewer", "!tts привет из чата")
    ov.played(f"command-{mid}")
    assert texts(tts) == ["привет из чата"]
    e = tts.event(f"command-{mid}")
    assert e["outcome"] == "played" and e["source"] == "command" and e["login"] == "cmd_viewer"


def test_command_off_ignored(chat, tts):
    mid = chat.say("off_viewer", "!tts способ выключен")
    chat.sync()
    assert not queue(tts) and not texts(tts)
    assert not any("off_viewer" in j for j in journal(tts))
    assert mid


def test_command_access_by_badges(chat, tts):
    """«Сабы, VIP и модеры» — по бейджам сообщения; стример (user-id = room-id) проходит и без бейджа."""
    command(tts, access="subs")
    ov = tts.overlay()
    chat.say("acc_plain", "!tts без бейджа")
    chat.sync()
    assert rejected(tts, "acc_plain", "уровень доступа")
    for login, badges in (("acc_sub", ["subscriber"]), ("acc_vip", ["vip"]), ("acc_mod", ["moderator"]),
                          ("testchan", [])):
        ov.played(f"command-{chat.say(login, f'!tts пишет {login}', badges)}")
    command(tts, access="mods")
    chat.say("acc_vip", "!tts теперь только модерам", ["vip"])
    chat.sync()
    assert rejected(tts, "acc_vip", "уровень доступа")
    assert len(texts(tts)) == 4


def test_allow_list(chat, tts):
    """«Всегда доступно» — мимо уровня доступа и кулдауна."""
    command(tts, access="mods", user_cd=60, allow="@Friend, другой")
    assert tts.api.state()["settings"]["sources"]["command"]["allow"] == ["friend", "другой"]
    ov = tts.overlay()
    for i in range(2):
        ov.played(f"command-{chat.say('Friend', f'!tts друг пишет {i}')}")
    chat.say("stranger", "!tts а мне нельзя")
    chat.sync()
    assert rejected(tts, "stranger", "уровень доступа")


def test_user_cooldown(chat, tts):
    command(tts, user_cd=60)
    ov = tts.overlay()
    ov.played(f"command-{chat.say('cd_a', '!tts первая')}")
    chat.say("cd_a", "!tts вторая сразу")
    chat.sync()
    assert rejected(tts, "cd_a", "кулдаун")
    ov.played(f"command-{chat.say('cd_b', '!tts у другого зрителя свой кулдаун')}")
    for i in range(2):   # модеры — мимо кулдауна
        ov.played(f"command-{chat.say('cd_mod', f'!tts модер {i}', ['moderator'])}")


def test_global_cooldown(chat, tts):
    command(tts)
    ov = tts.overlay()
    ov.played(f"command-{chat.say('gcd_a', '!tts до общего кулдауна')}")
    command(tts, global_cd=60)   # кулдаун отсчитывается от последней принятой фразы
    chat.say("gcd_b", "!tts общий кулдаун")
    chat.sync()
    assert rejected(tts, "gcd_b", "кулдаун")


def test_command_empty(chat, tts):
    command(tts)
    chat.say("empty_cmd", "!tts")
    chat.sync()
    assert rejected(tts, "empty_cmd", "пустой текст")


def test_command_name(chat, tts):
    command(tts, name="Say")
    assert tts.api.state()["settings"]["sources"]["command"]["name"] == "!say"
    ov = tts.overlay()
    ov.played(f"command-{chat.say('name_viewer', '!say своя команда')}")
    chat.say("name_viewer", "!tts старая команда")
    chat.sync()
    assert texts(tts) == ["своя команда"]
    for bad in ("!ttsskip", "два слова"):
        r = tts.api.post("/api/source", dict(source="command", name=bad))
        assert r.status == 400, bad


# --- все сообщения чата ---
def test_all_messages(chat, tts):
    """Каждое сообщение, кроме команд (в том числе чужих ботов), идёт в очередь."""
    all_messages(tts)
    ov = tts.overlay()
    chat.say("all_viewer", "!другойбот привет")
    chat.say("all_viewer", "!tts команда выключена")
    ov.played(f"chat-{chat.say('all_viewer', 'просто сообщение в чат')}")
    assert texts(tts) == ["просто сообщение в чат"]
    assert tts.event(f"chat-{chat.say('all_viewer', 'ещё одно')}")["source"] == "chat"


def test_all_messages_access_silent(chat, tts):
    """Не прошедшее по доступу сообщение просто не читается, журнал не засоряется."""
    all_messages(tts, access="subs")
    chat.say("silent_viewer", "меня не прочтут")
    chat.sync()
    assert not queue(tts)
    assert not any("silent_viewer" in j for j in journal(tts))


@pytest.mark.parametrize("text,kw,spoken", [
    ("Kappa привет Kappa", dict(emotes="25:0-4,13-17"), "привет"),
    ("Cheer100 спасибо за стрим", dict(bits=100), "спасибо за стрим"),
    ("@Vasya согласен", dict(reply_to=("parent-1", "Vasya")), "согласен"),
    ("\x01ACTION машет рукой\x01", {}, "машет рукой"),
], ids=["emotes", "cheer", "reply", "me"])
def test_what_is_read(chat, tts, text, kw, spoken):
    """Эмоуты и cheer-эмоуты — имена картинок, @ник в начале ответа отбрасывается, /me — обычный текст."""
    all_messages(tts)
    ov = tts.overlay()
    ov.played(f"chat-{chat.say('frag_viewer', text, **kw)}")
    assert texts(tts) == [spoken]


# --- !ttssf ---
def test_ttssf(chat, tts):
    all_messages(tts)
    tts.api.ok("/api/voice", dict(name="charlie", enabled=False))
    ov = tts.overlay()
    chat.say("sf_viewer", "!ttssf BRAVO")
    chat.sync()
    assert "sf_viewer выбрал голос bravo" in journal(tts)
    for cmd in ("!ttssf nosuch", "!ttssf charlie"):   # неизвестный и выключенный — молча мимо
        chat.say("sf_viewer", cmd)
    it = ov.played(f"chat-{chat.say('sf_viewer', 'каким я голосом')}")
    assert abs(dominant_hz(it.audio) - VOICES["bravo"][0]) < 3
    assert tts.event(it.id)["voice"] == "bravo"


# --- команды модера ---
def test_mod_skip_and_clear(chat, tts):
    all_messages(tts)
    ids = [f"chat-{chat.say(f'mq{i}', f'фраза номер {i}')}" for i in range(3)]
    chat.say("mq_viewer", "!ttsskip")
    chat.say("mq_viewer", "!ttsclear")
    chat.sync()
    assert queue(tts) == ids   # зрителю нельзя
    chat.say("mq_mod", "!ttsskip", ["moderator"])
    removed(tts, ids[0], "!ttsskip")
    chat.say("mq_mod", "!ttsclear", ["moderator"])
    for rid in ids[1:]:
        removed(tts, rid, "!ttsclear")
    assert not queue(tts)


def test_mod_ban_unban(chat, tts):
    all_messages(tts)
    ids = [f"chat-{chat.say('Troll', f'спам {i}')}" for i in range(2)]
    chat.say("ban_viewer", "!ttsban troll")
    chat.sync()
    assert tts.api.state()["bans"] == []
    chat.say("ban_mod", "!ttsban @Troll", ["moderator"])
    for rid in ids:   # его фразы из очереди убраны
        removed(tts, rid, "бан")
    assert tts.api.state()["bans"] == ["troll"]
    chat.say("Troll", "а теперь?")
    chat.sync()
    assert not queue(tts)
    chat.say("ban_mod", "!ttsunban troll", ["moderator"])
    mid = chat.say("Troll", "разбанили")
    chat.sync()
    assert tts.api.state()["bans"] == [] and queue(tts) == [f"chat-{mid}"]


def test_mod_on_off(chat, tts):
    all_messages(tts)
    chat.say("onoff_mod", "!ttsoff", ["moderator"])
    chat.sync()
    assert tts.api.state()["engine"]["enabled"] is False
    chat.say("onoff_viewer", "озвучка выключена")
    chat.say("onoff_viewer", "!ttson")
    chat.sync()
    s = tts.api.state()["engine"]
    assert s["enabled"] is False and not s["queue"]
    chat.say("testchan", "!ttson")   # стример: user-id совпадает с room-id
    chat.sync()
    assert tts.api.state()["engine"]["enabled"] is True


# --- модерация чата ---
def test_clearmsg(chat, tts):
    """Модер удалил сообщение — фраза уходит из очереди."""
    all_messages(tts)
    m1, m2 = chat.say("cm_viewer", "удалят"), chat.say("cm_viewer", "останется")
    chat.sync()
    chat.clear_msg(m1, "cm_viewer")
    removed(tts, f"chat-{m1}", "удалено модером")
    assert queue(tts) == [f"chat-{m2}"]


def test_clearchat_user(chat, tts):
    """Таймаут или бан зрителя — все его фразы уходят."""
    all_messages(tts)
    a = [chat.say("cc_a", f"от зрителя a {i}") for i in range(2)]
    b = chat.say("cc_b", "от зрителя b")
    chat.sync()
    chat.clear_chat("cc_a")
    for mid in a:
        removed(tts, f"chat-{mid}", "удалено модером")
    assert queue(tts) == [f"chat-{b}"]


def test_clearchat_all(chat, tts):
    """Очистка чата убирает фразы из чата, тестовую активацию не трогает."""
    all_messages(tts)
    mids = [chat.say(f"ca{i}", f"сообщение {i}") for i in range(2)]
    rid = tts.api.test("тестовая фраза", voice="alpha")
    chat.sync()
    chat.clear_chat()
    for mid in mids:
        removed(tts, f"chat-{mid}", "удалено модером")
    assert queue(tts) == [rid]


# --- переподключение ---
def test_reconnect(chat, tts, irc):
    """RECONNECT: соединение заново через ~1 с, и каждый раз с той же паузой (после удачного подключения она
    сбрасывается)."""
    all_messages(tts)
    ov = tts.overlay()
    for _ in range(2):
        n, t0 = irc.connections, time.monotonic()
        chat.reconnect()
        wait_for(lambda: irc.connections == n + 1, 5, "переподключение")
        chat.connected()
        assert time.monotonic() - t0 < 1.8
    ov.played(f"chat-{chat.say('rc_viewer', 'после переподключения')}")


def test_ping_when_idle(chat, irc):
    """После тишины stream-tts шлёт свой PING; ответили — соединение то же, не ответили — переподключение."""
    n = irc.connections
    wait_for(lambda: irc.client_pings >= 2, 5, "PING от stream-tts")
    assert irc.connections == n
    irc.ignore_ping = True
    wait_for(lambda: irc.connections == n + 1, 8, "переподключение без PONG")
    irc.ignore_ping = False
    chat.connected()
