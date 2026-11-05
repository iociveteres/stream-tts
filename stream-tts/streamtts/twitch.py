"""Twitch: вход по Device Code Flow, Helix (награда, статусы активаций), EventSub WebSocket.
Режим «Только чтение чата» (twitch_mode: chat) — без входа, чат через анонимный IRC (chat_irc.py)."""
import asyncio, collections, contextlib, json, logging, os, time

import aiohttp

from . import config, store
from .chat_irc import ChatReader, parse_channel
from .i18n import tr

log = logging.getLogger("twitch")

ID = "https://id.twitch.tv/oauth2"
HELIX = "https://api.twitch.tv/helix"
EVENTSUB = "wss://eventsub.wss.twitch.tv/ws"
if config.TEST_TWITCH:   # поддельный Twitch e2e-тестов (config.TEST_TWITCH)
    ID, HELIX = f"{config.TEST_TWITCH}/oauth2", f"{config.TEST_TWITCH}/helix"
    EVENTSUB = config.TEST_TWITCH.replace("http", "ws", 1) + "/ws"
SCOPES = "channel:manage:redemptions user:read:chat"
# Для ответов зрителю в чат при отказе. Не обязателен: вход, сделанный до его появления, продолжает работать, а
# ответы включаются после повторного входа (интерфейс подсказывает)
REPLY_SCOPE = "user:write:chat"
LOGIN_SCOPES = f"{SCOPES} {REPLY_SCOPE}"
REDEMPTION = "channel.channel_points_custom_reward_redemption.add"
CHAT = "channel.chat.message"


class TwitchError(Exception):
    def __init__(self, status, message):
        super().__init__(f"{status}: {message}")
        self.status, self.message = status, message


class AuthExpired(Exception):
    pass


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


class Twitch:
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

    def _save_token(self, tok):
        self.tok = tok
        if tok is None:
            with contextlib.suppress(OSError):
                os.remove(self.token_path)
        else:
            store.write_json(self.token_path, tok)

    # --- вход ---
    async def set_client_id(self, client_id):
        """Новый Client ID сохраняется, только если Twitch начал с ним вход."""
        client_id = client_id.strip()
        if not client_id:
            raise TwitchError(400, tr("Вставьте Client ID"))
        old = self.s["client_id"]
        if client_id != self.client_id:
            await self.logout()
        self.s.data["client_id"] = client_id
        try:
            await self.begin_login()
        except TwitchError:
            self.s.data["client_id"] = old
            self.state = "logged_out" if self.client_id else "no_client"
            raise
        finally:
            self.s.save()

    async def begin_login(self):
        """Device Code Flow: код для twitch.tv/activate. Ошибка здесь почти всегда — неверный Client ID."""
        self.error = ""
        try:
            async with self.http.post(f"{ID}/device", data=dict(client_id=self.client_id, scopes=LOGIN_SCOPES)) as r:
                data = await r.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            self.error = tr("Twitch недоступен: {e}", e=e or type(e).__name__)
            raise TwitchError(0, self.error)
        if r.status != 200:
            self.state = "logged_out"
            msg = data.get("message", r.status)
            self.error = tr("Twitch не принял Client ID ({msg}). Проверьте, что он скопирован целиком."
                            if r.status in (400, 404) else "Ошибка Twitch: {msg}", msg=msg)
            raise TwitchError(r.status, self.error)
        self.device = dict(code=data["user_code"], uri=data["verification_uri"],
                           expires=time.time() + data["expires_in"])
        self.state = "device"
        self._spawn("device", self._poll_device(data["device_code"], data.get("interval", 5)))

    async def _poll_device(self, device_code, interval):
        while time.time() < self.device["expires"]:
            await asyncio.sleep(interval)
            async with self.http.post(f"{ID}/token", data=dict(
                    client_id=self.client_id, scopes=LOGIN_SCOPES, device_code=device_code,
                    grant_type="urn:ietf:params:oauth:grant-type:device_code")) as r:
                data = await r.json(content_type=None)
            if r.status == 200:
                self.device = None
                self._save_token(self._token(data))
                await self.connect()
                return
            msg = str(data.get("message", ""))
            if msg == "authorization_pending":
                continue
            if msg == "slow_down":
                interval += 5
                continue
            self.device, self.state = None, "logged_out"
            if "secret" in msg.lower():
                self.error = tr("Приложение создано с типом Confidential. Тип клиента нельзя сменить: "
                                "зарегистрируйте новое приложение с Client Type: Public.")
            else:
                self.error = tr("Вход не удался: {msg}", msg=msg or r.status)
            return
        self.device, self.state, self.error = None, "logged_out", tr("Код истёк, начните вход заново.")

    def _token(self, data, user=None):
        tok = dict(access_token=data["access_token"], refresh_token=data.get("refresh_token"),
                   expires_at=time.time() + data.get("expires_in", 3600), client_id=self.client_id)
        if user or self.tok:
            src = user or self.tok
            tok.update(user_id=src.get("user_id"), login=src.get("login"), scopes=src.get("scopes", []))
        return tok

    async def logout(self):
        for name in ("eventsub", "validate", "device"):
            t = self.tasks.pop(name, None)
            if t:
                t.cancel()
        if self.tok:
            with contextlib.suppress(Exception):
                async with self.http.post(f"{ID}/revoke", data=dict(client_id=self.tok["client_id"],
                                                                     token=self.tok["access_token"])):
                    pass
        self._save_token(None)
        self.device, self.reward, self.ws_state = None, None, "off"
        self.state = "logged_out" if self.client_id else "no_client"

    async def refresh(self):
        """Refresh-токен у публичного клиента одноразовый: новый сохраняется сразу."""
        async with self.refresh_lock:
            if self.tok and self.tok["expires_at"] > time.time() + 120:
                return  # уже обновил параллельный запрос
            if not self.tok or not self.tok.get("refresh_token"):
                raise AuthExpired()
            async with self.http.post(f"{ID}/token", data=dict(
                    client_id=self.client_id, grant_type="refresh_token",
                    refresh_token=self.tok["refresh_token"])) as r:
                data = await r.json(content_type=None)
            if r.status != 200:
                log.warning("refresh: %s", data)
                raise AuthExpired()
            self._save_token(self._token(data))

    async def validate(self):
        """GET /validate (Twitch требует раз в час). Заодно канал берётся из токена."""
        for attempt in range(2):
            async with self.http.get(f"{ID}/validate",
                                     headers={"Authorization": f"OAuth {self.tok['access_token']}"}) as r:
                data = await r.json(content_type=None)
            if r.status == 200:
                missing = set(SCOPES.split()) - set(data.get("scopes") or [])
                if missing:
                    raise AuthExpired()
                self.tok.update(user_id=data["user_id"], login=data["login"], scopes=data.get("scopes") or [])
                self._save_token(self.tok)
                return data
            if attempt == 0:
                self.tok["expires_at"] = 0
                await self.refresh()
        raise AuthExpired()

    def _expired(self):
        self.state = "expired"
        self.ws_state = "off"
        self.error = tr("Вход в Twitch истёк: войдите заново.")
        log.warning(self.error)
        t = self.tasks.pop("eventsub", None)
        if t and t is not asyncio.current_task():
            t.cancel()

    async def connect(self):
        self.state, self.error = "connecting", ""
        while True:
            try:
                await self.validate()
                break
            except AuthExpired:
                self._expired()
                return
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                self.error = tr("Twitch недоступен ({e}), повтор через 15 с", e=e or type(e).__name__)
                await asyncio.sleep(15)
        self.state, self.error = "ok", ""
        log.info("вход: канал %s", self.login)
        try:
            await self.sync_reward()
        except Exception as e:
            log.exception("награда")
            self.error = explain(e)
        self._spawn("eventsub", self._eventsub())
        self._spawn("validate", self._validate_loop())

    async def _validate_loop(self):
        while True:
            await asyncio.sleep(3600)
            try:
                await self.validate()
            except AuthExpired:
                self._expired()
                return
            except aiohttp.ClientError as e:
                log.warning("validate: %s", e)

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
