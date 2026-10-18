"""Поддельный IRC Twitch (wss://irc-ws.chat.twitch.tv) для e2e-тестов режима «Только чтение чата».

Отвечает на то, что шлёт chat_irc.py: CAP REQ, PASS/NICK анонимного justinfan, JOIN (ROOMSTATE только для канала из
CHANNELS: иначе stream-tts решает, что канала нет), PING (PONG, если не ignore_ping). Тест шлёт от имени Twitch
PRIVMSG с тегами (say), CLEARMSG, CLEARCHAT и RECONNECT.

sync() — барьер: PING с меткой и ожидание PONG. stream-tts разбирает строки одного соединения по очереди и отвечает
на PING после всего, что пришло раньше, так что после sync() прежние сообщения уже прошли engine.chat. Этим тест
проверяет «сообщение проигнорировано» без пауз наугад.

Методы — корутины цикла событий теста (Loop.run)."""
import asyncio, itertools, time, uuid

from aiohttp import WSMsgType, web

CHANNELS = {"testchan": "1000"}   # канал -> room-id (он же user-id стримера)
TAG_ESC = {"\\": "\\\\", ";": "\\:", " ": "\\s", "\r": "\\r", "\n": "\\n"}


def esc(v):
    return "".join(TAG_ESC.get(c, c) for c in str(v))


def tags(**kw):
    return "@" + ";".join(f"{k.replace('_', '-')}={esc(v)}" for k, v in kw.items())


class FakeIrc:
    def __init__(self):
        self.runner = None
        self.url = ""
        self.clients = []         # открытые соединения: [ws, канал]
        self.connections = 0      # всего подключений (переподключение — +1)
        self.joins = []           # каналы из JOIN по порядку
        self.client_pings = 0     # PING от stream-tts (свой, после тишины)
        self.ignore_ping = False
        self.users = {}           # логин -> user-id
        self.ids = itertools.count(1)
        self.pongs = {}           # метка -> Future

    def reset(self):
        self.ignore_ping = False
        self.client_pings = 0

    async def start(self):
        app = web.Application()
        app.router.add_get("/", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"ws://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/"
        return self

    async def close(self):
        for ws, _ in list(self.clients):
            await ws.close()
        if self.runner:
            await self.runner.cleanup()

    # --- сервер ---
    async def handle(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        client = [ws, ""]
        self.clients.append(client)
        self.connections += 1
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                for line in msg.data.split("\r\n"):
                    if line:
                        await self._line(client, line)
        finally:
            self.clients.remove(client)
        return ws

    async def _line(self, client, line):
        ws = client[0]
        cmd, _, arg = line.partition(" ")
        if cmd == "CAP":
            await ws.send_str(":tmi.twitch.tv CAP * ACK :twitch.tv/tags twitch.tv/commands")
        elif cmd == "NICK":
            await ws.send_str(f":tmi.twitch.tv 001 {arg} :Welcome, GLHF!")
        elif cmd == "JOIN":
            ch = arg.lstrip("#")
            self.joins.append(ch)
            client[1] = ch
            if ch in CHANNELS:
                await ws.send_str(f":justinfan!justinfan@justinfan.tmi.twitch.tv JOIN #{ch}")
                await ws.send_str(tags(emote_only=0, followers_only=-1, r9k=0, room_id=CHANNELS[ch], slow=0,
                                       subs_only=0) + f" :tmi.twitch.tv ROOMSTATE #{ch}")
        elif cmd == "PING":
            self.client_pings += 1
            if not self.ignore_ping:
                await ws.send_str(f":tmi.twitch.tv PONG tmi.twitch.tv {arg}")
        elif cmd == "PONG":
            fut = self.pongs.pop(arg.lstrip(":"), None)
            if fut and not fut.done():
                fut.set_result(True)

    async def _send(self, line, channel="testchan"):
        sent = 0
        for ws, ch in list(self.clients):
            if ch == channel and not ws.closed:
                await ws.send_str(line)
                sent += 1
        assert sent, f"нет подключения к #{channel}"

    # --- тест ---
    def user_id(self, login):
        if login.lower() in CHANNELS:
            return CHANNELS[login.lower()]
        return self.users.setdefault(login.lower(), str(5000 + len(self.users)))

    async def say(self, login, text, badges=(), emotes="", bits=None, reply_to=None, channel="testchan"):
        """PRIVMSG от зрителя. badges — set_id бейджей (subscriber, vip, moderator, broadcaster); reply_to —
        (msg_id, логин) сообщения, на которое ответ. Возвращает ID сообщения."""
        msg_id = str(uuid.uuid4())
        t = dict(badge_info="", badges=",".join(f"{b}/1" for b in badges), color="", display_name=login,
                 emotes=emotes, first_msg=0, id=msg_id, mod=int("moderator" in badges),
                 room_id=CHANNELS[channel], subscriber=int("subscriber" in badges),
                 tmi_sent_ts=int(time.time() * 1000), turbo=0, user_id=self.user_id(login), user_type="")
        if bits:
            t["bits"] = bits
        if reply_to:
            t.update(reply_parent_msg_id=reply_to[0], reply_parent_user_login=reply_to[1].lower(),
                     reply_parent_display_name=reply_to[1], reply_parent_msg_body="...")
        nick = login.lower()
        await self._send(f"{tags(**t)} :{nick}!{nick}@{nick}.tmi.twitch.tv PRIVMSG #{channel} :{text}", channel)
        return msg_id

    async def clear_msg(self, msg_id, login, channel="testchan"):
        """Модер удалил одно сообщение."""
        await self._send(f"{tags(login=login.lower(), room_id='', target_msg_id=msg_id, tmi_sent_ts=0)}"
                         f" :tmi.twitch.tv CLEARMSG #{channel} :...", channel)

    async def clear_chat(self, login=None, channel="testchan"):
        """Таймаут или бан зрителя (login) либо очистка всего чата (без login)."""
        if login:
            line = (f"{tags(ban_duration=600, room_id=CHANNELS[channel], target_user_id=self.user_id(login))}"
                    f" :tmi.twitch.tv CLEARCHAT #{channel} :{login.lower()}")
        else:
            line = f"{tags(room_id=CHANNELS[channel])} :tmi.twitch.tv CLEARCHAT #{channel}"
        await self._send(line, channel)

    async def reconnect(self, channel="testchan"):
        await self._send(":tmi.twitch.tv RECONNECT", channel)

    async def sync(self, channel="testchan", timeout=5):
        """Барьер: всё, что отправлено раньше, stream-tts уже обработал."""
        mark = f"sync-{next(self.ids)}"
        fut = asyncio.get_running_loop().create_future()
        self.pongs[mark] = fut
        await self._send(f"PING :{mark}", channel)
        await asyncio.wait_for(fut, timeout)
