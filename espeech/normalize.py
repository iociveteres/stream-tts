"""Нормализатор текста чата для ESpeech: то, что модель прочесть не может или читает неверно, — словами
кириллицей. Порядок: нормализатор → RUAccent → ESpeech. Только стандартная библиотека (числительные свои: num2words —
LGPL).

Что делает:
- эмодзи, текстовые смайлы («:)», «)))», «xD», «<3», ¯\\_(ツ)_/¯), кавычки, «*», «~», «#», «@», «_», «|» — убирает;
- числа словами: количественные с падежом (по предлогу перед числом и окончанию слова после него) и родом (одна
  минута, две штуки), порядковые («1-й», «5-м», «90-х», «1st»), годы («в 2026 году», «2026 г.»), даты («19.10»,
  «05.03.1998», «1 мая»), время («18:30», «1:30:00»), счёт («2:1», «3-0»), диапазоны («2-3»), дроби («1/2», «10/10»),
  десятичные («1,5» — полтора, «2.5» — два с половиной), проценты, «5к», «2кк», деньги («500 р», «$10», «299₽»),
  единицы («80 кг», «120 км/ч», «16 ГБ»), «-5», «+25», «18+», версии («1.2.3»), коды («Код: 1234» — по цифре);
- сокращения: «т.е.», «и т.д.», «т.к.», «тыс.», «млн»; сленг чата («спс», «пж», «мб», «хз», «кд», «хп», «лвл»);
  кириллические аббревиатуры без гласных — по буквам («ФСБ» → «эф эс бэ»);
- латиница: словарь частых слов чата и брендов («gg» → «гэгэ», «Twitch» → «твич», «Arthas» → «Артес»), аббревиатуры
  капсом — английскими названиями букв («FPS» → «эф пи эс»), русский транслит («privet»; если передан lexicon — по
  словарю словоформ), остальное — правилами английского чтения (грубо);
- ники: «_», CamelCase и цифры разбиваются на слова («xXx_Killer_2007» → «Киллер две тысячи семь»);
- повторы букв сжимаются до двух («Аааааа» → «Аа»), слогов — до трёх («ахахахахах» → «ахахах»), «!!!» → «!»,
  «?!?!» → «?!», «.....» → «...»; капс (слово от 5 букв или вся фраза) — в нижний регистр;
- «+» (знак ударения для RUAccent и ESpeech) в выходе не остаётся: «плюс» или убирается.
Выход — только кириллица, пробел и « . , ! ? : ; — -»: остальное RUAccent выбрасывает сам (%, /, $, _, +, …, –),
а ESpeech вне словаря читает как пробел.

usage (библиотека): normalize(text, lexicon=None) → str; lexicon — множество слов (in), по нему транслит латиницы
  проверяется как русский (например, словарь словоформ RUAccent в SQLite).
usage (проверка): python -m espeech.normalize "текст" … — нормализованный текст по строке на аргумент.
Исследование и замеры — samples/espeech-normalize (вне git)."""
import re, sys, unicodedata

# ---------------------------------------------------------------- числительные
CASES = ("nom", "gen", "dat", "acc", "ins", "prep")
UNITS = {
    "nom": ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"],
    "gen": ["ноля", "одного", "двух", "трёх", "четырёх", "пяти", "шести", "семи", "восьми", "девяти"],
    "dat": ["нолю", "одному", "двум", "трём", "четырём", "пяти", "шести", "семи", "восьми", "девяти"],
    "ins": ["нолём", "одним", "двумя", "тремя", "четырьмя", "пятью", "шестью", "семью", "восемью", "девятью"],
    "prep": ["ноле", "одном", "двух", "трёх", "четырёх", "пяти", "шести", "семи", "восьми", "девяти"],
}
UNITS["acc"] = UNITS["nom"]
ONE = {"m": {"nom": "один", "gen": "одного", "dat": "одному", "acc": "один", "ins": "одним", "prep": "одном"},
       "f": {"nom": "одна", "gen": "одной", "dat": "одной", "acc": "одну", "ins": "одной", "prep": "одной"},
       "n": {"nom": "одно", "gen": "одного", "dat": "одному", "acc": "одно", "ins": "одним", "prep": "одном"}}
TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать",
         "семнадцать", "восемнадцать", "девятнадцать"]
TENS = {
    "nom": [None, None, "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят",
            "девяносто"],
    "gen": [None, None, "двадцати", "тридцати", "сорока", "пятидесяти", "шестидесяти", "семидесяти", "восьмидесяти",
            "девяноста"],
    "ins": [None, None, "двадцатью", "тридцатью", "сорока", "пятьюдесятью", "шестьюдесятью", "семьюдесятью",
            "восемьюдесятью", "девяноста"],
}
TENS["acc"] = TENS["nom"]
TENS["dat"] = TENS["prep"] = TENS["gen"]
HUNDREDS = {
    "nom": [None, "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"],
    "gen": [None, "ста", "двухсот", "трёхсот", "четырёхсот", "пятисот", "шестисот", "семисот", "восьмисот",
            "девятисот"],
    "dat": [None, "ста", "двумстам", "трёмстам", "четырёмстам", "пятистам", "шестистам", "семистам", "восьмистам",
            "девятистам"],
    "ins": [None, "ста", "двумястами", "тремястами", "четырьмястами", "пятьюстами", "шестьюстами", "семьюстами",
            "восемьюстами", "девятьюстами"],
    "prep": [None, "ста", "двухстах", "трёхстах", "четырёхстах", "пятистах", "шестистах", "семистах", "восьмистах",
             "девятистах"],
}
HUNDREDS["acc"] = HUNDREDS["nom"]
# существительные: падеж → (ед., мн.); после 2–4 в им./вин. — род. ед., после 5+ — род. мн.
NOUNS = {
    "тысяча": ("f", {"nom": ("тысяча", "тысячи"), "gen": ("тысячи", "тысяч"), "dat": ("тысяче", "тысячам"),
                     "acc": ("тысячу", "тысячи"), "ins": ("тысячей", "тысячами"), "prep": ("тысяче", "тысячах")}),
}


def _noun_m(stem, soft=False):
    """Мужской род на согласный (миллион, процент) или на -ь (рубль)."""
    if soft:
        b = stem
        return {"nom": (b + "ь", b + "и"), "gen": (b + "я", b + "ей"), "dat": (b + "ю", b + "ям"),
                "acc": (b + "ь", b + "и"), "ins": (b + "ем", b + "ями"), "prep": (b + "е", b + "ях")}
    return {"nom": (stem, stem + "ы"), "gen": (stem + "а", stem + "ов"), "dat": (stem + "у", stem + "ам"),
            "acc": (stem, stem + "ы"), "ins": (stem + "ом", stem + "ами"), "prep": (stem + "е", stem + "ах")}


def _noun_f(stem, soft=False):
    """Женский род на -а (минута, копейка: soft — на -я/-йка не нужен, род. мн. задаётся отдельно)."""
    return {"nom": (stem + "а", stem + "ы"), "gen": (stem + "ы", stem), "dat": (stem + "е", stem + "ам"),
            "acc": (stem + "у", stem + "ы"), "ins": (stem + "ой", stem + "ами"), "prep": (stem + "е", stem + "ах")}


for w in ("миллион", "миллиард", "процент", "доллар", "килограмм", "грамм", "километр", "метр", "сантиметр",
          "миллиметр", "гигабайт", "мегабайт", "терабайт", "килобайт", "мегабит", "гигабит", "градус", "час", "раз",
          "кадр", "герц", "килогерц", "мегагерц", "гигагерц"):
    NOUNS[w] = ("m", _noun_m(w))
NOUNS["рубль"] = ("m", _noun_m("рубл", soft=True))
for w in ("минут", "секунд", "штук", "миллисекунд"):
    NOUNS[w + "а"] = ("f", _noun_f(w))
NOUNS["копейка"] = ("f", {"nom": ("копейка", "копейки"), "gen": ("копейки", "копеек"), "dat": ("копейке", "копейкам"),
                          "acc": ("копейку", "копейки"), "ins": ("копейкой", "копейками"),
                          "prep": ("копейке", "копейках")})
NOUNS["евро"] = ("n", {c: ("евро", "евро") for c in CASES})
NOUNS["час"][1]["gen"] = ("часа", "часов")
NOUNS["раз"][1]["gen"] = ("раза", "раз")
NOUNS["раз"][1]["nom"] = ("раз", "разы")
NOUNS["килограмм"][1]["gen"] = ("килограмма", "килограммов")
for w in ("гигабайт", "мегабайт", "терабайт", "килобайт", "мегабит", "гигабит", "герц", "килогерц", "мегагерц",
          "гигагерц"):
    NOUNS[w][1]["gen"] = (w + "а", w)   # счётная форма: «шестнадцать гигабайт»


def plural_kind(n):
    """1 — «одна минута», 2 — «две минуты», 5 — «пять минут» (по последним цифрам)."""
    n = abs(n)
    if 11 <= n % 100 <= 14:
        return 5
    return {1: 1, 2: 2, 3: 2, 4: 2}.get(n % 10, 5)


def noun_form(word, n, case="nom", frac=False):
    gender, forms = NOUNS[word]
    if frac:
        return forms["gen"][0]
    k = plural_kind(n)
    if case in ("nom", "acc"):
        if k == 1:
            return forms[case][0]
        return forms["gen"][0] if k == 2 else forms["gen"][1]
    return forms[case][0] if k == 1 else forms[case][1]


def _triad(n, case, gender):
    """0 < n < 1000."""
    out = []
    h, r = divmod(n, 100)
    if h:
        out.append(HUNDREDS[case][h])
    if 10 <= r < 20:
        w = TEENS[r - 10]
        if case in ("gen", "dat", "prep"):
            w = w[:-1] + "и"
        elif case == "ins":
            w = w[:-1] + "ью"
        out.append(w)
    else:
        t, u = divmod(r, 10)
        if t:
            out.append(TENS[case][t])
        if u == 1:
            out.append(ONE[gender][case])
        elif u == 2 and gender == "f" and case in ("nom", "acc"):
            out.append("две")
        elif u:
            out.append(UNITS[case][u])
    return out


SCALES = [(10 ** 9, "миллиард"), (10 ** 6, "миллион"), (1000, "тысяча")]


def cardinal(n, case="nom", gender="m"):
    """Количественное числительное словами: падеж nom/gen/dat/acc/ins/prep, род m/f/n."""
    if n < 0:
        return "минус " + cardinal(-n, case, gender)
    if n == 0:
        return UNITS[case][0]
    out = []
    for size, word in SCALES:
        k, n = divmod(n, size)
        if not k:
            continue
        g = NOUNS[word][0]
        if k == 1 and case in ("nom", "acc") and word == "тысяча":
            out.append(noun_form(word, 1, case))   # «тысяча», а не «одна тысяча»
            continue
        out += _triad(k, case, g) if k < 1000 else cardinal(k, case, g).split()
        out.append(noun_form(word, k, case))
    if n:
        out += _triad(n, case, gender)
    return " ".join(out)


ORD_UNITS = [None, "перв", "втор", "трет", "четвёрт", "пят", "шест", "седьм", "восьм", "девят"]
ORD_TEENS = ["десят", "одиннадцат", "двенадцат", "тринадцат", "четырнадцат", "пятнадцат", "шестнадцат",
             "семнадцат", "восемнадцат", "девятнадцат"]
ORD_TENS = [None, None, "двадцат", "тридцат", "сороков", "пятидесят", "шестидесят", "семидесят", "восьмидесят",
            "девяност"]
ORD_HUNDREDS = [None, "сот", "двухсот", "трёхсот", "четырёхсот", "пятисот", "шестисот", "семисот", "восьмисот",
                "девятисот"]
STRESSED_END = {"втор", "шест", "седьм", "восьм", "сороков"}   # «-ой», а не «-ый»
ADJ = {  # окончания порядковых: род/число → падеж
    "m": {"nom": "ый", "gen": "ого", "dat": "ому", "acc": "ый", "ins": "ым", "prep": "ом"},
    "f": {"nom": "ая", "gen": "ой", "dat": "ой", "acc": "ую", "ins": "ой", "prep": "ой"},
    "n": {"nom": "ое", "gen": "ого", "dat": "ому", "acc": "ое", "ins": "ым", "prep": "ом"},
    "pl": {"nom": "ые", "gen": "ых", "dat": "ым", "acc": "ые", "ins": "ыми", "prep": "ых"},
}
THIRD = {
    "m": {"nom": "третий", "gen": "третьего", "dat": "третьему", "acc": "третий", "ins": "третьим", "prep": "третьем"},
    "f": {"nom": "третья", "gen": "третьей", "dat": "третьей", "acc": "третью", "ins": "третьей", "prep": "третьей"},
    "n": {"nom": "третье", "gen": "третьего", "dat": "третьему", "acc": "третье", "ins": "третьим",
          "prep": "третьем"},
    "pl": {"nom": "третьи", "gen": "третьих", "dat": "третьим", "acc": "третьи", "ins": "третьими",
           "prep": "третьих"},
}
GEN_PREFIX = [None, "", "двух", "трёх", "четырёх", "пяти", "шести", "семи", "восьми", "девяти"]   # двухтысячный


def ordinal(n, case="nom", gender="m"):
    """Порядковое: «две тысячи двадцать шестом» — всё, кроме последнего слова, в им. падеже."""
    if n == 0:
        return "нулев" + ("ой" if gender == "m" and case in ("nom", "acc") else ADJ[gender][case])
    if n % 1000 == 0:
        k = n // 1000
        if k % 1000 == 0 or k > 9:
            stem, rest = ("миллионн", n - 10 ** 6) if n == 10 ** 6 else (None, None)
            if stem is None:
                return cardinal(n, case, "m")
        else:
            stem, rest = GEN_PREFIX[k] + "тысячн", 0
    else:
        r = n % 100
        if 10 <= r < 20:
            stem, rest = ORD_TEENS[r - 10], n - r
        elif r % 10:
            stem, rest = ORD_UNITS[r % 10], n - r % 10
        elif r:
            stem, rest = ORD_TENS[r // 10], n - r
        else:
            stem, rest = ORD_HUNDREDS[(n % 1000) // 100], n - n % 1000
    prefix = cardinal(rest) + " " if rest else ""
    if stem == "трет":
        return prefix + THIRD[gender][case]
    end = ADJ[gender][case]
    if gender == "m" and case in ("nom", "acc") and stem in STRESSED_END:
        end = "ой"
    return prefix + stem + end


def digits(s):
    return " ".join(UNITS["nom"][int(c)] for c in s if c.isdigit())





def fraction_word(num, den):
    """«одна вторая», «три четверти», «пять восьмых»."""
    if (num, den) == (3, 4):
        return "три четверти"
    if (num, den) == (1, 4):
        return "одна четверть"
    k = plural_kind(num)
    head = cardinal(num, "nom", "f")
    if k == 1:
        return head + " " + ordinal(den, "nom", "f")
    return head + " " + ordinal(den, "gen", "pl")


def decimal_words(whole, frac, case="nom", gender="m"):
    """«1,5» → «полтора» (с родом), «2,5» → «два с половиной», иначе «X целых Y десятых»."""
    if frac == "5" and case in ("nom", "acc"):
        if whole == 1:
            return "полторы" if gender == "f" else "полтора"
        if whole:
            return cardinal(whole, "nom", gender) + " с половиной"
    w = int(whole)
    head = cardinal(w, case, "f") + " " + ("целая" if plural_kind(w) == 1 and w % 100 != 11 else "целых")
    f = int(frac)
    name = {1: "десят", 2: "сот", 3: "тысячн"}.get(len(frac))
    if not name:
        return head + " " + digits(frac)
    tail = cardinal(f, case, "f") + " " + name + ("ая" if plural_kind(f) == 1 and f % 100 != 11 else "ых")
    return head + " " + tail


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября",
          "декабря"]
MONTH_RE = "|".join(MONTHS)
DATE_PREP = {"по": "acc"}   # «по пятое мая»; без предлога дата — род. падеж («стрим будет девятнадцатого октября»)

# предлог перед числом → падеж (если слово после числа падеж не выдаёт)
PREP_CASE = {"до": "gen", "от": "gen", "из": "gen", "около": "gen", "после": "gen", "без": "gen", "для": "gen",
             "у": "gen", "кроме": "gen", "больше": "gen", "меньше": "gen", "более": "gen", "менее": "gen",
             "свыше": "gen", "против": "gen", "среди": "gen", "с": "gen", "со": "gen",
             "к": "dat", "ко": "dat",
             "над": "ins", "под": "ins", "между": "ins", "перед": "ins",
             "о": "prep", "об": "prep", "при": "prep",
             "в": "acc", "во": "acc", "на": "acc", "за": "acc", "через": "acc", "про": "acc"}
# слова после числа, по которым понятен род (кроме окончаний: -а/-я → ж, -о/-е → ср)
FEM = set("минута минуты минуту секунда секунды секунду неделя недели неделю жизнь жизни штука штуки штуку тысяча тысячи "
          "игра игры игру катка катки катку попытка попытки попытку победа победы победу смерть смерти ночь ночи "
          "копейка копейки копейку карта карты карту серия серии серию волна волны волну".split())
MASC_SOFT = set("рубль рубля день дня уровень уровня гость гостя зритель зрителя".split())
# слова, перед которыми число — порядковое, если количественное не согласуется («11 уровень», «2 место»)
_M = {"": "nom", "а": "gen", "у": "dat", "ом": "ins", "е": "prep"}
ORD_STEMS = {"уров": ("m", {"ень": "nom", "ня": "gen", "ню": "dat", "нем": "ins", "не": "prep"}),
             "мест": ("n", {"о": "nom", "а": "gen", "у": "dat", "ом": "ins", "е": "prep"}),
             "лвл": ("m", {"": "nom"}), "сезон": ("m", _M), "раунд": ("m", _M), "этап": ("m", _M),
             "этаж": ("m", {"": "nom", "а": "gen", "у": "dat", "ом": "ins", "е": "prep"}), "класс": ("m", _M),
             "курс": ("m", _M), "акт": ("m", _M), "ранг": ("m", _M)}


def word_at(s, i, direction):
    """Соседнее слово (без знаков) слева (direction=-1, i — начало числа) или справа (i — конец)."""
    if direction < 0:
        m = re.search(r"([^\W\d_]+)[^\w]*$", s[:i])
        return m.group(1).lower() if m and not re.search(r"[.!?]\s*$", s[m.end(1):i]) else ""
    m = re.match(r"\s*([^\W\d_]+)", s[i:])
    return m.group(1).lower() if m else ""


def case_of(prev, nxt):
    """Падеж числа: по окончанию слова после него, иначе по предлогу."""
    if nxt:
        if re.search(r"(ами|ями)$", nxt):
            return "ins"
        if re.search(r"[^ст](ах|ях)$", nxt):
            return "prep"
        if re.search(r"(ам|ям)$", nxt) and nxt not in ("грамм", "килограмм"):
            return "dat"
    c = PREP_CASE.get(prev, "nom")
    if prev in ("с", "со") and nxt and re.search(r"(ом|ем|ой|ей|ою)$", nxt):
        return "ins"
    return c


def gender_of_for(n, nxt, case="nom"):
    """Род нужен только для 1 и 2: «одна минута», «две минуты», «одно зелье»; в косвенных падежах — по окончанию
    падежа («без одной минуты», «без одного голоса»)."""
    if not nxt:
        return "m"
    if nxt in FEM:
        return "f"
    if nxt in MASC_SOFT:
        return "m"
    if case != "nom" and case != "acc":
        fem = {"gen": r"[ыи]$", "dat": r"[^у]е$", "ins": r"(ой|ей|ью)$", "prep": r"^$"}[case]
        return "f" if re.search(fem, nxt) else "m"
    k = plural_kind(n)
    if k == 1:
        if re.search(r"[ая]$", nxt):
            return "f"
        if re.search(r"[оеё]$", nxt) and nxt != "евро":
            return "n"
        return "m"
    if k == 2 and re.search(r"[ыи]$", nxt):
        return "f"
    return "m"


# ---------------------------------------------------------------- словари
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
# целые токены (до разбиения)
SPECIAL = {"c++": "си плюс плюс", "c#": "си шарп", "5head": "файвхед", "cs:go": "кс го", "1v1": "один на один",
           "2v2": "два на два", "5v5": "пять на пять", "18+": "восемнадцать плюс", "wc3": "варкрафт три",
           "p.s.": "пэ эс", "ps": "пэ эс"}
# кириллица: сокращения и сленг (нижний регистр, без точек) → чтение
RU_WORDS = {
    "спс": "спасибо", "пж": "пожалуйста", "пжл": "пожалуйста", "пжлст": "пожалуйста", "плз": "плиз",
    "мб": "может быть", "хз": "хэзэ", "кд": "кэдэ", "хп": "хэпэ", "лвл": "левел", "оч": "очень", "кст": "кстати",
    "пк": "пэка", "сша": "сэ шэ а", "озу": "озу", "гб": "гигабайт", "мб.": "мегабайт", "тыс": "тысяч",
    "млн": "миллионов", "млрд": "миллиардов", "др": "другие", "тг": "тэгэ", "вк": "вэка", "дс": "дискорд",
    "лс": "личку", "имхо": "имхо", "чсв": "чэ эс вэ", "нзч": "не за что", "спок": "спокойной ночи",
    "кс": "кс", "гг": "гэгэ", "вп": "вэпэ", "лол": "лол", "кек": "кек",
}
ABBR = [  # с точками, по порядку
    (r"\bи\s*т\.\s*д\.", "и так далее"), (r"\bи\s*т\.\s*п\.", "и тому подобное"), (r"\bт\.\s*е\.", "то есть"),
    (r"\bт\.\s*к\.", "так как"), (r"\bт\.\s*н\.", "так называемый"), (r"\bт\.\s*д\.", "так далее"),
    (r"\bи\s*др\.", "и другие"), (r"\bнапр\.", "например"), (r"\bp\.\s*s\.", "пэ эс"), (r"\bт\.\s*ч\.", "том числе"),
]
# единицы после числа: запись → (существительное, «/ч»-хвост)
UNITS_AFTER = {
    "кг": "килограмм", "г": "грамм", "км": "километр", "м": "метр", "см": "сантиметр", "мм": "миллиметр",
    "гб": "гигабайт", "мб": "мегабайт", "тб": "терабайт", "кб": "килобайт", "мбит": "мегабит", "гбит": "гигабит",
    "гц": "герц", "кгц": "килогерц", "мгц": "мегагерц", "ггц": "гигагерц",
    "ч": "час", "час": None, "мин": "минута", "сек": "секунда", "мс": "миллисекунда", "шт": "штука",
    "тыс": "тысяча", "млн": "миллион", "млрд": "миллиард", "к": "тысяча", "k": "тысяча", "кк": "миллион",
    "kk": "миллион", "р": "рубль", "руб": "рубль", "₽": "рубль", "$": "доллар", "€": "евро", "%": "процент",
    "kg": "килограмм", "km": "километр", "gb": "гигабайт", "mb": "мегабайт", "ms": "миллисекунда", "°": "градус",
    "usd": "доллар", "eur": "евро", "rub": "рубль",
}
UNITS_AFTER = {k: v for k, v in UNITS_AFTER.items() if v}
PER = {"ч": "в час", "с": "в секунду", "сек": "в секунду", "мин": "в минуту"}
GLUED = {"к", "k", "кк", "kk", "г"}   # только слитно с числом: «1 к удаче» — не тысяча
UNIT_RE = "|".join(sorted((re.escape(k) for k in UNITS_AFTER if k not in GLUED), key=len, reverse=True))
GLUED_RE = "|".join(sorted((re.escape(k) for k in GLUED), key=len, reverse=True))

# названия букв для аббревиатур
RU_LETTERS = dict(zip("бвгджзклмнпрстфхцчшщ", ["бэ", "вэ", "гэ", "дэ", "жэ", "зэ", "ка", "эл", "эм", "эн", "пэ", "эр",
                                                "эс", "тэ", "эф", "ха", "цэ", "чэ", "ша", "ща"]))
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


# ---------------------------------------------------------------- очистка
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿←-⇿️︎‍⃣"
    "\U000E0020-\U000E007F〰〽㊗㊙]+")
SMILEY = re.compile(
    r"¯\\_\(ツ\)_/¯|\(ツ\)|"
    r"(?<!\w)(?:[:;=8][-'^o]?[)(\]\[DPpОоOЗз3*|/\\]+|[xXхХ][DdДд]+|<3+|\^_*\^|[TТ]_[TТ]|-_+-|o_O|O_o)(?!\w)|"
    r"[)(]{2,}|(?<=\w)\)+(?=\s|$)|(?<=\s)\)+(?=\s|$)")
KEEP = re.compile(r"[^а-яёА-ЯЁ0-9 .,!?:;—\-]")


def normalize(text, lexicon=None):
    s = unicodedata.normalize("NFC", text)
    s = s.replace("…", "...").replace("–", "—").replace("―", "—").replace("−", "-").replace(" ", " ")
    s = EMOJI.sub(" ", s)
    s = SMILEY.sub(" ", s)
    s = re.sub(r"[«»„“”\"`*~_|#^\[\]{}]", lambda m: " " if m.group(0) in "_|" else "", s)  # «_» — граница слов в никах
    s = re.sub(r"@(\w)", r"\1", s)
    # целые токены из словаря (до разбиения ников и чисел)
    s = re.sub(r"(?<![\w+#])(c\+\+|c#|cs:go|\d+head|\d+v\d+|18\+|wc3|p\.s\.)(?![\w+])",
               lambda m: SPECIAL.get(m.group(1).lower(), _vs(m.group(1))), s, flags=re.I)
    for rx, rep in ABBR:
        s = re.sub(rx, rep, s, flags=re.I)
    # ники: части из одних x («xXx») убираются, CamelCase и буквы+цифры разбиваются
    s = re.sub(r"(?<![^\s])[xX]{2,}(?=\s)|(?<=\s)[xX]{2,}(?![^\s])", " ", " " + s + " ").strip()
    s = re.sub(r"[A-Za-zА-Яа-яЁё]+", lambda m: split_camel(m.group(0)) if not _is_lex_word(m.group(0)) else m.group(0), s)
    s = re.sub(r"(?<=[A-Za-zА-Яа-яЁё]{2})(?=\d)", " ", s)        # cs2, топ1, Pro100, Killer2007
    s = re.sub(r"\b\d{1,3}(?:[ \u00a0]\d{3})+\b", lambda m: re.sub(r"\s", "", m.group(0)), s)   # 1 000 000
    s = re.sub(r"(?<![\w])[xхXХ](?=\d+\b)", "икс ", s)            # x2, x10
    s = numbers(s)
    s = latin(s, lexicon)
    s = cyrillic(s, lexicon)
    s = symbols(s)
    s = KEEP.sub(" ", s)
    s = punct(s)
    return s


def _vs(t):
    m = re.fullmatch(r"(\d+)v(\d+)", t, re.I)
    return f"{cardinal(int(m.group(1)))} на {cardinal(int(m.group(2)))}" if m else t


def _is_lex_word(w):
    return w.lower() in LATIN_WORDS


# ---------------------------------------------------------------- числа
NUM = r"\d+(?:[.,]\d+)?"


def _num_val(t):
    t = t.replace(",", ".")
    return float(t) if "." in t else int(t)


def say_number(tok, prev, nxt, gender=None, case=None):
    """Число из текста (целое или десятичное) с учётом соседних слов."""
    c = case or case_of(prev, nxt)
    if re.fullmatch(r"\d+[.,]\d+", tok):
        whole, frac = re.split(r"[.,]", tok)
        g = gender or gender_of_for(2, nxt, c)
        return decimal_words(int(whole), frac, c, g)
    if len(tok) > 1 and tok.startswith("0") or len(tok) > 12:
        return digits(tok)
    n = int(tok)
    if prev in ("код", "пин", "пароль", "номер", "телефон", "id", "айди"):
        return digits(tok)
    return cardinal(n, c, gender or gender_of_for(n, nxt, c))


def unit_phrase(val_tok, unit, prev, nxt_after, per=None):
    """«80 кг» → «восемьдесят килограммов»; «1.5к» → «полторы тысячи»; падеж — по предлогу."""
    noun = UNITS_AFTER[unit.lower()] if unit.lower() in UNITS_AFTER else UNITS_AFTER[unit]
    gender = NOUNS[noun][0]
    case = PREP_CASE.get(prev, "nom")
    if case == "acc" and prev in ("в", "во") and noun not in ("процент", "раз"):
        case = "acc"
    frac = bool(re.fullmatch(r"\d+[.,]\d+", val_tok))
    if frac:
        whole, f = re.split(r"[.,]", val_tok)
        num = decimal_words(int(whole), f, case, gender)
        form = noun_form(noun, 2, case) if f == "5" and case in ("nom", "acc") else NOUNS[noun][1]["gen"][0]
    else:
        n = int(val_tok)
        num = cardinal(n, case, gender)
        form = noun_form(noun, n, case)
        if n == 1 and case in ("nom", "acc") and noun in ("тысяча", "миллион", "миллиард"):
            num = "" if noun == "тысяча" else "один"   # «1к» — «тысяча»
    out = (num + " " + form).strip()
    if per:
        out += " " + PER.get(per.lower(), "в " + per)
    return out


def numbers(s):
    # время 1:30:00 → «один час тридцать минут»
    def hms(m):
        h, mi, se = int(m.group(1)), int(m.group(2)), int(m.group(3))
        parts = [(h, "час"), (mi, "минута"), (se, "секунда")]
        return " ".join(f"{cardinal(v, 'nom', NOUNS[w][0])} {noun_form(w, v)}" for v, w in parts if v)
    s = re.sub(r"\b(\d{1,2}):(\d{2}):(\d{2})\b", hms, s)

    # время 18:30 / счёт 2:1
    def hm(m):
        prev = word_at(s, m.start(), -1)
        h, mi = m.group(1), m.group(2)
        if len(mi) == 2 and int(h) < 24 and int(mi) < 60:
            c = PREP_CASE.get(prev, "nom")
            c = "nom" if c == "acc" else c
            hw = cardinal(int(h), c)
            mw = "ноль ноль" if mi == "00" else ("ноль " + cardinal(int(mi[1]), c)) if mi[0] == "0" else cardinal(
                int(mi), c)
            return hw + " " + mw
        return cardinal(int(h)) + " " + cardinal(int(mi))
    s = re.sub(r"\b(\d{1,2}):(\d{1,2})\b", hm, s)

    # даты 05.03.1998, 19.10
    def date(m):
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        prev = word_at(s, m.start(), -1)
        nxt = word_at(s, m.end(), 1)
        if not y and d < 24 and mo < 60 and (prev in ("в", "к", "до", "с", "после", "около") and not 1 <= mo <= 12
                                              or nxt in ("утра", "вечера", "дня", "ночи")):
            return hm(m)   # «в 18.30» — время
        if not (1 <= d <= 31 and 1 <= mo <= 12):
            return m.group(0)
        c = "nom" if prev in ("сегодня", "завтра", "вчера", "это") else PREP_CASE.get(prev, DATE_PREP.get(prev, "gen"))
        out = ordinal(d, c, "n") + " " + MONTHS[mo - 1]
        if y:
            yy = int(y) if len(y) == 4 else 2000 + int(y)
            out += " " + ordinal(yy, "gen") + " года"
        return out
    s = re.sub(r"\b(\d{1,2})\.(\d{2})(?:\.(\d{4}|\d{2}))?\b(?![.,]\d)", date, s)

    # версии 1.2.3 (после дат: 05.03.1998 — дата)
    s = re.sub(r"\b\d+(?:\.\d+){2,}\b", lambda m: " точка ".join(cardinal(int(x)) for x in m.group(0).split(".")), s)

    # день + месяц словом: «1 мая», «19 октября 2026 г.»
    def day_month(m):
        prev = word_at(s, m.start(), -1)
        c = "nom" if prev in ("сегодня", "завтра", "вчера", "это") else PREP_CASE.get(prev, DATE_PREP.get(prev, "gen"))
        out = ordinal(int(m.group(1)), c, "n") + " " + m.group(2)
        if m.group(3):
            out += " " + ordinal(int(m.group(3)), "gen") + " года"
        return out
    s = re.sub(rf"\b(\d{{1,2}})\s+({MONTH_RE})(?:\s+(\d{{4}})(?:\s*(?:г\.|года?\b))?)?", day_month, s, flags=re.I)

    # годы: «в 2026 году», «2026 г.», «с 2010 года»
    def year(m):
        prev = word_at(s, m.start(), -1)
        n = int(m.group(1))
        w = m.group(2).lower()
        if w.startswith("г."):
            c = "prep" if prev in ("в", "во") else "gen"
            return ordinal(n, c) + " " + {"prep": "году", "gen": "года"}[c] + m.group(2)[2:]
        c = {"год": "nom", "года": "gen", "году": "prep" if prev in ("в", "во") else "dat", "годом": "ins",
             "годов": "gen"}.get(w, "nom")
        return ordinal(n, c) + " " + m.group(2)
    s = re.sub(r"\b(\d{1,4})\s*(года|году|годом|год|г\.)(?![а-я])", year, s, flags=re.I)

    # порядковые с окончанием: 1-й, 2-е, 5-м, 90-х, 1st
    ENDS = {"й": ("m", "nom"), "ий": ("m", "nom"), "ый": ("m", "nom"), "ой": ("f", "gen"), "го": ("m", "gen"),
            "ого": ("m", "gen"), "му": ("m", "dat"), "ому": ("m", "dat"), "м": ("m", "prep"), "ом": ("m", "prep"),
            "ым": ("m", "ins"), "я": ("f", "nom"), "ая": ("f", "nom"), "ю": ("f", "acc"), "ую": ("f", "acc"),
            "е": ("n", "nom"), "ое": ("n", "nom"), "х": ("pl", "prep"), "ых": ("pl", "prep"), "ые": ("pl", "nom"),
            "ми": ("pl", "ins")}

    def ord_suffix(m):
        n, end = int(m.group(1)), m.group(2).lower()
        g, c = ENDS[end]
        prev = word_at(s, m.start(), -1)
        if end in ("м", "ом") and prev not in ("в", "во", "на", "о", "об", "при"):
            c = "ins"
        if end in ("й", "ий", "ый", "ой") and m.group(2).lower() == "ой" and prev not in PREP_CASE:
            g, c = "m", "nom"
        if end == "е" and n % 10 == 0 and n >= 20 and prev in ("в", "во"):
            g, c = "pl", "acc"
        return ordinal(n, c, g)
    s = re.sub(r"\b(\d+)\s?-\s?(ого|ому|ий|ый|ой|ая|ую|ое|ых|ые|ым|ом|ми|го|му|й|м|я|ю|е|х)\b", ord_suffix, s,
               flags=re.I)
    s = re.sub(r"\b(\d+)(?:st|nd|rd|th)\b", lambda m: ordinal(int(m.group(1))), s, flags=re.I)

    # дроби и оценки: 1/2, 3/4, 10/10
    def frac(m):
        a, b = int(m.group(1)), int(m.group(2))
        if b in (5, 10, 100) and a <= b:
            return cardinal(a) + " из " + cardinal(b, "gen")
        if 2 <= b <= 10 and a < b:
            return fraction_word(a, b)
        return cardinal(a) + " " + cardinal(b)
    s = re.sub(r"(?<![\d/])(\d{1,3})/(\d{1,3})(?![\d/])", frac, s)

    # счёт и диапазоны: 3-0, 2-3
    def dash(m):
        a, b = int(m.group(1)), int(m.group(2))
        nxt = word_at(s, m.end(), 1)
        if b == 0 or a > b or not nxt:
            return cardinal(a) + " " + cardinal(b)
        prev = word_at(s, m.start(), -1)
        return say_number(m.group(1), prev, nxt) + "-" + say_number(m.group(2), prev, nxt)
    s = re.sub(r"(?<![\d-])(\d+)\s?[-—]\s?(\d+)(?![\d-])", dash, s)

    # знаки: -5, +25, 18+
    s = re.sub(r"(?<![\w])-(?=\d)", "минус ", s)
    s = re.sub(r"(?<![\w])\+(?=\d)", "плюс ", s)
    s = re.sub(r"(?<=\d)\+", " плюс ", s)

    # валюта перед числом: $10, €15
    def cur_before(m):
        return unit_phrase(m.group(2), m.group(1), word_at(s, m.start(), -1), "")
    s = re.sub(rf"([$€₽])\s?({NUM})", cur_before, s)

    # число + единица: 5к, 10k, 2кк, 80 кг, 120 км/ч, 100%, 500 р, 1000 руб.
    def with_unit(m):
        prev = word_at(s, m.start(), -1)
        return unit_phrase(m.group(1), m.group(2) or m.group(3), prev, "", m.group(4))
    s = re.sub(rf"({NUM})(?:\s?({UNIT_RE})|({GLUED_RE}))(?:/(ч|с|сек|мин))?\.?(?![\wа-яё])", with_unit, s,
               flags=re.I)

    # модели железа: RTX 3060 → тридцать шестьдесят
    s = re.sub(r"\b([A-Z]{2,4})\s(\d)(\d)(\d)(\d)\b",
               lambda m: m.group(1) + " " + cardinal(int(m.group(2) + m.group(3))) + " " + cardinal(
                   int(m.group(4) + m.group(5))) if m.group(2) != "0" and m.group(4) != "0" else m.group(0), s)

    # число + слово-порядковое: «11 уровень» → «одиннадцатый уровень»
    def ord_noun(m):
        n, w = int(m.group(1)), m.group(2)
        low = w.lower()
        stem = next(k for k in ORD_STEMS if low.startswith(k))
        g, forms = ORD_STEMS[stem]
        c = forms.get(low[len(stem):])
        k = plural_kind(n)
        # количественное согласуется: «1 уровень», «2 уровня»; иначе — порядковое («11 уровень», «на 3 месте»)
        if c is None or (c == "nom" and k == 1 and n % 100 != 11) or (c == "gen" and k == 2):
            return m.group(0)
        if c == "gen" and k == 5 and word_at(s, m.start(), -1) not in PREP_CASE:
            return m.group(0)   # «5 уровней»
        return ordinal(n, c, g) + " " + w
    s = re.sub(r"\b(\d+)\s+((?:" + "|".join(ORD_STEMS) + r")[а-я]*)", ord_noun, s, flags=re.I)

    # остальные числа
    def plain(m):
        tok = m.group(0)
        return say_number(tok, word_at(s, m.start(), -1), word_at(s, m.end(), 1))
    s = re.sub(rf"(?<![\d.,]){NUM}(?![\d])", plain, s)
    # тысячи через пробел уже разобраны как отдельные числа — редкость в чате
    return s


# ---------------------------------------------------------------- латиница, кириллица, знаки
def latin(s, lexicon):
    return re.sub(r"[A-Za-z]+(?:'[a-z]+)?", lambda m: latin_word(m.group(0), lexicon), s)


def cyrillic(s, lexicon=None):
    words = re.findall(r"[А-Яа-яЁё]+", s)
    letters = [c for w in words for c in w]
    all_caps = len(letters) >= 4 and sum(c.isupper() for c in letters) / len(letters) > 0.7

    def word(m):
        w = m.group(0)
        low = w.lower()
        if low in RU_WORDS:
            return RU_WORDS[low]
        if w.isupper() and not all_caps and not (set(low) & RU_VOWELS) and 2 <= len(low) <= 5:
            return " ".join(RU_LETTERS.get(c, c) for c in low)    # ФСБ, РФ; «Хм», «Мм» — междометия, не трогать
        # повторы: «ахахахах» → «ахахах», «аааа» → «аа»; со словарём — «пожааалуйста» → «пожалуйста»
        low2 = re.sub(r"(\w\w)\1{3,}", r"\1\1\1", low)
        low2 = re.sub(r"(\w)\1{2,}", r"\1\1", low2)
        if low2 != low:
            one = re.sub(r"(\w)\1+", r"\1", low)
            if lexicon is not None and len(one) >= 5 and one in lexicon:   # «Дааа» — протяжно, не «Да»
                low2 = one
            w, low = (low2.capitalize() if w[0].isupper() else low2), low2
        if all_caps or (w.isupper() and len(w) >= 5) or (re.search(r"[а-яё][А-ЯЁ]", w) and len(w) > 3):
            w = low
        return w.replace("Ъ", "ъ")   # заглавного Ъ в словаре ESpeech нет
    return re.sub(r"[А-Яа-яЁё]+", word, s)


def symbols(s):
    s = re.sub(r"\s*\+\s*", " плюс ", s)
    s = re.sub(r"\s*=\s*", " равно ", s)
    s = re.sub(r"\s*&\s*", " и ", s)
    s = re.sub(r"\s+>\s+", " больше ", s)
    s = re.sub(r"\s+<\s+", " меньше ", s)
    s = re.sub(r"№\s*", "номер ", s)
    s = s.replace("%", " процентов").replace("°", " градусов").replace("/", " ").replace("\\", " ")
    return s


def punct(s):
    s = re.sub(r"\s+-\s+", " — ", s)
    s = re.sub(r"([!?])[!?]*(?:[!?])", lambda m: "?!" if "?" in m.group(0) and "!" in m.group(0) else m.group(1), s)
    s = re.sub(r"\.{4,}", "...", s)
    s = re.sub(r"\s+([.,!?:;])", r"\1", s)
    s = re.sub(r"([.,!?:;])(?=[^\s.,!?:;])", r"\1 ", s)
    s = re.sub(r"(^|[\s])[.,!?:;]+(?=\s|$)", r"\1", s) if re.search(r"[а-яё]", s, re.I) else ""
    s = re.sub(r"\s+", " ", s).strip(" ,;:—-")
    s = re.sub(r"^[.!?]+\s*", "", s)
    if s and s[0].islower():
        s = s[0].upper() + s[1:]
    return s


def normalize_nick(nick, lexicon=None):
    """Ник отдельно: то же самое, но без завершающих знаков."""
    return normalize(nick, lexicon).rstrip(".!?")


def main():
    for text in sys.argv[1:]:
        print(normalize(text))


if __name__ == "__main__":
    main()
