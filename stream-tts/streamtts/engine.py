"""Очередь озвучки: приём активаций, синтез (одна фраза вперёд), воспроизведение через оверлей, команды чата."""
import asyncio, collections, contextlib, json, logging, math, time

import numpy as np
from aiohttp import WSMsgType, web

from . import audio, filters
from .config import (SR, TARGET_LUFS, CHARS_PER_SEC, MAX_LEN_FACTOR, MAX_SECONDS, MIN_SECONDS, TOKENS_PER_SEC,
                     STREAM_MIN_CHUNKS)
from .cosy import LIVE
from .i18n import tr

log = logging.getLogger("engine")

MODS = {"broadcaster", "moderator", "lead_moderator"}
ACCESS = {"all": None, "mods": MODS, "vip": MODS | {"vip"}, "subs": MODS | {"vip", "subscriber", "founder"}}

# Причины возврата баллов (в статистике и интерфейсе): в базе по-русски, перевод при показе
R_OFF, R_BAN, R_ACCESS, R_EMPTY, R_FULL = "озвучка выключена", "бан", "уровень доступа", "пустой текст", "очередь полна"
R_NOVOICE, R_SYNTH, R_SKIP, R_CLEAR, R_RESTART = ("нет голосов", "ошибка синтеза", "!ttsskip", "!ttsclear",
                                                  "остались после перезапуска")
R_COOLDOWN = "кулдаун"
# Ответ зрителю при отказе (настройка reply): бан не объясняется, ошибки синтеза и пропуски — не отказ при приёме
REPLIES = {R_OFF: "озвучка сейчас выключена", R_ACCESS: "озвучка доступна только {who}",
           R_EMPTY: "после фильтров озвучивать нечего", R_FULL: "очередь озвучки заполнена, попробуйте позже",
           R_NOVOICE: "нет доступных голосов", R_COOLDOWN: "озвучка снова будет доступна через {n} с"}
ACCESS_WHO = {"subs": "сабам, VIP и модерам", "vip": "VIP и модерам", "mods": "модерам"}
REPLY_EVERY = 30   # с: не чаще одного ответа зрителю, иначе спам командой во время кулдауна превращается в спам ответами
# Команды сервиса: имя команды озвучки не может с ними совпадать
SERVICE_COMMANDS = ("!ttssf", "!ttsskip", "!ttsclear", "!ttsban", "!ttsunban", "!ttson", "!ttsoff")


def max_samples(text):
    sec = max(MIN_SECONDS, min(MAX_SECONDS, MAX_LEN_FACTOR * len(text) / CHARS_PER_SEC))
    return int(sec * SR)


class Item:
    def __init__(self, rid, reward_id, user_id, login, display, raw, source="reward", msg_id=""):
        self.id, self.reward_id, self.user_id, self.login, self.display = rid, reward_id, user_id, login, display
        self.raw, self.source = raw, source   # source: reward | command | chat | test
        self.msg_id = msg_id    # сообщение чата с командой: на него отвечают при отказе
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

    @property
    def test(self):
        return self.source == "test"

    @property
    def chat(self):
        """Команда и сообщения чата: баллов нет, возвращать нечего."""
        return self.source in ("command", "chat")

    def public(self, playing=False):
        return dict(id=self.id, login=self.login, display=self.display, text=self.text, voice=self.voice,
                    state="playing" if playing else self.state, source=self.source,
                    age=round(time.time() - self.received),
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
        self.cd_user, self.cd_global = {}, 0.0   # кулдауны команды: user_id -> время приёма
        self.replied = {}                         # user_id -> время последнего ответа об отказе

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
    def src(self, item):
        return self.s["sources"].get(item.source, {})

    def privileged(self, item):
        """Стример, модеры и ники из списка способа: мимо уровня доступа и кулдаунов."""
        return bool(item.login in self.src(item).get("allow", [])
                    or (self.twitch and item.user_id == self.twitch.user_id)
                    or self.db.badges(item.user_id) & MODS)

    def access_ok(self, item):
        need = ACCESS.get(self.src(item).get("access", "all"))
        return need is None or self.privileged(item) or bool(self.db.badges(item.user_id) & need)

    def cooldown_left(self, item):
        """Секунд до конца кулдауна команды (на зрителя и общего); 0 — можно."""
        if item.source != "command" or self.privileged(item):
            return 0
        c, now = self.src(item), time.time()
        left = max(self.cd_user.get(item.user_id, 0) + int(c.get("user_cd") or 0),
                   self.cd_global + int(c.get("global_cd") or 0)) - now
        return max(0, math.ceil(left))

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
        item = Item(rid, reward_id, user_id, login.lower(), display or login, raw or "", "test" if test else "reward")
        if not test:
            self.db.touch_user(user_id, item.login)
        await self._accept(item)

    async def chat_message(self, ev, source="chat", prefix=""):
        """Сообщение чата (все сообщения или команда озвучки): тот же путь, что награда, но без баллов."""
        frags = ev.get("message", {}).get("fragments") or []
        if ev.get("reply") and frags and frags[0].get("type") == "mention":
            frags = frags[1:]   # ответ начинается с @ника того, кому отвечают: его не читаем
        # эмоуты и cheer-эмоуты — это имена картинок (Kappa, Cheer100), читаются только текст и упоминания
        raw = "".join(f.get("text", "") for f in frags if f.get("type") in ("text", "mention")).strip()
        if prefix and raw.lower().startswith(prefix):
            raw = raw[len(prefix):].strip()
        login = ev["chatter_user_login"].lower()
        item = Item(f"{source}-{ev['message_id']}", "", ev["chatter_user_id"], login,
                    ev.get("chatter_user_name") or login, raw, source, ev["message_id"])
        await self._accept(item)

    async def _accept(self, item):
        user_id = item.user_id
        reason, wait = None, 0
        if not self.s["enabled"] or (not item.test and not self.src(item).get("on")):
            reason = R_OFF
        elif self.db.banned(item.login):
            reason = R_BAN
        elif not item.test and not self.access_ok(item):
            reason = R_ACCESS
        elif wait := self.cooldown_left(item):
            reason = R_COOLDOWN
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
            if item.source != "chat":
                self.reply(item, reason, wait)
            if item.chat:  # чат и команда не засоряют историю: баллов нет, возвращать нечего
                if reason == R_FULL or item.source == "command":
                    self.note(tr("{who}: сообщение чата не озвучено ({reason})", who=item.display, reason=tr(reason)))
                return
            self.note(tr("{who}: возврат ({reason})", who=item.display, reason=tr(reason)))
            await self.resolve(item, "refund", reason)
            return
        if item.source == "command":
            self.cd_user[user_id] = self.cd_global = time.time()
        if self.s["read_nick"]:
            nick = self.profanity.clean(item.display) if self.s["filter_profanity"] else item.display
            spoken = tr("{nick} говорит: {text}", nick=nick, text=spoken)
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
        if not item.test and not item.chat and self.twitch:
            asyncio.create_task(self.twitch.set_status(item.reward_id, [item.id],
                                                       "FULFILLED" if outcome == "played" else "CANCELED"))
        wait = round((item.play_start - item.received) * 1000) if item.play_start else None
        self.db.log(redemption=item.id, ts=item.received, user_id=item.user_id, login=item.login,
                    voice=item.voice or None, text_len=len(item.text or item.raw), outcome=outcome, reason=reason,
                    wait_ms=wait, synth_ms=item.synth_ms, audio_ms=round(item.samples / SR * 1000) or None,
                    test=int(item.test), chat=int(item.chat), source=item.source)
        await self.poke()

    def reply(self, item, reason, wait=0):
        """Зрителю — почему не озвучено (настройка reply: chat — ответом в чате, off — молча)."""
        mode = self.s["reply"]
        if mode == "off" or item.test or reason not in REPLIES or not self.twitch or self.twitch.state != "ok":
            return
        now = time.time()
        if now - self.replied.get(item.user_id, 0) < REPLY_EVERY:
            return
        self.replied[item.user_id] = now
        if item.source == "command" and reason == R_EMPTY and not item.raw.strip():
            text = tr("напишите текст после команды: {cmd} привет", cmd=self.src(item)["name"])
        else:
            text = tr(REPLIES[reason], n=wait, who=tr(ACCESS_WHO.get(self.src(item).get("access"), "")))
        if item.source == "reward":
            text += tr(", баллы вернутся")
        asyncio.create_task(self.twitch.tell(item.display, text, item.msg_id))

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
            self.note(tr("{by}: пропуск фразы {who}", by=by or tr("интерфейс"), who=item.display))
            await self.cancel(item, R_SKIP)

    async def remove(self, rid):
        for item in self.queue:
            if item.id == rid:
                await self.cancel(item, R_SKIP)
                return True
        return False

    async def clear(self, by="", reason=R_CLEAR):
        if self.queue:
            self.note(tr("{by}: очередь очищена ({n})", by=by or tr("интерфейс"), n=len(self.queue)))
        for item in list(self.queue):
            await self.cancel(item, reason)

    async def set_enabled(self, on, by=""):
        self.s.update(enabled=bool(on))
        self.note(tr("{by}: озвучка включена" if on else "{by}: озвучка выключена", by=by or tr("интерфейс")))
        if not on:
            await self.clear(by, R_OFF)
        if self.twitch:
            await self.twitch.set_paused(self.twitch.want_paused())
        await self.poke()

    async def chat(self, ev):
        """Сообщение чата (EventSub channel.chat.message): бейджи для уровня доступа и команды."""
        text = (ev.get("message") or {}).get("text", "").strip()
        uid, login = ev["chatter_user_id"], ev["chatter_user_login"].lower()
        badges = {b["set_id"] for b in ev.get("badges") or []}
        self.db.touch_user(uid, login, badges)
        src = self.s["sources"]
        if not text.startswith("!"):
            # Сообщение, отправленное вместе с нашей наградой, уже озвучивается как активация
            if src["chat"]["on"] and text and (not self.twitch or
                                               ev.get("channel_points_custom_reward_id") != self.s["reward"]["id"]):
                await self.chat_message(ev)
            return
        parts = text.split()
        cmd, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if src["command"]["on"] and cmd == src["command"]["name"]:
            await self.chat_message(ev, "command", cmd)
            return
        if not cmd.startswith("!tts"):
            return   # команды других ботов не читаются
        if cmd == "!ttssf":
            v = self.voices.find(arg)
            if v:  # неизвестный или выключенный голос молча игнорируется
                self.db.set_voice(uid, login, v)
                self.note(tr("{who} выбрал голос {voice}", who=login, voice=v))
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
            self.note(tr("{by}: бан {who}" if cmd == "!ttsban" else "{by}: разбан {who}", by=who, who=nick))
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
        chunk = int(self.s["chunk_tokens"])
        long_enough = len(item.speak) / CHARS_PER_SEC * TOKENS_PER_SEC > STREAM_MIN_CHUNKS * chunk
        if self.s["streaming"] and not post and long_enough:
            gain = audio.db(cfg.get("calib_db", 0.0) + cfg["gain_db"])
            for k, part in enumerate(parts):
                if k and await self._push_limited(item, beep, limit_n):
                    return
                if not filters.has_words(part):
                    continue
                async with contextlib.aclosing(self.cosy.stream(item.voice, part.strip(), ratio, chunk)) as chunks:
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
                self.note(tr("{who}: ошибка синтеза, возврат ({error})", who=item.display,
                                error=item.error or tr("пустой звук")))
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
