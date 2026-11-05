"""Вход в Twitch: Device Code Flow, токены (обновление одноразовым refresh-токеном, проверка раз в час), выход."""
import asyncio, contextlib, logging, os, time

import aiohttp

from .. import store
from ..i18n import tr
from .common import ID, LOGIN_SCOPES, SCOPES, AuthExpired, TwitchError
from .helix import explain

log = logging.getLogger("twitch")


class Auth:
    """Примесь класса Twitch (twitch/__init__.py): после входа connect() сверяет награду (helix.py) и запускает
    EventSub (eventsub.py)."""

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
