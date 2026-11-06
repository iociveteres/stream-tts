"""Числительные словами для нормализатора ESpeech (normalize.py): количественные с падежом и родом, порядковые,
дроби и десятичные; существительные при числе в нужной форме (noun_form). Только стандартная библиотека
(num2words — LGPL)."""

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
