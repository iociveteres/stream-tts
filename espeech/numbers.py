"""Числа в тексте чата словами (numbers): даты, время, счёт, диапазоны, дроби, деньги, единицы, проценты, версии и
коды; падеж — по предлогу перед числом и окончанию слова после него, род — по слову после числа. Сами
числительные — numerals.py."""
import re

from .numerals import (NOUNS, cardinal, decimal_words, digits, fraction_word, noun_form, ordinal,
                       plural_kind)

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
