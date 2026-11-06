"""Twitch EventSub WebSocket: подписки на активации награды и чат, переподключение, события в очередь озвучки."""
import asyncio, contextlib, json, logging

import aiohttp

from ..i18n import tr
from .common import CHAT, EVENTSUB, REDEMPTION, TwitchError

log = logging.getLogger("twitch")


class EventSub:
    """Примесь класса Twitch (twitch/__init__.py): запросы подписок — через api() из helix.py."""

    # --- EventSub ---
    async def _welcome(self, ws):
        msg = await ws.receive(timeout=15)
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError(f"EventSub: {msg.type}")
        data = json.loads(msg.data)
        if data["metadata"]["message_type"] != "session_welcome":
            raise ConnectionError("EventSub: нет session_welcome")
        return data["payload"]["session"]

    async def _subscribe(self, session_id):
        self.session_id = session_id
        wanted = [(CHAT, dict(broadcaster_user_id=self.user_id, user_id=self.user_id))]
        if self.s["reward"]["id"]:
            wanted.append((REDEMPTION, dict(broadcaster_user_id=self.user_id, reward_id=self.s["reward"]["id"])))
        self.subscribed = set()
        for kind, cond in wanted:
            try:
                await self.api("POST", "/eventsub/subscriptions", None, dict(
                    type=kind, version="1", condition=cond, transport=dict(method="websocket", session_id=session_id)))
            except TwitchError as e:
                if e.status != 409:  # 409: подписка уже есть (перенесена при session_reconnect)
                    log.warning("подписка %s: %s", kind, e)
                    self.error = tr("Подписка {kind}: {e}", kind=kind, e=e.message)
                    continue
            self.subscribed.add(kind)

    async def _eventsub(self):
        ws, first, backoff = None, True, 1
        while self.state == "ok":
            try:
                if ws is None:
                    self.ws_state = "connecting"
                    ws = await self.http.ws_connect(EVENTSUB, heartbeat=None)
                    session = await self._welcome(ws)
                    await self._subscribe(session["id"])
                    self.ws_state, backoff = "connected", 1
                    if not first:
                        await self.catch_up()
                    first = False
                ws, session = await self._listen(ws, session)
                # session_reconnect: новое соединение уже открыто, подписки создаются заново
                await self._subscribe(session["id"])
            except asyncio.CancelledError:
                if ws:
                    await ws.close()
                raise
            except Exception as e:
                log.warning("EventSub: %s", e)
                if ws:
                    with contextlib.suppress(Exception):
                        await ws.close()
                ws, self.ws_state = None, "connecting"
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        self.ws_state = "off"

    async def _listen(self, ws, session):
        keepalive = (session.get("keepalive_timeout_seconds") or 10) + 5
        while True:
            try:
                msg = await ws.receive(timeout=keepalive)
            except asyncio.TimeoutError:
                raise ConnectionError("нет keepalive")
            if msg.type != aiohttp.WSMsgType.TEXT:
                raise ConnectionError(f"соединение закрыто ({msg.type.name}, {ws.close_code})")
            data = json.loads(msg.data)
            meta = data["metadata"]
            if meta["message_id"] in self.msg_ids:
                continue
            self.msg_ids.append(meta["message_id"])
            kind = meta["message_type"]
            if kind == "notification":
                sub, ev = data["payload"]["subscription"], data["payload"]["event"]
                try:
                    await self._event(sub["type"], ev)
                except Exception:
                    log.exception("событие %s", sub["type"])
            elif kind == "session_reconnect":
                url = data["payload"]["session"]["reconnect_url"]
                log.info("EventSub: session_reconnect")
                new = await self.http.ws_connect(url, heartbeat=None)
                new_session = await self._welcome(new)
                await ws.close()
                return new, new_session
            elif kind == "revocation":
                status = data["payload"]["subscription"]["status"]
                log.warning("EventSub: подписка отозвана (%s)", status)
                if status == "authorization_revoked":
                    self._expired()
                    raise ConnectionError("доступ отозван")

    async def _event(self, kind, ev):
        if kind == REDEMPTION:
            if ev["reward"]["id"] != self.s["reward"]["id"]:
                return
            await self.engine.redemption(ev["id"], ev["reward"]["id"], ev["user_id"], ev["user_login"],
                                         ev["user_name"], ev.get("user_input", ""))
        elif kind == CHAT:
            await self.engine.chat(ev)
