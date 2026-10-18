"""Режим «Только чтение чата»: чат Twitch через анонимный IRC, без приложения Twitch и входа.

Для приложения на dev.twitch.tv нужна двухфакторная аутентификация, а читать чат IRC пускает анонимно (ник
justinfan…). В тегах сообщений есть всё, на что опирается engine.chat: ID и ник зрителя, бейджи, ID сообщения,
эмоуты, ответ на сообщение. Сообщение перекладывается в форму события EventSub channel.chat.message, поэтому
команды, способы «команда» и «все сообщения» работают как с входом. Писать в чат и управлять наградой нельзя.
Бонус IRC: CLEARMSG и CLEARCHAT (модер удалил сообщение, таймаут или бан) убирают фразы из очереди."""
import asyncio, contextlib, logging, random, re

import aiohttp

from . import paths
from .i18n import tr

log = logging.getLogger("chat")

# Переменные STREAM_TTS_IRC_* — для e2e-тестов (tests/e2e): поддельный IRC и короткие таймауты
IRC = paths.env("STREAM_TTS_IRC_URL", "wss://irc-ws.chat.twitch.tv:443")
# с: ROOMSTATE приходит сразу после JOIN; нет его — канала нет
JOIN_WAIT = float(paths.env("STREAM_TTS_IRC_JOIN_WAIT", "10"))
# с без данных: свой PING. Twitch пингует раз в ~5 минут, обрыв без FIN иначе заметен нескоро
IDLE = float(paths.env("STREAM_TTS_IRC_IDLE", "60"))
PONG_WAIT = float(paths.env("STREAM_TTS_IRC_PONG_WAIT", "15"))
# Логин Twitch: 4–25 символов, у старых аккаунтов бывает короче
CHANNEL_RE = re.compile(r"[a-z0-9_]{2,25}")
# Cheer-эмоут в сообщении с битами: Cheer100, BibleThump50 — имена картинок, не читаются (как cheermote в EventSub)
CHEER_RE = re.compile(r"(?<!\S)[A-Za-z]+\d+(?!\S)")
TAG_ESC = {"s": " ", ":": ";", "\\": "\\", "r": "\r", "n": "\n"}


def parse_channel(text):
    """Ник канала из ввода: «name», «#name», «twitch.tv/name», ссылка целиком. Пустая строка — не похоже на ник."""
    t = text.strip().lower().rstrip("/")
    t = re.sub(r"^(https?://)?(www\.|m\.)?twitch\.tv/", "", t).lstrip("#@")
    return t if CHANNEL_RE.fullmatch(t) else ""


def parse(line):
    """Строка IRC → (теги, ник отправителя, команда, параметры, текст)."""
    tags, nick = {}, ""
    if line.startswith("@"):
        raw, line = line[1:].split(" ", 1)
        for kv in raw.split(";"):
            k, _, v = kv.partition("=")
            tags[k] = re.sub(r"\\(.)?", lambda m: TAG_ESC.get(m[1] or "", m[1] or ""), v)
    if line.startswith(":"):
        prefix, line = line[1:].split(" ", 1)
        nick = prefix.split("!", 1)[0]
    line, _, text = line.partition(" :")
    parts = line.split()
    return tags, nick, (parts[0] if parts else ""), parts[1:], text


def fragments(text, tags):
    """Текст и эмоуты, как fragments в EventSub. Позиции в теге emotes — в символах Unicode, как индексы str."""
    spans = []
    for group in filter(None, tags.get("emotes", "").split("/")):
        for rng in group.partition(":")[2].split(","):
            a, _, b = rng.partition("-")
            if a.isdigit() and b.isdigit():
                spans.append((int(a), int(b) + 1))
    out, pos = [], 0
    for a, b in sorted(spans):
        if a >= pos:
            out += [dict(type="text", text=text[pos:a]), dict(type="emote", text=text[a:b])]
            pos = b
    out.append(dict(type="text", text=text[pos:]))
    if tags.get("bits"):
        for f in out:
            if f["type"] == "text":
                f["text"] = CHEER_RE.sub("", f["text"])
    parent = tags.get("reply-parent-user-login")
    if parent and out[0]["type"] == "text" and out[0]["text"].lower().startswith(f"@{parent} "):
        # ответ начинается с @ника того, кому отвечают: в EventSub это отдельный фрагмент-упоминание
        n = len(parent) + 1
        out[0:1] = [dict(type="mention", text=out[0]["text"][:n]), dict(type="text", text=out[0]["text"][n:])]
    return [f for f in out if f["text"]]


def event(tags, nick, text):
    """PRIVMSG → событие в форме EventSub channel.chat.message (engine.chat и chat_message)."""
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):   # /me
        text = text[8:-1]
    login = (tags.get("login") or nick).lower()
    return dict(
        message_id=tags.get("id", ""), chatter_user_id=tags.get("user-id", ""), chatter_user_login=login,
        chatter_user_name=tags.get("display-name") or login, broadcaster_user_id=tags.get("room-id", ""),
        badges=[dict(set_id=b.split("/")[0]) for b in filter(None, tags.get("badges", "").split(","))],
        message=dict(text=text, fragments=fragments(text, tags)),
        reply=dict(parent_message_id=tags["reply-parent-msg-id"]) if tags.get("reply-parent-msg-id") else None,
        channel_points_custom_reward_id=tags.get("custom-reward-id"))


class ChatReader:
    def __init__(self, engine):
        self.engine = engine
        self.channel = ""
        self.room_id = ""
        self.state = "off"           # off | connecting | ok | not_found
        self.error = ""
        self.task = None
        self.backoff = 1

    def public(self):
        return dict(state=self.state, channel=self.channel, error=self.error)

    async def start(self, http, channel):
        await self.stop()
        self.http, self.channel = http, channel
        if channel:
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            # ws.close() ждёт ответного кадра закрытия до 10 с: смена канала или режима столько не ждёт
            await asyncio.wait({self.task}, timeout=2)
        self.task, self.state, self.error, self.room_id = None, "off", "", ""

    async def _run(self):
        self.backoff = 1
        while True:
            self.state = "connecting"
            ws = None
            try:
                ws = await self.http.ws_connect(IRC, heartbeat=None)
                await ws.send_str("CAP REQ :twitch.tv/tags twitch.tv/commands")
                await ws.send_str("PASS SCHMOOPIIE")   # анонимный вход: пароль любой, ник justinfan<число>
                await ws.send_str(f"NICK justinfan{random.randint(10000, 999999)}")
                await ws.send_str(f"JOIN #{self.channel}")
                if not await self._listen(ws):
                    return
            except asyncio.CancelledError:
                if ws:
                    with contextlib.suppress(Exception):
                        await ws.close()
                raise
            except Exception as e:
                log.warning("чат #%s: %s", self.channel, e)
                self.error = tr("Чат недоступен: {e}", e=str(e) or type(e).__name__)
            if ws:
                with contextlib.suppress(Exception):
                    await ws.close()
            self.state = "connecting"
            await asyncio.sleep(self.backoff)
            self.backoff = min(self.backoff * 2, 60)

    async def _listen(self, ws):
        """Читает до обрыва. False — переподключаться не надо (канала нет)."""
        loop = asyncio.get_running_loop()
        joined_by = loop.time() + JOIN_WAIT
        pinged = False
        while True:
            timeout = max(0.1, joined_by - loop.time()) if self.state != "ok" else PONG_WAIT if pinged else IDLE
            try:
                msg = await ws.receive(timeout=timeout)
            except asyncio.TimeoutError:
                if self.state != "ok":
                    log.warning("чат #%s: канал не найден", self.channel)
                    self.state, self.error = "not_found", tr("Канал {ch} не найден", ch=self.channel)
                    return False
                if pinged:
                    raise ConnectionError("нет ответа на PING")
                await ws.send_str("PING :stream-tts")
                pinged = True
                continue
            if msg.type != aiohttp.WSMsgType.TEXT:
                raise ConnectionError(f"соединение закрыто ({msg.type.name}, {ws.close_code})")
            pinged = False
            for line in msg.data.split("\r\n"):
                if line and not await self._line(ws, line):
                    return False

    async def _line(self, ws, line):
        tags, nick, cmd, params, text = parse(line)
        if cmd == "PING":
            await ws.send_str(f"PONG :{text}")
        elif cmd == "RECONNECT":   # Twitch перезапускает сервер чата
            raise ConnectionError("RECONNECT")
        elif cmd == "ROOMSTATE" and tags.get("room-id"):
            if self.state != "ok":
                log.info("чат #%s: подключено", self.channel)
            # пауза переподключения — заново после удачного входа: иначе каждый RECONNECT Twitch удваивал её до 60 с
            self.state, self.error, self.room_id, self.backoff = "ok", "", tags["room-id"], 1
        elif cmd == "NOTICE" and tags.get("msg-id") in ("msg_channel_suspended", "msg_banned"):
            log.warning("чат #%s: %s", self.channel, text)
            self.state, self.error = "not_found", text or tr("Канал {ch} не найден", ch=self.channel)
            return False
        elif cmd == "PRIVMSG" and self.state == "ok":
            try:
                await self.engine.chat(event(tags, nick, text))
            except Exception:
                log.exception("сообщение чата")
        elif cmd == "CLEARMSG" and tags.get("target-msg-id"):
            await self.engine.removed(msg_id=tags["target-msg-id"], who=tags.get("login", ""))
        elif cmd == "CLEARCHAT":
            # с ником — таймаут или бан зрителя, без — модер очистил весь чат
            await self.engine.removed(user_id=tags.get("target-user-id") or None, who=text, all_chat=not text)
        return True
