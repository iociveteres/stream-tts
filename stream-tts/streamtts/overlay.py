"""Оверлеи OBS: подключённые клиенты, какой из них играет, готовность и конец фразы. Транспорт — WebSocket в web/
(overlay_ws): он передаёт сокет с send_str/send_bytes и closed и сообщения клиента; здесь aiohttp нет."""
import contextlib, json, time


class Client:
    def __init__(self, ws):
        self.ws, self.ready, self.since = ws, False, time.time()

    async def send(self, data):
        if isinstance(data, bytes):
            await self.ws.send_bytes(data)
        else:
            await self.ws.send_str(json.dumps(data, ensure_ascii=False))

    @property
    def closed(self):
        return self.ws.closed


class Overlay:
    """Подключённые оверлеи. Играет последний подключившийся, остальные показывают «неактивен»."""

    def __init__(self, engine):
        self.engine = engine
        self.clients = []

    @property
    def active(self):
        return self.clients[-1] if self.clients else None

    def ready(self):
        return bool(self.active and self.active.ready)

    def public(self):
        a = self.active
        return dict(count=len(self.clients), active=bool(a), ready=bool(a and a.ready))

    async def connect(self, c):
        """Новый оверлей играет, прежний — «неактивен»."""
        prev = self.active
        self.clients.append(c)
        with contextlib.suppress(Exception):
            if prev:
                await prev.send({"type": "inactive"})
            await c.send({"type": "active"})
        await self.engine.poke()

    async def message(self, c, data):
        if data.get("type") == "ready":
            c.ready = bool(data.get("ready", True))
        elif data.get("type") == "ended":
            # Только от играющего оверлея и про текущую фразу: оверлей открыт без пароля, а любой id раньше
            # пропускал фразу и копился в ended
            cur = self.engine.current
            if c is self.active and cur is not None and data.get("id") == cur.id:
                self.engine.ended.add(cur.id)
        await self.engine.poke()

    async def disconnect(self, c):
        was_active = c is self.active
        self.clients.remove(c)
        if was_active and self.active:
            with contextlib.suppress(Exception):
                await self.active.send({"type": "active"})
        await self.engine.poke()
