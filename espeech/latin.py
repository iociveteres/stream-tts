"""Латиница кириллицей для нормализатора ESpeech (normalize.py): словарь слов чата и брендов, названия английских
букв для аббревиатур, русский транслит (по словарю словоформ, если он есть) и грубое английское чтение;
разбиение CamelCase в никах."""
import re

# латиница и смешанное: слово (нижний регистр) → чтение
LATIN_WORDS = {
    # чат
    "gg": "гэгэ", "ggwp": "гэгэ вэпэ", "wp": "вэпэ", "glhf": "гэ эл эйч эф", "lol": "лол", "kek": "кек", "lul": "лул",
    "omg": "омайгад", "wtf": "вэтээф", "pog": "пог", "poggers": "погерс", "pogchamp": "погчамп", "kappa": "каппа",
    "ez": "изи", "easy": "изи", "ok": "окей", "okay": "окей", "yes": "йес", "no": "ноу", "imba": "имба",
    "nerf": "нёрф", "buff": "бафф", "afk": "афк", "rip": "рип", "brb": "би ар би", "gl": "гэ эл", "hf": "хэ эф",
    "noob": "нуб", "pro": "про", "op": "оп", "wow": "вау", "lmao": "лмао", "xd": "", "f": "эф", "plz": "плиз",
    "pls": "плиз", "thx": "сенкс", "ty": "сенкью", "np": "но проблем", "irl": "ай ар эл", "rofl": "рофл",
    "nice": "найс", "shot": "шот", "epic": "эпик", "fail": "фейл", "win": "вин", "lose": "луз", "go": "гоу",
    "next": "некст", "stream": "стрим", "streamer": "стример", "chat": "чат", "follow": "фоллоу", "sub": "саб",
    "donate": "донат", "thank": "сэнк", "thanks": "сэнкс", "you": "ю", "for": "фор", "the": "зе", "this": "зис",
    "that": "зэт", "love": "лав", "game": "гейм", "hello": "хеллоу", "hi": "хай", "bye": "бай", "everyone": "эвриван",
    "headshot": "хэдшот", "skill": "скилл", "boss": "босс", "team": "тим", "clutch": "клатч", "ace": "эйс",
    "best": "бест", "player": "плеер", "ever": "эвер", "killer": "киллер", "dark": "дарк", "lord": "лорд",
    "shadow": "шэдоу", "fiend": "финд", "camel": "кэмел", "case": "кейс", "nick": "ник", "gamer": "геймер",
    "bot": "бот", "mr": "мистер", "dr": "доктор", "is": "из", "it": "ит", "and": "энд", "of": "оф", "my": "май",
    "me": "ми", "fun": "фан", "good": "гуд", "bad": "бэд", "cool": "кул", "top": "топ", "lag": "лаг", "bug": "баг",
    "head": "хед", "pc": "пи си", "aaa": "трипл эй", "vs": "versus",
    # бренды, игры, герои WC3 (старые имена русской локализации — см. AGENTS.md)
    "warcraft": "варкрафт", "dota": "дота", "cs": "кс", "csgo": "кс го", "steam": "стим", "twitch": "твич",
    "youtube": "ютуб", "discord": "дискорд", "blizzard": "близзард", "arthas": "Артес", "thrall": "Тралл",
    "jaina": "Джайна", "illidan": "Иллидан", "uther": "Утер", "sylvanas": "Сильвана", "minecraft": "майнкрафт",
    "gta": "гэ тэ а", "iphone": "айфон", "photoshop": "фотошоп", "counter": "контр", "strike": "страйк",
    "wc": "варкрафт", "rtx": "эр тэ икс", "gtx": "джи ти икс", "fortnite": "фортнайт", "valorant": "валорант",
    "telegram": "телеграм", "windows": "виндоус", "google": "гугл", "obs": "о бэ эс", "freeman": "Фримен",
    "papich": "Папич", "sunboy": "Санбой", "saigo": "Сайго", "ermak": "Ермак", "jeet": "Джит", "jeetbot": "Джит бот",
    "nagibator": "нагибатор", "vasya": "Вася", "pupkin": "Пупкин", "lolkek": "лол кек",
}
LATIN_WORDS["vs"] = "против"

# названия букв для аббревиатур
EN_LETTERS = dict(zip("abcdefghijklmnopqrstuvwxyz",
                      ["эй", "би", "си", "ди", "и", "эф", "джи", "эйч", "ай", "джей", "кей", "эл", "эм", "эн", "оу",
                       "пи", "кью", "ар", "эс", "ти", "ю", "ви", "дабл ю", "икс", "уай", "зед"]))
RU_VOWELS = set("аеёиоуыэюя")

# ---------------------------------------------------------------- латиница → кириллица
RU_TR = [("shch", "щ"), ("sch", "щ"), ("zh", "ж"), ("kh", "х"), ("ch", "ч"), ("sh", "ш"), ("ts", "ц"), ("ya", "я"),
         ("ja", "я"), ("yu", "ю"), ("ju", "ю"), ("yo", "ё"), ("jo", "ё"), ("ye", "е"), ("je", "е")]
RU_TR1 = str.maketrans({"a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и",
                        "j": "й", "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р",
                        "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс", "z": "з", "'": "ь"})
# частые русские слова транслитом — когда нет lexicon
RU_TRANSLIT_COMMON = set("privet poka spasibo davai davay kak dela ya tut horosho khorosho normalno da net eshe eshche "
                         "chto kto gde pochemu blin ura igraesh igra strim tozhe vsem vse vsyo nu ok tak mozhno nado "
                         "dobryi dobriy vecher utro den noch ty my vy on ona oni moi tvoi krasava molodec ".split())


RU_TRANSLIT_FIX = {"еше": "ещё", "есче": "ещё", "играеш": "играешь", "молодес": "молодец"}   # ь и щ транслит теряет


def ru_translit(w):
    s = w.lower()
    for a, b in RU_TR:
        s = s.replace(a, b)
    out = []
    for ch in s:
        if ch == "y":
            ch = "й" if out and out[-1] in RU_VOWELS else "ы"
        out.append(ch)
    s = "".join(out).translate(RU_TR1)
    s = re.sub(r"(?<=[аеоуыэюя])и$", "й", s)   # davai → давай
    return RU_TRANSLIT_FIX.get(s, s)


EN_RULES = [  # грубое английское чтение: (регулярка, замена), по порядку
    (r"tion", "шн"), (r"sion", "жн"), (r"igh", "ай"), (r"ough", "оу"), (r"ee", "и"), (r"ea", "и"), (r"oo", "у"),
    (r"ou", "ау"), (r"ow$", "оу"), (r"ow", "ау"), (r"ay", "ей"), (r"ai", "ей"), (r"ey$", "и"), (r"ey", "ей"),
    (r"oy", "ой"), (r"oi", "ой"), (r"aw", "о"), (r"au", "о"), (r"ph", "ф"), (r"th", "т"), (r"sh", "ш"), (r"ch", "ч"),
    (r"ck", "к"), (r"qu", "кв"), (r"wh", "в"), (r"kn", "н"), (r"ng$", "нг"),
    (r"a([bcdfgklmnprstvz])e$", r"ей\1"), (r"i([bcdfgklmnprstvz])e$", r"ай\1"), (r"o([bcdfgklmnprstvz])e$", r"оу\1"),
    (r"u([bcdfgklmnprstvz])e$", r"ю\1"), (r"([bcdfgklmnprstvz])e$", r"\1"), (r"er$", "ер"), (r"y$", "и"),
    (r"^y", "й"), (r"c(?=[eiy])", "с"), (r"c", "к"), (r"x", "кс"), (r"j", "дж"), (r"w", "в"), (r"q", "к"),
    (r"^e", "э"), (r"u(?=[bcdfgklmnprstvz]{1,2}(?:[^aeiou]|$))", "а"),
]
EN_RULES = [(re.compile(a), b) for a, b in EN_RULES]
EN_TR1 = str.maketrans({"a": "а", "b": "б", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и", "k": "к",
                        "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "r": "р", "s": "с", "t": "т", "u": "у",
                        "v": "в", "y": "и", "z": "з", "'": ""})


def en_translit(w):
    s = w.lower()
    # правила с заменой кириллицей применяются по очереди; латиница, которой нет в правилах, — посимвольно
    for rx, rep in EN_RULES:
        s = rx.sub(rep, s)
    return s.translate(EN_TR1)


def latin_word(w, lexicon=None):
    low = w.lower()
    if low in LATIN_WORDS:
        r = LATIN_WORDS[low]
        return r
    if len(w) >= 2 and w.isupper() and len(w) <= 5:
        return " ".join(EN_LETTERS[c] for c in low)   # FPS, NPC, MMR
    if len(w) == 1:
        return {"a": "а", "b": "б", "c": "с", "x": "икс", "i": "ай"}.get(low, EN_LETTERS.get(low, ""))
    if not re.search(r"[aeiouy]", low) and len(low) <= 5:
        return " ".join(EN_LETTERS[c] for c in low)   # brb, wp
    ru = ru_translit(low)
    if low in RU_TRANSLIT_COMMON or (lexicon is not None and ru.replace("ё", "е") in lexicon):
        out = ru
    else:
        out = en_translit(low)
    return out


def split_camel(w):
    """CamelCaseNick → Camel Case Nick; iPhone не трогается (словарь раньше)."""
    return re.sub(r"(?<=[a-zа-яё]{2})(?=[A-ZА-ЯЁ][a-zа-яё])", " ", w)   # «СтРаНнЫй» не разбивается


def latin(s, lexicon):
    return re.sub(r"[A-Za-z]+(?:'[a-z]+)?", lambda m: latin_word(m.group(0), lexicon), s)
