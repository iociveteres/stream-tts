"""Twitch Helix: запросы с обновлением токена, награда за баллы (создание, правка, пауза), ответ зрителю в чат,
статусы активаций, возврат оставшихся с прошлого запуска и догон после обрыва EventSub."""
import asyncio, contextlib, json, logging, time

import aiohttp

from ..i18n import tr
from .common import HELIX, REDEMPTION, AuthExpired, TwitchError

log = logging.getLogger("twitch")

HELIX_HINTS = {
    "partner or affiliate": "Баллы канала есть только у компаньонов и партнёров Twitch.",
    "DUPLICATE_REWARD": "Награда с таким названием уже есть на канале.",
}


def explain(e):
    msg = getattr(e, "message", str(e))
    for k, v in HELIX_HINTS.items():
        if k.lower() in msg.lower():
            return f"{tr(v)} ({msg})"
    return msg


class Helix:
    """Примесь класса Twitch (twitch/__init__.py): токен и вход — из auth.py, подписки — из eventsub.py."""

    # --- Helix ---
    async def api(self, method, path, params=None, body=None):
        if self.state not in ("ok", "connecting"):
            raise TwitchError(0, tr("нет входа в Twitch"))
        for attempt in range(2):
            if self.tok["expires_at"] < time.time() + 60:
                try:
                    await self.refresh()
                except AuthExpired:
                    self._expired()
                    raise TwitchError(401, tr("вход истёк"))
            headers = {"Authorization": f"Bearer {self.tok['access_token']}", "Client-Id": self.client_id}
            async with self.http.request(method, HELIX + path, params=params, json=body, headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=20)) as r:
                text = await r.text()
                if r.status == 401 and attempt == 0:
                    self.tok["expires_at"] = 0
                    continue
                if r.status >= 400:
                    try:
                        msg = json.loads(text).get("message") or text
                    except ValueError:
                        msg = text
                    raise TwitchError(r.status, msg)
                return json.loads(text) if text else None
        raise TwitchError(401, tr("вход истёк"))

    # --- награда ---
    async def sync_reward(self):
        """При старте: награда на месте? Затем возврат зависших активаций и пауза по состоянию озвучки."""
        rid = self.s["reward"]["id"]
        self.reward = None
        if not rid:
            return
        try:
            data = await self.api("GET", "/channel_points/custom_rewards",
                                  dict(broadcaster_id=self.user_id, id=rid, only_manageable_rewards="true"))
            self.reward = (data or {}).get("data", [None])[0]
        except TwitchError as e:
            if e.status not in (400, 404):
                raise
        if not self.reward:
            log.warning("награда %s не найдена на канале", rid)
            self.s["reward"]["id"] = ""
            self.s.save()
            return
        await self.refund_leftovers()
        await self.set_paused(self.want_paused())

    def _reward_body(self, cfg):
        cooldown = max(0, int(cfg.get("cooldown") or 0))
        body = dict(title=cfg["title"][:45], cost=max(1, int(cfg["cost"])), prompt=(cfg.get("prompt") or "")[:200],
                    is_user_input_required=True, is_global_cooldown_enabled=cooldown > 0)
        if cooldown:
            body["global_cooldown_seconds"] = cooldown
        return body

    async def save_reward(self, cfg):
        """Создать награду (только так её активациями можно управлять) или обновить созданную."""
        self.s["reward"].update(title=cfg["title"], cost=int(cfg["cost"]), cooldown=int(cfg.get("cooldown") or 0),
                                prompt=cfg.get("prompt", ""))
        self.s.save()
        body = self._reward_body(self.s["reward"])
        rid = self.s["reward"]["id"]
        if rid:
            data = await self.api("PATCH", "/channel_points/custom_rewards",
                                  dict(broadcaster_id=self.user_id, id=rid), body)
        else:
            data = await self.api("POST", "/channel_points/custom_rewards", dict(broadcaster_id=self.user_id), body)
            self.s["reward"]["id"] = data["data"][0]["id"]
            self.s.save()
            log.info("награда создана: %s", self.s["reward"]["id"])
        self.reward = data["data"][0]
        await self.set_paused(self.want_paused())
        if self.session_id:
            await self._subscribe(self.session_id)

    async def delete_reward(self):
        rid = self.s["reward"]["id"]
        if rid:
            with contextlib.suppress(TwitchError):
                await self.api("DELETE", "/channel_points/custom_rewards", dict(broadcaster_id=self.user_id, id=rid))
        self.s["reward"]["id"] = ""
        self.s.save()
        self.reward = None
        self.subscribed.discard(REDEMPTION)

    def want_paused(self):
        """Награда на паузе, пока озвучка выключена или способ «награда» отключён: баллы не тратятся впустую."""
        return not (self.s["enabled"] and self.s["sources"]["reward"]["on"])

    async def set_paused(self, paused, force=False):
        rid = self.s["reward"]["id"]
        if not rid or self.state != "ok":
            return
        if not force and self.reward and self.reward.get("is_paused") == paused:
            return
        try:
            data = await self.api("PATCH", "/channel_points/custom_rewards", dict(broadcaster_id=self.user_id, id=rid),
                                  dict(is_paused=paused))
            self.reward = data["data"][0]
        except TwitchError as e:
            log.warning("пауза награды: %s", e)
            self.error = explain(e)

    # --- ответ зрителю ---
    async def tell(self, display, text, reply_to=""):
        """Ответ об отказе в чат: ответом на сообщение с командой, иначе с @ником. Личные сообщения отвергнуты:
        у Twitch на них много ограничений (подтверждённый телефон, около 40 новых собеседников в день), и они
        забивали бы личку стримера незнакомцами."""
        body = dict(broadcaster_id=self.user_id, sender_id=self.user_id, message=text if reply_to else f"@{display} {text}")
        if reply_to:
            body["reply_parent_message_id"] = reply_to
        try:
            data = await self.api("POST", "/chat/messages", None, body)
            drop = ((data or {}).get("data") or [{}])[0].get("drop_reason")
            if drop:   # Twitch принял запрос, но не пропустил сообщение (AutoMod, режим чата)
                raise TwitchError(200, drop.get("message") or drop.get("code"))
            self.reply_error = ""
        except (TwitchError, aiohttp.ClientError) as e:
            log.warning("ответ в чат %s: %s", display, e)
            self.reply_error = tr("Ответ в чат не отправлен: {e}", e=explain(e))

    # --- активации ---
    async def set_status(self, reward_id, ids, status):
        for attempt in range(3):
            try:
                await self.api("PATCH", "/channel_points/custom_rewards/redemptions",
                               [("broadcaster_id", self.user_id), ("reward_id", reward_id)] + [("id", i) for i in ids],
                               dict(status=status))
                return True
            except TwitchError as e:
                if e.status and e.status < 500:
                    log.warning("статус %s %s: %s", status, ids, e)
                    return False
            except aiohttp.ClientError as e:
                log.warning("статус %s: %s", status, e)
            await asyncio.sleep(2 ** attempt)
        return False

    async def unfulfilled(self):
        rid, out, cursor = self.s["reward"]["id"], [], None
        while rid:
            params = dict(broadcaster_id=self.user_id, reward_id=rid, status="UNFULFILLED", first="50", sort="OLDEST")
            if cursor:
                params["after"] = cursor
            data = await self.api("GET", "/channel_points/custom_rewards/redemptions", params)
            out += data["data"]
            cursor = (data.get("pagination") or {}).get("cursor")
            if not cursor or not data["data"]:
                break
        return out

    async def refund_leftovers(self):
        """Активации, оставшиеся UNFULFILLED после падения или простоя, возвращаются."""
        queued = {it.id for it in self.engine.queue}
        left = [r for r in await self.unfulfilled() if r["id"] not in queued]
        for r in left:
            self.engine.seen.add(r["id"])
        for i in range(0, len(left), 50):
            await self.set_status(self.s["reward"]["id"], [r["id"] for r in left[i:i + 50]], "CANCELED")
        for r in left:
            self.engine.db.log(redemption=r["id"], ts=time.time(), user_id=r["user_id"], login=r["user_login"],
                               text_len=len(r.get("user_input") or ""), outcome="refund", reason="остались после перезапуска")
        if left:
            self.engine.note(tr("возвращены баллы за {n} активаций, оставшихся с прошлого запуска", n=len(left)))

    async def catch_up(self):
        """После обрыва EventSub: активации, пришедшие за время переподключения."""
        for r in await self.unfulfilled():
            if r["id"] not in self.engine.seen:
                await self.engine.redemption(r["id"], r["reward"]["id"], r["user_id"], r["user_login"],
                                             r["user_name"], r.get("user_input", ""))
