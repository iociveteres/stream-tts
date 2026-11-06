"""Язык интерфейса: русский или английский (settings["lang"]).

Строки в коде пишутся по-русски и сами служат ключами: tr("нет такого голоса") в английском режиме ищет перевод
в static/en.json, без перевода остаётся русская строка. Подстановки — {имя}, как в str.format, тот же словарь
и тот же синтаксис у страниц (static/i18n.js, словарь сервер вставляет в страницу при отдаче).
Журнал и ошибки переводятся в момент создания; причины возвратов хранятся в базе по-русски и переводятся при показе.

Начальный язык: STREAM_TTS_LANG, иначе язык установщика (setup-lang.txt рядом с exe), иначе русский. Тексты для
зрителей по умолчанию (награда, фраза примеров) берутся на языке интерфейса, пока их не меняли."""
import functools, json, logging, os

from . import paths

log = logging.getLogger("i18n")
LANGS = ("ru", "en")
SETUP_LANG = os.path.join(paths.APP, "setup-lang.txt")
lang = "ru"
# В разработке: режим правки строк интерфейса (ui-edit.py, docs/stream-tts.md «Правка текста интерфейса»).
# Файл правок: {русская строка: {"text": новый вариант, "note": заметка, "where": вкладка}}, "" — строку убрать.
EDITS_PATH = paths.env("STREAM_TTS_UI_EDITS")
edits = {}

# Тексты по умолчанию, которые видят зрители: меняются вместе с языком, только если их не трогали
CONTENT = {
    "demo_phrase": {"ru": "Привет, чат! Вот так звучит мой голос.", "en": "Hi chat! This is how my voice sounds."},
    # Тестовые фразы редактора голоса (editor.py): обращение, вопрос, напор, длинная фраза
    "test_phrases": {
        "ru": ["Привет, чат! Сегодня играем до победного, никто не уходит.",
               "Подожди, а ты уверен? Мне кажется, мы что-то упустили.",
               "Ну всё, я пошёл в бой! Если не вернусь — считайте, что я был прав, а вы нет.",
               "Спасибо за подписку! Это лучший подарок за весь вечер, честное слово, я даже не ожидал."],
        "en": ["Hi chat! Today we play until we win, nobody leaves.",
               "Wait, are you sure? I think we missed something.",
               "Alright, I'm going in! If I don't come back, I was right and you were wrong.",
               "Thanks for the sub! Best gift of the whole evening, honestly, I didn't expect that at all."],
    },
}
REWARD = {
    "title": {"ru": "Озвучка голосом", "en": "Text to speech"},
    "prompt": {"ru": "Текст прозвучит на стриме твоим голосом. Выбрать голос: !ttssf <имя>",
               "en": "Your text will be read on stream in your voice. Pick a voice: !ttssf <name>"},
}


@functools.cache
def catalog():
    try:
        with open(os.path.join(paths.STATIC, "en.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        log.warning("en.json: %s", e)
        return {}


def tr(text, /, **kw):   # / — чтобы подстановка {text} не сталкивалась с аргументом
    if lang == "en":
        s = catalog().get(" ".join(text.split()), text)
    elif edits:
        e = edits.get(" ".join(text.split()))
        s = e["text"] if e else text
    else:
        s = text
    return s.format(**kw) if kw else s


def _initial():
    v = paths.env("STREAM_TTS_LANG").lower()
    if v in LANGS:
        return v
    try:
        with open(SETUP_LANG, encoding="utf-8-sig") as f:
            v = f.read().strip().lower()
    except OSError:
        return "ru"
    return v if v in LANGS else "ru"


def preload(settings_path):
    """Язык до загрузки настроек: консоль собранной программы пишет первые строки раньше (desktop.py)."""
    global lang
    try:
        with open(settings_path, encoding="utf-8") as f:
            v = json.load(f).get("lang")
    except (OSError, ValueError, AttributeError):
        v = None
    lang = v if v in LANGS else _initial()


def init(settings):
    """Язык из настроек; при первом запуске — начальный, вместе с текстами по умолчанию на нём."""
    global lang
    if settings.get("lang") not in LANGS:
        set_lang(settings, _initial(), old="ru")
    lang = settings["lang"]


def set_lang(settings, new, old=None):
    """Смена языка; возвращает True, если поменялась фраза примеров (их пора пересинтезировать)."""
    global lang
    old = old or settings.get("lang") or "ru"
    demos = False
    for k, d in CONTENT.items():
        if settings.get(k) == d[old] and new != old:
            settings.data[k] = d[new]
            demos = demos or k == "demo_phrase"
    reward = settings["reward"]
    if not reward.get("id"):  # созданную награду правят на вкладке Twitch
        for k, d in REWARD.items():
            if reward.get(k) == d[old]:
                reward[k] = d[new]
    settings.data["lang"] = lang = new
    settings.save()
    return demos


def load_edits():
    global edits
    if not EDITS_PATH:
        return
    try:
        with open(EDITS_PATH, encoding="utf-8") as f:
            edits = json.load(f)
    except FileNotFoundError:
        edits = {}
    except (OSError, ValueError) as e:
        log.warning("%s: %s", EDITS_PATH, e)


def save_edit(key, text, note="", where=""):
    """Правка строки key; text=None — вернуть исходную."""
    key = " ".join(key.split())
    if text is None or (text == key and not note):
        edits.pop(key, None)
    else:
        e = {"text": text, "note": note, "where": where}
        if key not in catalog():
            e["unknown"] = True   # не ключ словаря: строка с подставленными значениями или склеенная из частей
        edits[key] = e
    os.makedirs(os.path.dirname(os.path.abspath(EDITS_PATH)), exist_ok=True)
    tmp = EDITS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(edits, f, ensure_ascii=False, indent=1)
    os.replace(tmp, EDITS_PATH)


def page(html, edit=False):
    """HTML-страница на текущем языке: lang у <html> и словарь для static/i18n.js. edit — страница с режимом
    правки строк (только админка и только по-русски)."""
    if lang == "ru":
        if not (edit and EDITS_PATH):
            return html
        data = json.dumps(edits, ensure_ascii=False).replace("</", "<\\/")
        return (html.replace("</head>", f"<script>window.UI_EDITS = {data};</script>\n"
                                         '<link rel="stylesheet" href="/static/ui-edit.css">\n</head>', 1)
                .replace("</body>", '<script src="/static/ui-edit.js"></script>\n</body>', 1))
    data = json.dumps(catalog(), ensure_ascii=False).replace("</", "<\\/")
    return (html.replace('<html lang="ru">', f'<html lang="{lang}">', 1)
            .replace("</head>", f"<script>window.I18N = {data};</script>\n</head>", 1))
