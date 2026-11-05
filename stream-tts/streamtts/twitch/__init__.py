"""Twitch: вход по Device Code Flow (auth.py), Helix — награда, статусы активаций, ответы в чат (helix.py), EventSub
WebSocket (eventsub.py); общие адреса и ошибки — common.py. Класс Twitch собирается из примесей этих модулей.
Режим «Только чтение чата» (twitch_mode: chat) — без входа, чат через анонимный IRC (chat_irc.py)."""
import asyncio, collections, contextlib, logging, os

import aiohttp

from .. import config, store
from ..chat_irc import ChatReader, parse_channel
from ..i18n import tr
from .auth import Auth
from .common import TwitchError
from .eventsub import EventSub
from .helix import Helix, explain

__all__ = ["Twitch", "TwitchError", "explain"]   # TwitchError и explain берёт web/admin.py

log = logging.getLogger("twitch")


class Twitch(Auth, Helix, EventSub):
    def __init__(self, settings, engine):
        self.s, self.engine = settings, engine
        self.token_path = os.path.join(config.DATA, "token.json")
        self.tok = store.read_json(self.token_path, None)
        self.http = None
        self.state = "no_client"     # no_client | logged_out | device | connecting | ok | expired
        self.error = ""
        self.device = None
        self.reward = None           # награда с Twitch (GET custom_rewards)
        self.ws_state = "off"
        self.session_id = None
        self.subscribed = set()
        self.msg_ids = collections.deque(maxlen=200)
        self.refresh_lock = asyncio.Lock()
        self.tasks = {}
        self.reply_error = ""        # последняя ошибка ответа зрителю (показывается у настройки)
        self.irc = ChatReader(engine)

    @property
    def mode(self):
        return self.s["twitch_mode"]

    @property
    def client_id(self):
        return self.s["client_id"] or config.TWITCH_CLIENT_ID

    @property
    def user_id(self):
        return (self.tok or {}).get("user_id")

    @property
    def channel_id(self):
        """ID канала: из токена или, в режиме чтения чата, из ROOMSTATE."""
        return self.irc.room_id if self.mode == "chat" else self.user_id

    @property
    def login(self):
        return (self.tok or {}).get("login")

    def public(self):
        return dict(mode=self.mode, chat=dict(self.irc.public(), channel=self.s["chat_channel"]),
                    state=self.state, error=self.error, client_id=self.client_id,
                    client_id_env=bool(config.TWITCH_CLIENT_ID and not self.s["client_id"]),
                    login=self.login, device=self.device, ws=self.ws_state,
                    scopes=(self.tok or {}).get("scopes", []), reply_error=self.reply_error,
                    reward=dict(self.s["reward"], exists=bool(self.reward),
                                paused=(self.reward or {}).get("is_paused")))

    # --- жизненный цикл ---
    async def start(self):
        self.http = aiohttp.ClientSession()
        if self.mode == "chat":
            self.state = "off"
            await self.irc.start(self.http, self.s["chat_channel"])
        else:
            self._start_app()

    def _start_app(self):
        self.error = ""
        if not self.client_id:
            self.state = "no_client"
        elif self.tok and self.tok.get("client_id") == self.client_id:
            self._spawn("connect", self.connect())
        else:
            self.state = "logged_out"

    async def stop(self):
        """Остановка сервиса: награда на паузу, чтобы зрители не тратили баллы впустую."""
        if self.state == "ok" and self.s["reward"]["id"]:
            with contextlib.suppress(Exception):
                await self.set_paused(True, force=True)
                log.info("награда поставлена на паузу")
        for t in self.tasks.values():
            t.cancel()
        await self.irc.stop()
        await self.http.close()

    async def set_mode(self, mode, channel=None):
        """Смена режима: app — вход через приложение Twitch, chat — только чтение чата канала channel.
        Вход при переходе в чтение чата не стирается, награда ставится на паузу: её активации никто не обработает."""
        if channel is not None:
            ch = parse_channel(channel)
            if not ch:
                raise TwitchError(400, tr("Не похоже на ник канала Twitch"))
            self.s.data["chat_channel"] = ch
        if mode == "chat":
            if self.mode == "app":
                if self.state == "ok":
                    with contextlib.suppress(Exception):
                        await self.set_paused(True, force=True)
                for name in ("connect", "device", "eventsub", "validate"):
                    t = self.tasks.pop(name, None)
                    if t:
                        t.cancel()
                self.state, self.ws_state, self.device, self.error = "off", "off", None, ""
            self.s.data["twitch_mode"] = "chat"
            self.s.save()
            await self.irc.start(self.http, self.s["chat_channel"])
        elif self.mode == "chat":
            self.s.data["twitch_mode"] = "app"
            self.s.save()
            await self.irc.stop()
            self._start_app()

    def _spawn(self, name, coro):
        old = self.tasks.get(name)
        if old and not old.done() and old is not asyncio.current_task():
            old.cancel()
        self.tasks[name] = asyncio.create_task(self._guard(name, coro))

    async def _guard(self, name, coro):
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("%s", name)
            self.error = explain(e)
