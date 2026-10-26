"""Голоса: список с сервера, включение и громкость, эффекты post, примеры звучания и страница для зрителей."""
import asyncio, glob, hashlib, html, importlib.util, io, json, logging, math, os, re, time, zipfile

import numpy as np

from . import audio, config, i18n, paths, store
from .engines.client import BACKGROUND, PREVIEW
from .i18n import tr

log = logging.getLogger("voices")

DEMO_SEED = 7
DRAFT_PREFIX = "_draft-"   # служебные голоса черновика (editor.py): в список голосов не попадают
# Группы на странице для зрителей — наборы (источники) voice-refs/<модель>/<набор>/ и <голоса>/<набор>/, в этом
# порядке (docs/voice-pipeline.md); games — прежний набор игр до разбиения по играм (может стоять у зрителей программы)
GROUPS = {
    "wc3": "Warcraft III", "morrowind": "Morrowind", "dota2": "Dota 2", "portal": "Portal 2",
    "stalker": "S.T.A.L.K.E.R.", "minecraft": "Minecraft", "neco-arc": "Neco Arc", "altushka": "Альтушка для скуфа",
    "games": "Игры", "streamers": "Стримеры",
}


class Preempted(Exception):
    pass


_effects = {}  # путь -> (mtime, модуль)


def load_effect(path):
    """Скрипт эффекта как модуль (importlib), перезагружается при изменении файла."""
    mtime = os.path.getmtime(path)
    cached = _effects.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    name = "streamtts_fx_" + hashlib.sha1(path.encode()).hexdigest()[:8]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not callable(getattr(mod, "apply", None)):
        raise RuntimeError(tr("в эффекте {path} нет функции apply(звук, аргументы)", path=path))
    mod.FFMPEG = paths.ffmpeg()
    _effects[path] = (mtime, mod)
    return mod


class Voices:
    def __init__(self, settings, tts):
        self.s, self.tts = settings, tts
        self.engine = None
        self.root = os.path.join(config.DATA, "demos")
        self._indexes = {}   # модель -> {голос: {phrase, lufs, ts}}
        self.wake = asyncio.Event()
        self.progress = dict(todo=0, current="", error="")
        self._manifests = (0, {}, {})
        self.preempted = False
        self.rev = 0   # меняется вместе со списком голосов и примерами: админка получает новый список по ws

    def changed(self):
        self.rev += 1
        if self.engine:
            self.engine.dirty.set()

    # --- список ---
    def names(self):
        return list(self.s["known_voices"])

    def enabled(self):
        return [n for n in self.names() if self.s.voice(n)["enabled"]]

    def find(self, name):
        name = name.strip().lower()
        return next((v for v in self.enabled() if v.lower() == name), None)

    def by_hash(self, login):
        pool = sorted(self.enabled())
        if not pool:
            return None
        h = int.from_bytes(hashlib.sha1(login.lower().encode()).digest()[:8], "big")
        return pool[h % len(pool)]

    async def refresh(self):
        """Список голосов с сервера. При изменении досинтезируются примеры."""
        names = [n for n in await self.tts.speakers() if not n.startswith(DRAFT_PREFIX)]
        if names != self.s["known_voices"]:
            added = set(names) - set(self.s["known_voices"])
            for n in names:
                self.s.voice(n)
            self.s.update(known_voices=names)
            log.info("голоса: %d (новые: %s)", len(names), ", ".join(sorted(added)) or "нет")
            self.changed()
            self.wake.set()

    # --- эффекты ---
    def _read_manifests(self):
        """voice-refs/<модель>/<набор>/manifest.json проекта, <голоса>/<набор>/manifest.json установленных наборов и
        <данные>/sets/<набор>/manifest.json голосов из записи на внешнем сервере (editor.py)
        (перечитываются при изменении файлов): post и набор голоса."""
        files = (glob.glob(os.path.join(config.PROJECT, "voice-refs", "*", "*", "manifest.json"))
                 + glob.glob(os.path.join(paths.VOICES, "*", "manifest.json"))
                 + glob.glob(os.path.join(config.DATA, "sets", "*", "manifest.json")))
        stamp = sum(os.path.getmtime(f) for f in files)
        if stamp != self._manifests[0]:
            merged, sets = {}, {}
            for f in files:
                data = store.read_json(f, {})
                merged.update(data)
                sets.update(dict.fromkeys(data, os.path.basename(os.path.dirname(f))))
            self._manifests = (stamp, merged, sets)
        return self._manifests

    def manifests(self):
        return self._read_manifests()[1]

    def set_of(self, voice):
        return self._read_manifests()[2].get(voice, "")

    def post(self, voice):
        p = self.manifests().get(voice, {}).get("post")
        return p if isinstance(p, list) and p else None

    async def apply_post(self, voice, x, text):
        """Эффект поверх синтеза, как в tools/voices/synth.py, но в этом процессе: у скрипта эффекта
        вызывается apply(звук, аргументы). Звук на входе и выходе проходит через PCM16, как раньше через
        WAV-файлы, поэтому результат совпадает с запуском скрипта из командной строки."""
        post = self.post(voice)
        # manifest.json приходит и с наборами голосов из zip: запускать можно только эффекты из effects/
        if not re.fullmatch(r"effects/[\w.-]+\.py", post[0]):
            raise RuntimeError(tr("эффект {fx}: допустимы только скрипты effects/*.py", fx=post[0]))
        fx = load_effect(paths.resource(post[0]))
        args = [a.replace("{text}", text) for a in post[1:]]
        try:
            y = await asyncio.to_thread(fx.apply, audio.pcm16(x), args)
        except (Exception, SystemExit) as e:  # SystemExit: argparse в эффекте при неверных аргументах
            raise RuntimeError(tr("эффект {fx}: {e}", fx=post[0], e=e)) from e
        return audio.pcm16(np.clip(y, -1, 1))

    # --- примеры и калибровка: свои у каждой модели (голос с тем же именем звучит у них по-разному) ---
    def _dir(self, eid=None):
        """Примеры CosyVoice — в demos/, как до второй модели; других моделей — в demos/<модель>/."""
        eid = eid or self.tts.id
        d = self.root if eid == "cosyvoice" else os.path.join(self.root, eid)
        os.makedirs(d, exist_ok=True)
        return d

    def _index(self, eid=None):
        eid = eid or self.tts.id
        if eid not in self._indexes:
            self._indexes[eid] = store.read_json(os.path.join(self._dir(eid), "index.json"), {})
        return self._indexes[eid]

    @staticmethod
    def _calib_key(eid):
        return "calib_db" if eid == "cosyvoice" else f"calib_db_{eid}"

    def calib(self, voice):
        """Усиление голоса для стриминга по примеру текущей модели, дБ."""
        return self.s.voice(voice).get(self._calib_key(self.tts.id))

    def demo_path(self, voice, eid=None):
        return os.path.join(self._dir(eid), voice + ".mp3")

    def forget(self, names=None):
        """Примеры пересинтезировать: все текущей модели (names=None) или эти голоса у всех моделей."""
        if names is None:
            self._index().clear()
            return
        for eid in list(self._indexes):
            for n in names:
                self._indexes[eid].pop(n, None)

    def has_demo(self, voice):
        meta = self._index().get(voice)
        return bool(meta and meta.get("phrase") == self.s["demo_phrase"] and os.path.exists(self.demo_path(voice)))

    def missing(self):
        return [v for v in self.enabled() if not self.has_demo(v)]

    async def make_demo(self, voice, priority=BACKGROUND):
        """Демо-фраза целиком: по ней же калибруется усиление голоса для стриминга."""
        phrase = self.s["demo_phrase"]
        async with self.tts.lane.hold(priority, tr("пример {voice}", voice=voice)):
            eid = self.tts.id   # модель могла смениться, пока пример ждал очереди; после синтеза пишем в её папку
            self.preempted = False
            x = await self.tts.synth(voice, phrase, seed=DEMO_SEED)
            if self.preempted:  # остановлен ради живой очереди: звук неполный
                raise Preempted()
        if self.post(voice):
            x = await self.apply_post(voice, x, phrase)
        y, lufs = await asyncio.to_thread(audio.normalize, x, config.TARGET_LUFS)
        mp3 = await audio.encode_mp3(y)
        with open(self.demo_path(voice, eid), "wb") as f:
            f.write(mp3)
        v = self.s.voice(voice)
        if math.isfinite(lufs):
            v[self._calib_key(eid)] = round(config.TARGET_LUFS - lufs, 2)
        self.s.save()
        index = self._index(eid)
        index[voice] = dict(phrase=phrase, lufs=round(lufs, 2) if math.isfinite(lufs) else None, ts=time.time())
        store.write_json(os.path.join(self._dir(eid), "index.json"), index)
        log.info("пример %s: %.1f LUFS, %d КБ", voice, lufs, len(mp3) // 1024)
        self.changed()

    async def demo_worker(self):
        """Досинтезирует недостающие примеры, пока нет живой очереди."""
        self.wake.set()
        while True:
            await self.wake.wait()
            self.wake.clear()
            while True:
                todo = self.missing()
                self.progress.update(todo=len(todo), current="")
                if not todo or not self.tts.ok:
                    break
                while self.engine and self.engine.busy():
                    await asyncio.sleep(1)
                voice = todo[0]
                self.progress["current"] = voice
                try:
                    await self.make_demo(voice)
                    self.progress["error"] = ""
                except Preempted:
                    pass
                except Exception as e:
                    log.warning("пример %s: %s", voice, e)
                    self.progress["error"] = f"{voice}: {e}"
                    await asyncio.sleep(10)
            self.progress["current"] = ""

    async def preempt(self):
        """Живая фраза не ждёт фоновый пример: его синтез останавливается и повторится позже."""
        holder = self.tts.lane.holder
        if holder and holder[0] == BACKGROUND:
            self.preempted = True
            await self.tts.stop()

    async def preview(self, voice):
        if not self.has_demo(voice):
            await self.make_demo(voice, PREVIEW)
        return self.demo_path(voice)

    # --- страница для зрителей ---
    def page(self, demo_prefix="demos/"):
        groups = {}
        names = sorted(self.enabled())
        for n in names:  # голоса без образца в voice-refs (созданные в WebUI) — в «Другие»
            groups.setdefault(self.set_of(n), []).append(n)
        order = sorted(groups, key=lambda k: (k == "", list(GROUPS).index(k) if k in GROUPS else len(GROUPS), k))
        sections = []
        for key in order:
            cards = []
            for n in groups[key]:
                cmd = html.escape(f"!ttssf {n}")
                player = (f'<audio controls preload="none" src="{demo_prefix}{html.escape(n)}.mp3"></audio>'
                          if self.has_demo(n) else f'<span class="nodemo">{tr("пример ещё готовится")}</span>')
                cards.append(f'<li data-name="{html.escape(n)}"><div class="name">{html.escape(n)}</div>'
                             f'<button class="copy" data-cmd="{cmd}" title="{tr('Скопировать команду')}"><code>{cmd}</code></button>'
                             f'{player}</li>')
            title = tr(GROUPS.get(key, key.upper()) if key else "Другие")
            sections.append(f'<section><h2>{html.escape(title)} <span>{len(groups[key])}</span></h2>'
                            f'<ul>{"".join(cards)}</ul></section>')
        with open(os.path.join(config.HERE, "static", "voices.html"), encoding="utf-8") as f:
            tpl = f.read()
        # {{t:…}} — текст на языке интерфейса (i18n): страница уходит и в zip, без i18n.js
        tpl = re.sub(r"\{\{t:(.*?)\}\}", lambda m: tr(" ".join(m[1].split())), tpl, flags=re.S)
        tpl = tpl.replace('<html lang="ru">', f'<html lang="{i18n.lang}">', 1)
        return (tpl.replace("%COUNT%", str(len(names)))
                .replace("%PHRASE%", html.escape(self.s["demo_phrase"]))
                .replace("%SECTIONS%", "\n".join(sections)))

    def zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("index.html", self.page())
            for n in self.enabled():
                if self.has_demo(n):
                    z.write(self.demo_path(n), f"demos/{n}.mp3", compress_type=zipfile.ZIP_STORED)
        return buf.getvalue()

    def public(self):
        out = []
        for n in self.names():
            v = self.s.voice(n)
            out.append(dict(name=n, enabled=v["enabled"], gain_db=v["gain_db"], calib_db=self.calib(n),
                            demo=self.has_demo(n), post=" ".join(self.post(n) or [])))
        return out
