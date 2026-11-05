"""Очередь озвучки: приём активаций, итог (воспроизведено или возврат баллов), синтез (одна фраза вперёд),
воспроизведение через оверлей. Правила приёма — rules.py, команды чата — commands.py, оверлеи — overlay.py."""
import asyncio, collections, contextlib, logging, time

import numpy as np

from . import audio, commands, filters, rules
from .config import SR, TARGET_LUFS, CHARS_PER_SEC, MAX_LEN_FACTOR, MAX_SECONDS, MIN_SECONDS
from .engines.client import LIVE
from .i18n import tr
from .overlay import Overlay
from .rules import R_ACCESS, R_BAN, R_CLEAR, R_COOLDOWN, R_EMPTY, R_FULL, R_NOVOICE, R_OFF, R_REMOVED, R_SKIP, R_SYNTH

log = logging.getLogger("engine")


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


class Engine:
    def __init__(self, settings, db, tts, voices, profanity):
        self.s, self.db, self.tts, self.voices, self.profanity = settings, db, tts, voices, profanity
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

    def voice_for(self, user_id, login):
        u = self.db.user(user_id)
        enabled = set(self.voices.enabled())
        if u and u["voice"] in enabled:
            return u["voice"]
        if u and u["voice"] and u["voice"] not in self.voices.names():
            # голоса нет у текущей модели (нет файла голоса этой модели или удалён с сервера): выбор зрителя сохраняется
            # — вернётся вместе с моделью
            return self.voices.by_hash(login)
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
        if not self.s["enabled"] or (not item.test and not rules.src(self, item).get("on")):
            reason = R_OFF
        elif self.db.banned(item.login):
            reason = R_BAN
        elif not item.test and not rules.access_ok(self, item):
            reason = R_ACCESS
        elif wait := rules.cooldown_left(self, item):
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
                rules.reply(self, item, reason, wait)
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

    # --- управление ---
    async def cancel(self, item, reason):
        synthesizing = item.state == "synth" and not item.synth_done
        item.cancelled = True
        if item is self.current and self.overlay.active:
            with contextlib.suppress(Exception):
                await self.overlay.active.send({"type": "stop", "id": item.id})
        await self.resolve(item, "refund", reason)
        if synthesizing:
            await self.tts.stop()

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

    async def removed(self, msg_id=None, user_id=None, who="", all_chat=False):
        """Модер удалил сообщение, дал зрителю таймаут или бан, очистил чат (CLEARMSG и CLEARCHAT в режиме «Только
        чтение чата»): такие фразы из чата не читаются."""
        items = [it for it in self.queue if it.chat and (all_chat or (msg_id and it.msg_id == msg_id)
                                                          or (user_id and it.user_id == user_id))]
        if not items:
            return
        self.note(tr("чат очищен модером: убрано фраз — {n}" if all_chat else
                     "{who}: сообщение удалено модером, убрано фраз — {n}", who=who or items[0].display, n=len(items)))
        for item in items:
            await self.cancel(item, R_REMOVED)

    async def set_enabled(self, on, by=""):
        self.s.update(enabled=bool(on))
        self.note(tr("{by}: озвучка включена" if on else "{by}: озвучка выключена", by=by or tr("интерфейс")))
        if not on:
            await self.clear(by, R_OFF)
        if self.twitch:
            await self.twitch.set_paused(self.twitch.want_paused())
        await self.poke()

    async def chat(self, ev):
        """Сообщение чата (EventSub channel.chat.message; его зовут twitch/eventsub.py и chat_irc.py) — commands.py."""
        await commands.chat(self, ev)

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
                async with self.tts.lane.hold(LIVE, item.voice):
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
        parts = item.speak.split(filters.BEEP)
        beep = audio.beep()
        if self.s["streaming"] and not post and self.tts.streams(item.speak):
            gain = audio.db((self.voices.calib(item.voice) or 0.0) + cfg["gain_db"])
            for k, part in enumerate(parts):
                if k and await self._push_limited(item, beep, limit_n):
                    return
                if not filters.has_words(part):
                    continue
                async with contextlib.aclosing(self.tts.stream(item.voice, part.strip())) as chunks:
                    async for x in chunks:
                        if item.cancelled:
                            return   # /tts/stop уже вызван в cancel()
                        if await self._push_limited(item, audio.soft_clip(x * gain), limit_n):
                            await self.tts.stop()
                            return
        else:
            speech = []
            for part in parts:
                x = np.zeros(0, np.float32)
                if filters.has_words(part):
                    x = await self.tts.synth(item.voice, part.strip())
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
        await ov.send({"type": "start", "id": item.id, "sr": SR})
        await self.poke()
        sent = 0
        lost = lambda: ov is not self.overlay.active or ov.closed
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
