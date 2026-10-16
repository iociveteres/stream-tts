"""Очередь озвучки: приём активаций, синтез (одна фраза вперёд), воспроизведение через оверлей, команды чата."""
import asyncio, collections, contextlib, json, logging, time

import numpy as np
from aiohttp import WSMsgType, web

from . import audio, filters
from .config import SR, TARGET_LUFS, CHARS_PER_SEC, MAX_LEN_FACTOR, MAX_SECONDS, MIN_SECONDS
from .cosy import LIVE

log = logging.getLogger("engine")

MODS = {"broadcaster", "moderator", "lead_moderator"}
ACCESS = {"all": None, "mods": MODS, "vip": MODS | {"vip"}, "subs": MODS | {"vip", "subscriber", "founder"}}

# Причины возврата баллов (в статистике и интерфейсе)
R_OFF, R_BAN, R_ACCESS, R_EMPTY, R_FULL = "озвучка выключена", "бан", "уровень доступа", "пустой текст", "очередь полна"
R_NOVOICE, R_SYNTH, R_SKIP, R_CLEAR, R_RESTART = ("нет голосов", "ошибка синтеза", "!ttsskip", "!ttsclear",
                                                  "остались после перезапуска")


def max_samples(text):
    sec = max(MIN_SECONDS, min(MAX_SECONDS, MAX_LEN_FACTOR * len(text) / CHARS_PER_SEC))
    return int(sec * SR)


class Item:
    def __init__(self, rid, reward_id, user_id, login, display, raw, test=False):
        self.id, self.reward_id, self.user_id, self.login, self.display = rid, reward_id, user_id, login, display
        self.raw, self.test = raw, test
        self.text = ""          # после фильтров
        self.speak = ""         # что синтезируется (с «<ник> говорит:»)
        self.voice = ""
        self.received = time.time()
        self.state = "queued"   # queued | synth | ready
        self.chunks = []        # PCM16 после усиления
        self.samples = 0
        self.synth_done = False
        self.error = None
        self.cancelled = False
        self.synth_ms = None
        self.play_start = None

    def public(self, playing=False):
        return dict(id=self.id, login=self.login, display=self.display, text=self.text, voice=self.voice,
                    state="playing" if playing else self.state, test=self.test, age=round(time.time() - self.received),
                    audio=round(self.samples / SR, 1))


class Client:
    def __init__(self, ws):
        self.ws, self.ready, self.since = ws, False, time.time()

    async def send(self, data):
        if isinstance(data, bytes):
            await self.ws.send_bytes(data)
        else:
            await self.ws.send_str(json.dumps(data, ensure_ascii=False))


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

    async def handle(self, request):
        ws = web.WebSocketResponse(heartbeat=15)
        await ws.prepare(request)
        c = Client(ws)
        prev = self.active
        self.clients.append(c)
        with contextlib.suppress(Exception):
            if prev:
                await prev.send({"type": "inactive"})
            await c.send({"type": "active"})
        await self.engine.poke()
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                if data.get("type") == "ready":
                    c.ready = bool(data.get("ready", True))
                elif data.get("type") == "ended":
                    self.engine.ended.add(data.get("id"))
                await self.engine.poke()
        finally:
            was_active = c is self.active
            self.clients.remove(c)
            if was_active and self.active:
                with contextlib.suppress(Exception):
                    await self.active.send({"type": "active"})
            await self.engine.poke()
        return ws


class Engine:
    def __init__(self, settings, db, cosy, voices, profanity):
        self.s, self.db, self.cosy, self.voices, self.profanity = settings, db, cosy, voices, profanity
        self.twitch = None
        self.queue = []
        self.current = None
        self.cond = asyncio.Condition()
        self.overlay = Overlay(self)
        self.ended = set()
        self.seen = set()               # ID активаций, уже принятых в этом запуске
        self.rtf = collections.deque(maxlen=20)  # время синтеза / длина аудио
        self.journal = collections.deque(maxlen=50)
        self.dirty = asyncio.Event()

    # --- общее ---
    async def poke(self):
        async with self.cond:
            self.cond.notify_all()
        self.dirty.set()

    def note(self, text):
        log.info(text)
        self.journal.appendleft(dict(ts=time.time(), text=text))
        self.dirty.set()

    def busy(self):
        """Есть живая работа для cosyvoice: тогда фоновые задачи (примеры голосов) ждут."""
        return any(not it.synth_done for it in self.queue)

    def snapshot(self):
        cur = self.current
        return dict(queue=[it.public(it is cur) for it in self.queue], enabled=self.s["enabled"],
                    overlay=self.overlay.public(), journal=list(self.journal),
                    rtf=round(sum(self.rtf) / len(self.rtf), 2) if self.rtf else None)

    # --- приём активаций ---
    def access_ok(self, user_id):
        need = ACCESS.get(self.s["access"])
        if need is None or (self.twitch and user_id == self.twitch.user_id):
            return True
        return bool(self.db.badges(user_id) & need)

    def voice_for(self, user_id, login):
        u = self.db.user(user_id)
        enabled = set(self.voices.enabled())
        if u and u["voice"] in enabled:
            return u["voice"]
        v = self.voices.by_hash(login)   # голос не выбран или его выключили
        if v:
            self.db.set_voice(user_id, login, v)
        return v

    def clean(self, raw):
        t = raw[: int(self.s["max_chars"])]
        if self.s["filter_links"]:
            t = filters.strip_links(t)
        t = filters.normalize_space(t)
        if self.s["filter_profanity"]:
            t = self.profanity.clean(t)
        return t if filters.has_words(t.replace(filters.BEEP, "")) else ""

    async def redemption(self, rid, reward_id, user_id, login, display, raw, test=False):
        if rid in self.seen:
            return
        self.seen.add(rid)
        item = Item(rid, reward_id, user_id, login.lower(), display or login, raw or "", test)
        if not test:
            self.db.touch_user(user_id, item.login)
        reason = None
        if not self.s["enabled"]:
            reason = R_OFF
        elif self.db.banned(item.login):
            reason = R_BAN
        elif not self.access_ok(user_id):
            reason = R_ACCESS
        else:
            spoken = self.clean(item.raw)   # мат помечен filters.BEEP: там будет писк
            item.text = spoken.replace(filters.BEEP, filters.BEEP_SHOWN)
            if not spoken:
                reason = R_EMPTY
            elif len(self.queue) >= int(self.s["queue_limit"]):
                reason = R_FULL
            else:
                item.voice = self.voice_for(user_id, item.login)
                if not item.voice:
                    reason = R_NOVOICE
        if reason:
            self.note(f"{item.display}: возврат ({reason})")
            await self.resolve(item, "refund", reason)
            return
        if self.s["read_nick"]:
            nick = self.profanity.clean(item.display) if self.s["filter_profanity"] else item.display
            spoken = f"{nick} говорит: {spoken}"
        item.speak = spoken
        self.queue.append(item)
        self.note(f"{item.display} [{item.voice}]: {item.text}")
        await self.poke()
        await self.voices.preempt()

    async def resolve(self, item, outcome, reason=None):
        """Итог активации: FULFILLED для сыгранной, CANCELED (возврат баллов) для остальных."""
        if item in self.queue:
            self.queue.remove(item)
        if outcome != "played":
            item.cancelled = True
        if not item.test and self.twitch:
            asyncio.create_task(self.twitch.set_status(item.reward_id, [item.id],
                                                       "FULFILLED" if outcome == "played" else "CANCELED"))
        wait = round((item.play_start - item.received) * 1000) if item.play_start else None
        self.db.log(redemption=item.id, ts=item.received, user_id=item.user_id, login=item.login,
                    voice=item.voice or None, text_len=len(item.text or item.raw), outcome=outcome, reason=reason,
                    wait_ms=wait, synth_ms=item.synth_ms, audio_ms=round(item.samples / SR * 1000) or None,
                    test=int(item.test))
        await self.poke()

    # --- управление ---
    async def cancel(self, item, reason):
        synthesizing = item.state == "synth" and not item.synth_done
        item.cancelled = True
        if item is self.current and self.overlay.active:
            with contextlib.suppress(Exception):
                await self.overlay.active.send({"type": "stop", "id": item.id})
        await self.resolve(item, "refund", reason)
        if synthesizing:
            await self.cosy.stop()

    async def skip(self, by=""):
        if self.queue:
            item = self.queue[0]
            self.note(f"{by or 'интерфейс'}: пропуск фразы {item.display}")
            await self.cancel(item, R_SKIP)

    async def remove(self, rid):
        for item in self.queue:
            if item.id == rid:
                await self.cancel(item, R_SKIP)
                return True
        return False

    async def clear(self, by="", reason=R_CLEAR):
        if self.queue:
            self.note(f"{by or 'интерфейс'}: очередь очищена ({len(self.queue)})")
        for item in list(self.queue):
            await self.cancel(item, reason)

    async def set_enabled(self, on, by=""):
        self.s.update(enabled=bool(on))
        self.note(f"{by or 'интерфейс'}: озвучка {'включена' if on else 'выключена'}")
        if not on:
            await self.clear(by, R_OFF)
        if self.twitch:
            await self.twitch.set_paused(not on)
        await self.poke()

    async def chat(self, ev):
        """Сообщение чата (EventSub channel.chat.message): бейджи для уровня доступа и команды."""
        text = (ev.get("message") or {}).get("text", "").strip()
        uid, login = ev["chatter_user_id"], ev["chatter_user_login"].lower()
        badges = {b["set_id"] for b in ev.get("badges") or []}
        self.db.touch_user(uid, login, badges)
        if not text.lower().startswith("!tts"):
            return
        parts = text.split()
        cmd, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if cmd == "!ttssf":
            v = self.voices.find(arg)
            if v:  # неизвестный или выключенный голос молча игнорируется
                self.db.set_voice(uid, login, v)
                self.note(f"{login} выбрал голос {v}")
            return
        if not (badges & MODS or uid == ev.get("broadcaster_user_id")):
            return
        who = ev.get("chatter_user_name") or login
        if cmd == "!ttsskip":
            await self.skip(who)
        elif cmd == "!ttsclear":
            await self.clear(who)
        elif cmd in ("!ttsban", "!ttsunban") and arg:
            nick = arg.lstrip("@").lower()
            self.db.ban(nick, cmd == "!ttsban")
            self.note(f"{who}: {'бан' if cmd == '!ttsban' else 'разбан'} {nick}")
            if cmd == "!ttsban":
                for item in [it for it in self.queue if it.login == nick and it is not self.current]:
                    await self.cancel(item, R_BAN)
        elif cmd == "!ttson":
            await self.set_enabled(True, who)
        elif cmd == "!ttsoff":
            await self.set_enabled(False, who)

    # --- синтез ---
    def _next_synth(self):
        for it in self.queue[:2]:   # текущая фраза и одна следующая
            if it.state == "queued" and not it.cancelled:
                return it
        return None

    async def synth_worker(self):
        while True:
            async with self.cond:
                await self.cond.wait_for(lambda: self._next_synth() is not None)
                item = self._next_synth()
            try:
                async with self.cosy.lane.hold(LIVE, item.voice):
                    if item.cancelled:
                        continue
                    item.state = "synth"
                    await self.poke()
                    t0 = time.monotonic()
                    await self._synth(item)
                    item.synth_ms = round((time.monotonic() - t0) * 1000)
                    if item.samples and not item.cancelled:
                        self.rtf.append(item.synth_ms / 1000 / (item.samples / SR))
            except Exception as e:
                log.exception("синтез %s", item.id)
                item.error = str(e) or type(e).__name__
            finally:
                item.synth_done = True
                if item.state == "synth":
                    item.state = "ready"
                await self.poke()

    async def _push(self, item, x):
        if len(x):
            item.chunks.append(audio.to_pcm16(x))
            item.samples += len(x)
            await self.poke()

    async def _synth(self, item):
        """Фраза синтезируется кусками между писками (мат), писк вставляется готовым тоном."""
        cfg = self.s.voice(item.voice)
        post = self.voices.post(item.voice)
        limit_n = max_samples(item.speak.replace(filters.BEEP, " пип "))
        ratio = self.s["token_ratio"]
        parts = item.speak.split(filters.BEEP)
        beep = audio.beep()
        if self.s["streaming"] and not post:
            gain = audio.db(cfg.get("calib_db", 0.0) + cfg["gain_db"])
            for k, part in enumerate(parts):
                if k and await self._push_limited(item, beep, limit_n):
                    return
                if not filters.has_words(part):
                    continue
                async with contextlib.aclosing(self.cosy.stream(item.voice, part.strip(), ratio)) as chunks:
                    async for x in chunks:
                        if item.cancelled:
                            return   # /tts/stop уже вызван в cancel()
                        if await self._push_limited(item, audio.soft_clip(x * gain), limit_n):
                            await self.cosy.stop()
                            return
        else:
            speech = []
            for part in parts:
                x = np.zeros(0, np.float32)
                if filters.has_words(part):
                    x = await self.cosy.synth(item.voice, part.strip(), ratio)
                    if item.cancelled:
                        return
                    if post:
                        x = await self.voices.apply_post(item.voice, x, part.strip())
                speech.append(x)
            # громкость считается по речи без писков, писк остаётся на своём уровне
            lens = [len(x) for x in speech]
            joined, _ = await asyncio.to_thread(audio.normalize, np.concatenate(speech), TARGET_LUFS,
                                                cfg["gain_db"])
            out, pos = [], 0
            for k, n in enumerate(lens):
                if k:
                    out.append(beep)
                out.append(joined[pos:pos + n])
                pos += n
            x = np.concatenate(out)
            if len(x) > limit_n:
                x = audio.fade_out(x[:limit_n])
            for i in range(0, len(x), SR // 2):
                await self._push(item, x[i:i + SR // 2])

    async def _push_limited(self, item, x, limit_n):
        """Кусок с учётом жёсткой обрезки; True — предел достигнут."""
        room = limit_n - item.samples
        cut = len(x) >= room
        if cut:
            x = audio.fade_out(x[:room])
            log.info("обрезка %s на %.1f с", item.id, limit_n / SR)
        await self._push(item, x)
        return cut

    # --- воспроизведение ---
    def _playable(self):
        if not self.queue or not self.overlay.ready():
            return False
        it = self.queue[0]
        return bool(it.chunks) or it.synth_done

    async def player(self):
        while True:
            async with self.cond:
                await self.cond.wait_for(self._playable)
                item, ov = self.queue[0], self.overlay.active
            if item.synth_done and not item.chunks:
                self.note(f"{item.display}: ошибка синтеза, возврат ({item.error or 'пустой звук'})")
                await self.resolve(item, "refund", R_SYNTH)
                continue
            self.current = item
            try:
                result = await self._play(item, ov)
            except Exception as e:  # оверлей отвалился посреди отправки
                log.info("оверлей: %s", e)
                result = "lost"
            finally:
                self.current = None
            if result == "done" and item in self.queue:
                await self.resolve(item, "played")
            elif result == "lost":
                await asyncio.sleep(0.2)

    async def _play(self, item, ov):
        if item.play_start is None:
            item.play_start = time.time()
        self.ended.discard(item.id)
        subtitle = f"{item.display}: {item.text}" if self.s["subtitles"] else ""
        await ov.send({"type": "start", "id": item.id, "sr": SR, "subtitle": subtitle})
        await self.poke()
        sent = 0
        lost = lambda: ov is not self.overlay.active or ov.ws.closed
        while True:
            while sent < len(item.chunks):
                await ov.send(item.chunks[sent])
                sent += 1
            if item.cancelled:
                return "cancelled"
            if lost():
                return "lost"
            if item.synth_done:
                break
            async with self.cond:
                await self.cond.wait_for(lambda: item.cancelled or sent < len(item.chunks) or item.synth_done
                                         or lost())
        await ov.send({"type": "end", "id": item.id})
        try:
            async with self.cond:
                await asyncio.wait_for(
                    self.cond.wait_for(lambda: item.id in self.ended or item.cancelled or lost()),
                    item.samples / SR + 10)
        except asyncio.TimeoutError:
            log.warning("оверлей не сообщил о конце %s", item.id)
        if item.cancelled:
            return "cancelled"
        if item.id not in self.ended and lost():
            return "lost"
        return "done"
