"""Поддельный Twitch (id.twitch.tv, api.twitch.tv/helix, eventsub.wss.twitch.tv) для e2e-тестов Twitch-приложения
stream-tts. Адрес — в STREAM_TTS_TEST_TWITCH (config.TEST_TWITCH, только для тестов).

Отвечает на то, что шлёт streamtts/twitch/:
- /oauth2: Device Code Flow (device, token: authorization_pending до authorize()), refresh-токен (одноразовый, как у
  публичного клиента), validate, revoke. Неизвестный Client ID — 400, как у Twitch.
- /helix: награда channel points (создание, чтение, правка, удаление; повтор названия — DUPLICATE_REWARD, канал без
  статуса компаньона — affiliate=False), погашения (список UNFULFILLED страницами, смена статуса), подписки EventSub
  (409 на повтор), сообщение в чат. Просроченный или отозванный токен — 401.
- /ws: EventSub WebSocket — session_welcome, keepalive раз в keepalive с (mute — молчать), уведомления, session_reconnect
  (подписки переезжают в новую сессию), revocation, обрыв соединения (подписки сессии пропадают).

Канал — testchan (user-id 1000, как в fake_irc.py), зрители получают ID по логину. Тест шлёт от имени Twitch
погашения (redeem) и сообщения чата (say); запросы stream-tts записываются (calls, statuses, chat, revoked, refreshes).

Методы-корутины — в цикле событий теста (Loop.run), остальное читается из потока теста."""
import asyncio, http, itertools, json, time, uuid

from aiohttp import WSMsgType, web

CLIENT_ID = "test-client"
CHANNEL = dict(id="1000", login="testchan", name="TestChan")
REDEMPTION = "channel.channel_points_custom_reward_redemption.add"
CHAT = "channel.chat.message"


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def error(status, message):
    return web.json_response(dict(error=http.HTTPStatus(status).phrase, status=status, message=message), status=status)


class Session:
    """Соединение EventSub."""

    def __init__(self, ws):
        self.id = str(uuid.uuid4())
        self.ws = ws
        self.closed = asyncio.Event()


class FakeTwitch:
    def __init__(self):
        self.runner = None
        self.url = ""
        self.ids = itertools.count(1)
        # ID зрителей не повторяются между тестами: stream-tts помнит, кому отвечал (не чаще раза в 30 с)
        self.user_ids = itertools.count(5000)
        self.sessions = {}            # id -> Session (открытые)
        self.reset()

    def reset(self):
        """Канал без награды и входа, всё записанное — с нуля. Открытые соединения EventSub не трогаются."""
        self.devices = {}             # device_code -> {"authorized": bool, "scopes": str}
        self.tokens = {}              # access_token -> {"refresh": ..., "scopes": [...], "valid": bool}
        self.refresh_tokens = {}      # refresh_token -> access_token (одноразовые)
        self.rewards = {}             # id -> награда, как в ответе Helix
        self.redemptions = {}         # id -> погашение, как в ответе Helix
        self.subs = []                # подписки EventSub: dict(id, type, condition, session)
        self.chat = []                # POST /chat/messages: тела запросов
        self.statuses = []            # (status, [id...]) — PATCH погашений по порядку
        self.calls = []               # (метод, путь) всех запросов Helix
        self.revoked = []             # отозванные токены (logout)
        self.refreshes = 0            # обновлений по refresh-токену
        self.polls = 0                # опросов /token, пока код не введён
        self.connections = 0          # подключений EventSub
        self.interval = 0.1           # с между опросами /token (у Twitch — 5)
        self.affiliate = True         # есть ли у канала баллы (иначе 403 на создание награды)
        self.refresh_ok = True        # принимать ли refresh-токен
        self.keepalive = 10           # keepalive_timeout_seconds новой сессии
        self.mute = False             # не слать keepalive: stream-tts решит, что соединение мертво
        self.drop_reason = None       # ответ в чат принят, но не пропущен (AutoMod)
        self.users = {}               # логин -> user-id

    # --- запуск ---
    async def start(self):
        app = web.Application()
        r = app.router
        r.add_post("/oauth2/device", self.device)
        r.add_post("/oauth2/token", self.token)
        r.add_get("/oauth2/validate", self.validate)
        r.add_post("/oauth2/revoke", self.revoke)
        r.add_route("*", "/helix/channel_points/custom_rewards", self.rewards_api)
        r.add_route("*", "/helix/channel_points/custom_rewards/redemptions", self.redemptions_api)
        r.add_post("/helix/eventsub/subscriptions", self.subscribe)
        r.add_post("/helix/chat/messages", self.chat_message)
        r.add_get("/ws", self.eventsub)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        return self

    async def close(self):
        await self.drop()
        if self.runner:
            await self.runner.cleanup()

    # --- OAuth ---
    async def device(self, request):
        form = await request.post()
        if form.get("client_id") != CLIENT_ID:
            return error(400, "invalid client")
        code = f"dev-{next(self.ids)}"
        self.devices[code] = dict(authorized=False, scopes=form.get("scopes", ""))
        return web.json_response(dict(device_code=code, user_code=f"ABCD{len(self.devices):04d}",
                                      verification_uri="https://www.twitch.tv/activate", expires_in=1800,
                                      interval=self.interval))

    async def token(self, request):
        form = await request.post()
        if form.get("client_id") != CLIENT_ID:
            return error(400, "invalid client")
        if form.get("grant_type") == "refresh_token":
            old = self.refresh_tokens.pop(form.get("refresh_token"), None)
            if not old or not self.refresh_ok:
                return error(400, "Invalid refresh token")
            self.refreshes += 1
            self.tokens[old]["valid"] = False
            return web.json_response(self._issue(self.tokens[old]["scopes"]))
        dev = self.devices.get(form.get("device_code"))
        if not dev:
            return error(400, "invalid device code")
        if not dev["authorized"]:
            self.polls += 1
            return error(400, "authorization_pending")
        del self.devices[form["device_code"]]
        return web.json_response(self._issue(dev["scopes"].split()))

    def _issue(self, scopes):
        n = next(self.ids)
        access, refresh = f"access-{n}", f"refresh-{n}"
        self.tokens[access] = dict(scopes=list(scopes), valid=True)
        self.refresh_tokens[refresh] = access
        return dict(access_token=access, refresh_token=refresh, expires_in=14400, scope=list(scopes),
                    token_type="bearer")

    def _token_ok(self, request, prefix):
        auth = request.headers.get("Authorization", "")
        tok = self.tokens.get(auth[len(prefix):]) if auth.startswith(prefix) else None
        return tok if tok and tok["valid"] else None

    async def validate(self, request):
        tok = self._token_ok(request, "OAuth ")
        if not tok:
            return error(401, "invalid access token")
        return web.json_response(dict(client_id=CLIENT_ID, login=CHANNEL["login"], scopes=tok["scopes"],
                                      user_id=CHANNEL["id"], expires_in=14000))

    async def revoke(self, request):
        form = await request.post()
        if form.get("token") in self.tokens:
            self.tokens[form["token"]]["valid"] = False
        self.revoked.append(form.get("token"))
        return web.Response()

    # --- Helix ---
    def _helix(self, request):
        """Проверка токена и Client-Id; None — можно, иначе ответ с ошибкой."""
        self.calls.append((request.method, request.path))
        if request.headers.get("Client-Id") != CLIENT_ID:
            return error(401, "Client ID and OAuth token do not match")
        if not self._token_ok(request, "Bearer "):
            return error(401, "Invalid OAuth token")
        if request.query.get("broadcaster_id", CHANNEL["id"]) != CHANNEL["id"]:
            return error(403, "broadcaster_id must match the user ID in the access token")
        return None

    async def rewards_api(self, request):
        if err := self._helix(request):
            return err
        rid = request.query.get("id")
        if request.method == "GET":
            data = [r for r in self.rewards.values() if not rid or r["id"] == rid]
            return web.json_response(dict(data=data)) if data else error(404, "Not Found")
        if request.method == "DELETE":
            return web.Response(status=204) if self.rewards.pop(rid, None) else error(404, "Not Found")
        body = await request.json()
        if request.method == "POST":
            if not self.affiliate:
                return error(403, "The broadcaster must have partner or affiliate status.")
            rid = str(uuid.uuid4())
            self.rewards[rid] = dict(id=rid, broadcaster_id=CHANNEL["id"], broadcaster_login=CHANNEL["login"],
                                     title="", cost=1, prompt="", is_enabled=True, is_paused=False,
                                     is_user_input_required=False, is_global_cooldown_enabled=False,
                                     global_cooldown_seconds=0)
        elif request.method != "PATCH" or rid not in self.rewards:
            return error(404, "Not Found")
        if any(r["title"] == body.get("title") for k, r in self.rewards.items() if k != rid):
            if request.method == "POST":
                del self.rewards[rid]
            return error(400, "CREATE_CUSTOM_REWARD_DUPLICATE_REWARD")
        reward = self.rewards[rid]
        reward.update(body)
        if "is_global_cooldown_enabled" in body:
            reward["global_cooldown_setting"] = dict(is_enabled=reward["is_global_cooldown_enabled"],
                                                     global_cooldown_seconds=reward["global_cooldown_seconds"])
        return web.json_response(dict(data=[reward]))

    async def redemptions_api(self, request):
        if err := self._helix(request):
            return err
        q = request.query
        reward_id = q.get("reward_id")
        if request.method == "GET":
            found = sorted((r for r in self.redemptions.values()
                            if r["reward"]["id"] == reward_id and r["status"] == q.get("status", r["status"])),
                           key=lambda r: r["n"], reverse=q.get("sort") == "NEWEST")
            start = int(q.get("after") or 0)
            page = found[start:start + int(q.get("first") or 20)]
            more = start + len(page) < len(found)
            return web.json_response(dict(data=[self._public(r) for r in page],
                                          pagination=dict(cursor=str(start + len(page))) if more else {}))
        if request.method != "PATCH":
            return error(405, "Method Not Allowed")
        status = (await request.json()).get("status")
        ids = [i for i in q.getall("id", []) if i in self.redemptions
               and self.redemptions[i]["reward"]["id"] == reward_id and self.redemptions[i]["status"] == "UNFULFILLED"]
        if not ids:
            return error(404, "Not Found")
        for i in ids:
            self.redemptions[i]["status"] = status
        self.statuses.append((status, ids))
        return web.json_response(dict(data=[self._public(self.redemptions[i]) for i in ids]))

    async def subscribe(self, request):
        if err := self._helix(request):
            return err
        body = await request.json()
        sid = body["transport"]["session_id"]
        if sid not in self.sessions:
            return error(400, "websocket transport session does not exist or has already disconnected")
        if any(s["type"] == body["type"] and s["condition"] == body["condition"] for s in self.subs):
            return error(409, "subscription already exists")
        sub = dict(id=str(uuid.uuid4()), type=body["type"], version=body["version"], condition=body["condition"],
                   session=sid, status="enabled")
        self.subs.append(sub)
        return web.json_response(dict(data=[self._sub(sub)]), status=202)

    async def chat_message(self, request):
        if err := self._helix(request):
            return err
        body = await request.json()
        self.chat.append(body)
        out = dict(message_id=str(uuid.uuid4()), is_sent=not self.drop_reason)
        if self.drop_reason:
            out["drop_reason"] = dict(code="msg_rejected", message=self.drop_reason)
        return web.json_response(dict(data=[out]))

    # --- EventSub ---
    async def eventsub(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        s = Session(ws)
        self.connections += 1
        old = self.sessions.pop(request.query.get("reconnect", ""), None)
        if old:   # session_reconnect: подписки переезжают в новую сессию, старое соединение закрывается
            for sub in self.subs:
                if sub["session"] == old.id:
                    sub["session"] = s.id
        self.sessions[s.id] = s
        await self._send(s, "session_welcome", dict(session=dict(
            id=s.id, status="connected", connected_at=now_iso(), keepalive_timeout_seconds=self.keepalive,
            reconnect_url=None)))
        keep = asyncio.create_task(self._keepalive(s, self.keepalive))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.CLOSE:
                    break
        finally:
            keep.cancel()
            s.closed.set()
            if self.sessions.get(s.id) is s:
                del self.sessions[s.id]
                # у Twitch подписки оборвавшейся сессии отключаются: их надо создать заново
                self.subs = [x for x in self.subs if x["session"] != s.id]
        return ws

    async def _keepalive(self, s, every):
        while True:
            await asyncio.sleep(every / 2)
            if not self.mute:
                await self._send(s, "session_keepalive", {})

    async def _send(self, s, kind, payload, sub_type=None, message_id=None):
        meta = dict(message_id=message_id or str(uuid.uuid4()), message_type=kind, message_timestamp=now_iso())
        if sub_type:
            meta.update(subscription_type=sub_type, subscription_version="1")
        if not s.ws.closed:
            await s.ws.send_str(json.dumps(dict(metadata=meta, payload=payload)))
        return meta["message_id"]

    def _sub(self, sub):
        return dict(id=sub["id"], status=sub["status"], type=sub["type"], version=sub["version"], cost=0,
                    condition=sub["condition"], transport=dict(method="websocket", session_id=sub["session"]),
                    created_at=now_iso())

    async def notify(self, kind, event, message_id=None):
        """Уведомление во все сессии с подпиской kind. → сколько отправлено."""
        sent = 0
        for sub in list(self.subs):
            s = self.sessions.get(sub["session"])
            reward = (event.get("reward") or {}).get("id")
            if sub["type"] == kind and s and sub["condition"].get("reward_id", reward) == reward:
                await self._send(s, "notification", dict(subscription=self._sub(sub), event=event), kind, message_id)
                sent += 1
        return sent

    # --- тест ---
    def authorize(self):
        """Стример ввёл код на twitch.tv/activate."""
        for d in self.devices.values():
            d["authorized"] = True

    def expire_tokens(self):
        """Срок access-токенов вышел: Helix и validate отвечают 401, refresh-токен ещё годен."""
        for t in self.tokens.values():
            t["valid"] = False

    def user(self, login):
        if login.lower() == CHANNEL["login"]:
            return CHANNEL["id"]
        if login.lower() not in self.users:
            self.users[login.lower()] = str(next(self.user_ids))
        return self.users[login.lower()]

    @property
    def reward(self):
        """Единственная награда канала или None."""
        return next(iter(self.rewards.values()), None)

    def subscribed(self, kind):
        return any(s["type"] == kind and s["session"] in self.sessions for s in self.subs)

    def _public(self, r):
        return {k: v for k, v in r.items() if k != "n"}

    async def redeem(self, login, text, reward_id=None, deliver=True, message_id=None):
        """Зритель погасил награду: погашение UNFULFILLED и, если deliver, уведомление EventSub. → ID погашения."""
        reward = self.rewards[reward_id] if reward_id else self.reward
        rid = str(uuid.uuid4())
        self.redemptions[rid] = dict(
            n=next(self.ids), id=rid, broadcaster_user_id=CHANNEL["id"], broadcaster_user_login=CHANNEL["login"],
            broadcaster_user_name=CHANNEL["name"], user_id=self.user(login), user_login=login.lower(), user_name=login,
            user_input=text, status="UNFULFILLED", redeemed_at=now_iso(),
            reward=dict(id=reward["id"], title=reward["title"], prompt=reward["prompt"], cost=reward["cost"]))
        if deliver:
            assert await self.notify(REDEMPTION, self._public(self.redemptions[rid]), message_id), \
                "нет подписки на погашения награды"
        return rid

    def status(self, rid):
        return self.redemptions[rid]["status"]

    async def say(self, login, text, badges=(), reward_id=None, message_id=None):
        """Сообщение в чат канала (channel.chat.message). → ID сообщения."""
        msg_id = str(uuid.uuid4())
        ev = dict(broadcaster_user_id=CHANNEL["id"], broadcaster_user_login=CHANNEL["login"],
                  broadcaster_user_name=CHANNEL["name"], chatter_user_id=self.user(login),
                  chatter_user_login=login.lower(), chatter_user_name=login, message_id=msg_id,
                  message=dict(text=text, fragments=[dict(type="text", text=text, cheermote=None, emote=None,
                                                          mention=None)]),
                  color="", badges=[dict(set_id=b, id="1", info="") for b in badges], message_type="text",
                  cheer=None, reply=None, channel_points_custom_reward_id=reward_id)
        assert await self.notify(CHAT, ev, message_id), "нет подписки на чат"
        return msg_id

    async def session_reconnect(self):
        """Twitch переносит соединение: session_reconnect со ссылкой на новое."""
        for s in list(self.sessions.values()):
            await self._send(s, "session_reconnect", dict(session=dict(
                id=s.id, status="reconnecting", keepalive_timeout_seconds=None, connected_at=now_iso(),
                reconnect_url=self.url.replace("http", "ws", 1) + f"/ws?reconnect={s.id}")))

    async def revoke_access(self):
        """Стример отозвал доступ приложения: revocation с authorization_revoked."""
        for sub in list(self.subs):
            s = self.sessions.get(sub["session"])
            if s:
                sub["status"] = "authorization_revoked"
                await self._send(s, "revocation", dict(subscription=self._sub(sub)), sub["type"])
                break

    async def drop(self):
        """Обрыв всех соединений EventSub."""
        for s in list(self.sessions.values()):
            await s.ws.close()
            await s.closed.wait()
