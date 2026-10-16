"""Настройки (settings.json) и SQLite: зрители, баны, история активаций."""
import copy, json, os, sqlite3, threading, time

from . import config

DEFAULT_PROMPT = "Текст прозвучит на стриме твоим голосом. Выбрать голос: !ttssf <имя>"

DEFAULTS = {
    "client_id": "",
    "enabled": True,
    "reward": {"id": "", "title": "Озвучка голосом", "cost": 500, "cooldown": 0, "prompt": DEFAULT_PROMPT},
    "access": "all",            # all | subs | vip | mods
    "filter_links": True,
    "filter_profanity": True,
    "read_nick": False,
    "subtitles": True,
    "queue_limit": 20,
    "streaming": True,
    "max_chars": 500,
    "token_ratio": 10.0,        # max_token_text_ratio для /tts
    "chunk_tokens": 150,        # токенов в куске стриминга (по умолчанию в cosyvoice.cpp 28: заметно медленнее)
    "demo_phrase": "Привет, чат! Вот так звучит мой голос.",
    "guide_done": [],           # пройденные шаги гайда регистрации приложения
    "voices": {},               # имя -> {enabled, gain_db, calib_db, calib_phrase}
    "known_voices": [],         # последний список голосов сервера (на случай, если он недоступен)
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
    test INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""


class DB:
    """Запросы короткие, поэтому выполняются прямо в цикле событий."""

    def __init__(self, path):
        self.con = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.executescript(SCHEMA)
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
                "wait_ms", "synth_ms", "audio_ms", "test")
        self.q(f"INSERT INTO events({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
               *(e.get(c) for c in cols))

    def purge(self):
        self.q("DELETE FROM events WHERE ts < ?", time.time() - config.HISTORY_DAYS * 86400)
