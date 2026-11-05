"""Команды чата: служебные !tts* (голос, пропуск, очистка, бан, вкл/выкл) и команда озвучки. Событие — в форме
EventSub channel.chat.message (twitch/eventsub.py) или такое же из тегов IRC (chat_irc.py), приходит через Engine.chat."""
from .i18n import tr
from .rules import MODS, R_BAN

# Команды сервиса: имя команды озвучки не может с ними совпадать
SERVICE_COMMANDS = ("!ttssf", "!ttsskip", "!ttsclear", "!ttsban", "!ttsunban", "!ttson", "!ttsoff")


async def chat(engine, ev):
    """Сообщение чата (EventSub channel.chat.message): бейджи для уровня доступа и команды."""
    text = (ev.get("message") or {}).get("text", "").strip()
    uid, login = ev["chatter_user_id"], ev["chatter_user_login"].lower()
    badges = {b["set_id"] for b in ev.get("badges") or []}
    engine.db.touch_user(uid, login, badges)
    src = engine.s["sources"]
    if not text.startswith("!"):
        # Сообщение, отправленное вместе с нашей наградой, уже озвучивается как активация
        if src["chat"]["on"] and text and (not engine.twitch or
                                           ev.get("channel_points_custom_reward_id") != engine.s["reward"]["id"]):
            await engine.chat_message(ev)
        return
    parts = text.split()
    cmd, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
    if src["command"]["on"] and cmd == src["command"]["name"]:
        await engine.chat_message(ev, "command", cmd)
        return
    if not cmd.startswith("!tts"):
        return   # команды других ботов не читаются
    if cmd == "!ttssf":
        v = engine.voices.find(arg)
        if v:  # неизвестный или выключенный голос молча игнорируется
            engine.db.set_voice(uid, login, v)
            engine.note(tr("{who} выбрал голос {voice}", who=login, voice=v))
        return
    if not (badges & MODS or uid == ev.get("broadcaster_user_id")):
        return
    who = ev.get("chatter_user_name") or login
    if cmd == "!ttsskip":
        await engine.skip(who)
    elif cmd == "!ttsclear":
        await engine.clear(who)
    elif cmd in ("!ttsban", "!ttsunban") and arg:
        nick = arg.lstrip("@").lower()
        engine.db.ban(nick, cmd == "!ttsban")
        engine.note(tr("{by}: бан {who}" if cmd == "!ttsban" else "{by}: разбан {who}", by=who, who=nick))
        if cmd == "!ttsban":
            for item in [it for it in engine.queue if it.login == nick and it is not engine.current]:
                await engine.cancel(item, R_BAN)
    elif cmd == "!ttson":
        await engine.set_enabled(True, who)
    elif cmd == "!ttsoff":
        await engine.set_enabled(False, who)
