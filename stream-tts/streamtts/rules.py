"""Правила приёма фразы: способы заказа и уровни доступа, кулдауны команды, причины отказа и возврата баллов (R_*),
ответ зрителю об отказе. Функции берут engine (Engine, engine.py): настройки, база, Twitch, кулдауны."""
import asyncio, math, time

from .i18n import tr

MODS = {"broadcaster", "moderator", "lead_moderator"}
ACCESS = {"all": None, "mods": MODS, "vip": MODS | {"vip"}, "subs": MODS | {"vip", "subscriber", "founder"}}

# Причины возврата баллов (в статистике и интерфейсе): в базе по-русски, перевод при показе
R_OFF, R_BAN, R_ACCESS, R_EMPTY, R_FULL = "озвучка выключена", "бан", "уровень доступа", "пустой текст", "очередь полна"
R_NOVOICE, R_SYNTH, R_SKIP, R_CLEAR, R_RESTART = ("нет голосов", "ошибка синтеза", "!ttsskip", "!ttsclear",
                                                  "остались после перезапуска")
R_COOLDOWN = "кулдаун"
R_REMOVED = "удалено модером"
# Ответ зрителю при отказе (настройка reply): бан не объясняется, ошибки синтеза и пропуски — не отказ при приёме
REPLIES = {R_OFF: "озвучка сейчас выключена", R_ACCESS: "озвучка доступна только {who}",
           R_EMPTY: "после фильтров озвучивать нечего", R_FULL: "очередь озвучки заполнена, попробуйте позже",
           R_NOVOICE: "нет доступных голосов", R_COOLDOWN: "озвучка снова будет доступна через {n} с"}
ACCESS_WHO = {"subs": "сабам, VIP и модерам", "vip": "VIP и модерам", "mods": "модерам"}
REPLY_EVERY = 30   # с: не чаще одного ответа зрителю, иначе спам командой во время кулдауна превращается в спам ответами


def src(engine, item):
    return engine.s["sources"].get(item.source, {})


def privileged(engine, item):
    """Стример, модеры и ники из списка способа: мимо уровня доступа и кулдаунов."""
    return bool(item.login in src(engine, item).get("allow", [])
                or (engine.twitch and item.user_id == engine.twitch.channel_id)
                or engine.db.badges(item.user_id) & MODS)


def access_ok(engine, item):
    need = ACCESS.get(src(engine, item).get("access", "all"))
    return need is None or privileged(engine, item) or bool(engine.db.badges(item.user_id) & need)


def cooldown_left(engine, item):
    """Секунд до конца кулдауна команды (на зрителя и общего); 0 — можно."""
    if item.source != "command" or privileged(engine, item):
        return 0
    c, now = src(engine, item), time.time()
    left = max(engine.cd_user.get(item.user_id, 0) + int(c.get("user_cd") or 0),
               engine.cd_global + int(c.get("global_cd") or 0)) - now
    return max(0, math.ceil(left))


def reply(engine, item, reason, wait=0):
    """Зрителю — почему не озвучено (настройка reply: chat — ответом в чате, off — молча)."""
    mode = engine.s["reply"]
    if mode == "off" or item.test or reason not in REPLIES or not engine.twitch or engine.twitch.state != "ok":
        return
    now = time.time()
    if now - engine.replied.get(item.user_id, 0) < REPLY_EVERY:
        return
    engine.replied[item.user_id] = now
    if item.source == "command" and reason == R_EMPTY and not item.raw.strip():
        text = tr("напишите текст после команды: {cmd} привет", cmd=src(engine, item)["name"])
    else:
        text = tr(REPLIES[reason], n=wait, who=tr(ACCESS_WHO.get(src(engine, item).get("access"), "")))
    if item.source == "reward":
        text += tr(", баллы вернутся")
    asyncio.create_task(engine.twitch.tell(item.display, text, item.msg_id))
