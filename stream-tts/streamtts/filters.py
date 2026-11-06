"""Фильтры текста: ссылки вырезаются, мат заменяется на «пип»."""
import os, re

# Маркер писка в тексте: на его месте синтез прерывается и вставляется тон (см. audio.beep)
BEEP = "\x07"
BEEP_SHOWN = "***"

TLDS = ("com net org info biz io co me tv gg ly to cc fm be app dev xyz site online live link shop store club pro "
        "ru su рф ua by kz uz ge am az md lv lt ee pl de fr it es uk us eu cz sk nl se no fi dk ch at jp cn kr in "
        "gl gs ws vk tk ml ga cf top")
LINK = re.compile(
    r"https?://\S+|www\.\S+|"
    r"\b[\w-]+(?:\.[\w-]+)*\.(?:" + "|".join(TLDS.split()) + r")\b(?:[/?#]\S*)?",
    re.IGNORECASE)


def strip_links(text):
    return LINK.sub(" ", text)


# Латиница и цифры, похожие на кириллицу по виду (xуй, пuзда, 6ля)
HOMO = str.maketrans({
    "a": "а", "b": "б", "c": "с", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "н", "i": "и", "j": "й",
    "k": "к", "l": "л", "m": "м", "n": "п", "o": "о", "p": "р", "q": "к", "r": "г", "s": "с", "t": "т",
    "u": "и", "v": "в", "w": "ш", "x": "х", "y": "у", "z": "з", "ё": "е",
    "0": "о", "3": "з", "4": "ч", "6": "б", "@": "а",
})
# Транслит (blyat, huy, pizda)
DIGRAPHS = [("shch", "щ"), ("sch", "щ"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"),
            ("ya", "я"), ("ja", "я"), ("yu", "ю"), ("ju", "ю"), ("yo", "е"), ("jo", "е"), ("ye", "е")]
TRANSLIT = str.maketrans({
    "a": "а", "b": "б", "c": "с", "d": "д", "e": "е", "f": "ф", "g": "г", "h": "х", "i": "и", "j": "й",
    "k": "к", "l": "л", "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т",
    "u": "у", "v": "в", "w": "в", "x": "х", "z": "з", "ё": "е",
    "0": "о", "3": "з", "4": "ч", "6": "б", "@": "а",
})
VOWELS = set("аеиоуыэюяёaeiou")
PREFIXES = ("в вз вы до за из ис на над недо ни о об обо от ото па пере по под пре при про раз рас с со у ох "
            "съ сь въ вь отъ подъ объ разъ изъ надъ взъ").split()
LATIN = re.compile(r"[a-z]")
KEEP = re.compile(r"[^0-9a-zа-яё@]")


def _translit(w):
    for a, b in DIGRAPHS:
        w = w.replace(a, b)
    out = []
    for ch in w:
        if ch == "y":  # после гласной й (huy), иначе ы
            ch = "й" if out and out[-1] in VOWELS else "ы"
        out.append(ch)
    return "".join(out).translate(TRANSLIT)


def _dedupe(w):
    return re.sub(r"(.)\1+", r"\1", w)


class Profanity:
    def __init__(self, path):
        self.path = path
        self.reload()

    def reload(self):
        self.anywhere, self.start, self.exceptions = [], [], []
        self.prefixes = list(PREFIXES)
        try:
            lines = open(self.path, encoding="utf-8").read().splitlines()
        except OSError:
            lines = []
        for line in lines:
            line = line.strip().lower().replace("ё", "е")
            if not line or line.startswith("#"):
                continue
            kind, word = line[0], line[1:].strip()
            if kind == "^":
                self.start.append(word)
            elif kind == "!":
                self.exceptions.append(word)
            elif kind == "+":
                self.prefixes.append(word)
            else:
                self.anywhere.append(line)
        self.prefixes.sort(key=len, reverse=True)

    def _bad_form(self, w):
        if any(r in w for r in self.anywhere):
            return True
        # Слово, затем оно же без одной-двух приставок. Исключение (себе, мудрый) обрывает разбор:
        # «себе» не превращается в «с» + «ебе».
        todo = [w]
        for _ in range(3):
            nxt = []
            for s in todo:
                if any(s.startswith(e) for e in self.exceptions):
                    continue
                if any(s.startswith(r) for r in self.start):
                    return True
                nxt += [s[len(p):] for p in self.prefixes if s.startswith(p) and len(s) > len(p) + 1]
            todo = nxt
        return False

    def is_bad(self, token):
        w = KEEP.sub("", token.lower())
        if not w:
            return False
        forms = {w.translate(HOMO)}
        if LATIN.search(w):
            forms.add(_translit(w))
        forms |= {_dedupe(f) for f in forms}
        return any(self._bad_form(f) for f in forms)

    def clean(self, text, beep=BEEP):
        tokens = text.split()
        out, i = [], 0
        while i < len(tokens):
            # х у й: подряд идущие одиночные буквы проверяются вместе
            j = i
            while j < len(tokens) and len(KEEP.sub("", tokens[j].lower())) == 1:
                j += 1
            if j - i >= 2 and self.is_bad("".join(tokens[i:j])):
                out.append(beep)
                i = j
                continue
            t = tokens[i]
            if self.is_bad(t):
                m = re.search(r"[.,!?;:…)»\"']+$", t)  # знаки после слова сохраняются для интонации
                out.append(beep + (m.group(0) if m else ""))
            else:
                out.append(t)
            i += 1
        return " ".join(out)


def has_words(text):
    """Есть что произнести (писки не в счёт)."""
    return bool(re.search(r"[0-9a-zа-яё]", text, re.IGNORECASE))


def normalize_space(text):
    return re.sub(r"\s+", " ", text.replace(BEEP, "")).strip()


def default_profanity_file():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "profanity.txt")
