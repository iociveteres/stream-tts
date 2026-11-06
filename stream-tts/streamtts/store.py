"""Настройки (settings.json) и SQLite: зрители, баны, история активаций."""
import copy, json, os, sqlite3, threading, time

from . import config
from .i18n import CONTENT, REWARD

DEFAULTS = {
    "client_id": "",
    "twitch_mode": "app",       # app — вход через приложение Twitch, chat — только чтение чата (chat_irc.py)
    "chat_channel": "",         # канал для режима chat
    "enabled": True,
    "reward": {"id": "", "title": REWARD["title"]["ru"], "cost": 500, "cooldown": 0, "prompt": REWARD["prompt"]["ru"]},
    # Способы заказать озвучку: on, кто может (access: all | subs | vip | mods), allow — ники, которым способ доступен
    # всегда (мимо access и кулдаунов, но не мимо бана). У команды баллов нет, поэтому от спама — кулдауны, с.
    "sources": {
        "reward": {"on": True, "access": "all", "allow": []},
        "command": {"on": False, "name": "!tts", "access": "subs", "allow": [], "user_cd": 60, "global_cd": 0},
        "chat": {"on": False, "access": "all", "allow": []},
    },
    "reply": "chat",            # ответ зрителю при отказе команды или награды: off | chat
    "filter_links": True,
    "filter_profanity": True,
    "read_nick": False,
    "queue_limit": 20,
    "streaming": True,
    "max_chars": 500,
    "token_ratio": 10.0,        # max_token_text_ratio для /tts
    "chunk_tokens": 150,        # токенов в куске стриминга (по умолчанию в cosyvoice.cpp 28: заметно медленнее)
    "engine": "",               # модель синтеза (engines/): cosyvoice | espeech | supertonic, пустая — ещё не скачана ни одна
    "espeech_steps": 16,        # ESpeech: шагов синтеза (nfe_steps) — 16 / 32 / 12, в каждом запросе
    "espeech_device": "",       # ESpeech: auto | cuda | cpu (у CosyVoice — backend)
    "supertonic_steps": 8,      # Supertonic 3: шагов синтеза (total_step) — 5 / 8 / 12 / 16, в каждом запросе
    "supertonic_speed": 1.05,   # Supertonic 3: скорость речи (speed) — 0.9…1.3, в каждом запросе
    "demo_phrase": CONTENT["demo_phrase"]["ru"],
    "test_phrases": CONTENT["test_phrases"]["ru"],   # редактор голоса: фразы для сравнения вариантов образца
    "voices": {},               # имя -> {enabled, gain_db, calib_db (CosyVoice), calib_db_<модель> (другие модели)}
    "lang": "",                 # ru | en, пустой — выбрать при первом запуске (i18n.init)
    "known_voices": [],         # последний список голосов сервера (на случай, если он недоступен)
    "update_check": "day",      # проверка новой версии (updates.py): day | week | month | off
    "hidden_hints": [],         # скрытые подсказки админки (data-hint): на сервере, чтобы не всплывали в другом браузере
    "tunnel": False,            # страница голосов в интернете через туннель Cloudflare (tunnel.py); поднимается при запуске
}


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class Settings:
    def __init__(self, path):
        self.path = path
        data = read_json(path, {})
        self.data = copy.deepcopy(DEFAULTS)
        for k, v in data.items():
            if isinstance(v, dict) and isinstance(self.data.get(k), dict) and k != "voices":
                self.data[k].update(v)
            else:
                self.data[k] = v
        # Настройки до способов заказа: общий уровень доступа и галочка «все сообщения чата»
        if "sources" not in data and ("access" in data or "chat_all" in data):
            for src in ("reward", "chat"):
                self.data["sources"][src]["access"] = data.get("access", "all")
            self.data["sources"]["chat"]["on"] = bool(data.get("chat_all"))
        for k in ("access", "chat_all", "subtitles"):  # subtitles — убранные субтитры оверлея
            self.data.pop(k, None)
        for name, d in DEFAULTS["sources"].items():  # новые поля способа после обновления
            self.data["sources"][name] = {**copy.deepcopy(d), **self.data["sources"].get(name, {})}

    def __getitem__(self, key):
        return self.data[key]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def update(self, **kw):
        self.data.update(kw)
        self.save()

    def save(self):
        write_json(self.path, self.data)

    def voice(self, name):
        """Настройки голоса; новый голос по умолчанию включён."""
        v = self.data["voices"].setdefault(name, {})
        v.setdefault("enabled", True)
        v.setdefault("gain_db", 0.0)
        return v


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY, login TEXT, voice TEXT, badges TEXT, seen REAL);
CREATE TABLE IF NOT EXISTS bans (login TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, redemption TEXT, ts REAL, user_id TEXT, login TEXT, voice TEXT,
    text_len INTEGER, outcome TEXT, reason TEXT, wait_ms INTEGER, synth_ms INTEGER, audio_ms INTEGER,
    test INTEGER DEFAULT 0, chat INTEGER DEFAULT 0, source TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""


class DB:
    """Запросы короткие, поэтому выполняются прямо в цикле событий."""

    def __init__(self, path):
        self.con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript(SCHEMA)
        cols = {r[1] for r in self.con.execute("PRAGMA table_info(events)")}
        if "chat" not in cols:  # база до режима чата
            self.con.execute("ALTER TABLE events ADD COLUMN chat INTEGER DEFAULT 0")
        if "source" not in cols:  # база до способов заказа: reward | command | chat | test
            self.con.execute("ALTER TABLE events ADD COLUMN source TEXT")
        self.lock = threading.Lock()

    def q(self, sql, *args):
        with self.lock:
            return self.con.execute(sql, args).fetchall()

    # --- зрители ---
    def user(self, user_id):
        rows = self.q("SELECT * FROM users WHERE user_id=?", user_id)
        return dict(rows[0]) if rows else None

    def touch_user(self, user_id, login, badges=None):
        u = self.user(user_id)
        if u is None:
            self.q("INSERT INTO users(user_id, login, voice, badges, seen) VALUES(?,?,?,?,?)",
                   user_id, login, None, json.dumps(sorted(badges)) if badges is not None else None, time.time())
        elif badges is not None:
            self.q("UPDATE users SET login=?, badges=?, seen=? WHERE user_id=?",
                   login, json.dumps(sorted(badges)), time.time(), user_id)
        else:
            self.q("UPDATE users SET login=? WHERE user_id=?", login, user_id)

    def badges(self, user_id):
        u = self.user(user_id)
        return set(json.loads(u["badges"])) if u and u["badges"] else set()

    def set_voice(self, user_id, login, voice):
        self.touch_user(user_id, login)
        self.q("UPDATE users SET voice=? WHERE user_id=?", voice, user_id)

    # --- баны ---
    def bans(self):
        return [r["login"] for r in self.q("SELECT login FROM bans ORDER BY login")]

    def banned(self, login):
        return bool(self.q("SELECT 1 FROM bans WHERE login=?", login.lower()))

    def ban(self, login, on=True):
        if on:
            self.q("INSERT OR IGNORE INTO bans(login, ts) VALUES(?,?)", login.lower(), time.time())
        else:
            self.q("DELETE FROM bans WHERE login=?", login.lower())

    # --- история ---
    def log(self, **e):
        cols = ("redemption", "ts", "user_id", "login", "voice", "text_len", "outcome", "reason",
                "wait_ms", "synth_ms", "audio_ms", "test", "chat", "source")
        self.q(f"INSERT INTO events({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
               *(e.get(c) for c in cols))

    def purge(self):
        self.q("DELETE FROM events WHERE ts < ?", time.time() - config.HISTORY_DAYS * 86400)
