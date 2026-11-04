"""Проверка английского словаря stream-tts (streamtts/static/en.json, механизм — streamtts/i18n.py).

Ищет строки, которым нужен перевод: tr("…") в streamtts/**/*.py, T("…") в static/*.js и overlay.html, {{t:…}} в
static/voices.html, плюс константы модулей, которые переводятся при показе: причины возврата R_*, ответы зрителю REPLIES и
ACCESS_WHO, подсказки Helix HELIX_HINTS, группы голосов GROUPS, подписи примеров SAMPLES, названия и описания пакетов
PACKAGES. Константы ищутся по имени во всех модулях пакета, а не в конкретном файле: перенос в другой модуль проверку
не выключает. Если известная константа не найдена нигде (переименована или задана не так, как ждёт разбор), это
ошибка: иначе её строки молча выпали бы из проверки. Печатает ключи без перевода, переводы с кириллицей и
расхождения подстановок {имя}. Код выхода 1, если что-то нашлось.

Текст из разметки (admin.html, login.html) так не виден: его проверяет страница — tests/e2e/test_admin_smoke.py
открывает админку на английском в headless-браузере и смотрит [...I18N_MISSING] (static/i18n.js).

usage: python stream-tts/check-i18n.py   (любой Python 3, только стандартная библиотека)"""
import ast, glob, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "streamtts")
STATIC = os.path.join(PKG, "static")
STR = r'"((?:[^"\\]|\\.)*)"'
norm = lambda s: " ".join(s.split())


def consts(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):  # tr("…" if x else "…")
        return consts(node.body) + consts(node.orelse)
    return []


def dict_values(node):   # {"ключ": "строка", …}: переводятся значения
    return [s for v in getattr(node, "values", ()) for s in consts(v)]


# Константы, которые переводятся при показе: имя → строки из значения. R_* — отдельно (причины возврата, по одной
# строке на имя, в том числе кортежем: R_A, R_B = "…", "…").
CONSTS = {
    "REPLIES": dict_values,      # ответ зрителю при отказе
    "ACCESS_WHO": dict_values,
    "HELIX_HINTS": dict_values,  # подсказки к ошибкам Twitch Helix
    "GROUPS": dict_values,       # группы на странице голосов
    "SAMPLES": lambda v: [t.elts[1].value for t in getattr(v, "elts", ()) if len(getattr(t, "elts", ())) > 1],
    # названия и описания пакетов: PACKAGES = {"id": dict(title=…, about=…, …)}
    "PACKAGES": lambda v: [s for n in ast.walk(v) if isinstance(n, ast.keyword) and n.arg in ("title", "about")
                           for s in consts(n.value)],
}
# Причины возврата баллов: все должны найтись, иначе их перевод перестал проверяться
REASONS = ("R_OFF", "R_BAN", "R_ACCESS", "R_EMPTY", "R_FULL", "R_NOVOICE", "R_SYNTH", "R_SKIP", "R_CLEAR",
           "R_RESTART", "R_COOLDOWN", "R_REMOVED")


def module_consts(tree, found):
    """Строки констант уровня модуля; found — имена найденных констант."""
    keys = set()
    for n in tree.body:
        if not isinstance(n, ast.Assign) or len(n.targets) != 1:
            continue
        target, value = n.targets[0], n.value
        names = [t.id for t in getattr(target, "elts", [target]) if isinstance(t, ast.Name)]
        if all(name.startswith("R_") for name in names) and names:
            values = value.elts if isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple) else [value]
            for name, v in zip(names, values):
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    keys.add(v.value)
                    found.add(name)
        elif len(names) == 1 and names[0] in CONSTS:
            got = CONSTS[names[0]](value)
            if got:
                keys.update(got)
                found.add(names[0])
    return keys


def python_keys(found):
    keys = set()
    for p in glob.glob(os.path.join(PKG, "**", "*.py"), recursive=True):
        tree = ast.parse(open(p, encoding="utf-8").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "tr" and n.args:
                keys.update(consts(n.args[0]))
        keys |= module_consts(tree, found)
    return keys


def js_keys():
    keys = set()
    for path in sorted(glob.glob(os.path.join(STATIC, "*.js"))) + [os.path.join(STATIC, "overlay.html")]:
        src = open(path, encoding="utf-8").read()
        for m in re.finditer(r'\bT\(\s*((?:' + STR + r'\s*\+?\s*)+)', src):  # T("…" + "…")
            keys.add("".join(json.loads('"' + s + '"') for s in re.findall(STR, m[1])))
        for m in re.finditer(r'\bT\([\w.]+ \? ' + STR + ' : ' + STR, src):   # T(x ? "…" : "…")
            keys.update([m[1], m[2]])
    src = open(os.path.join(STATIC, "voices.html"), encoding="utf-8").read()
    keys.update(re.findall(r"\{\{t:(.*?)\}\}", src, re.S))
    return keys


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows: кириллица в ключах
    en = json.load(open(os.path.join(STATIC, "en.json"), encoding="utf-8"))
    found = set()
    keys = {norm(k) for k in python_keys(found) | js_keys() if re.search("[а-яё]", k, re.I)}
    lost = [name for name in (*REASONS, *CONSTS) if name not in found]
    if lost:
        print("константы не найдены ни в одном модуле streamtts — их строки не проверяются; поправь разбор "
              "в check-i18n.py или имя в REASONS/CONSTS:", *lost, sep="\n  ")
    missing = sorted(k for k in keys if k not in en)
    cyrillic = sorted(k for k, v in en.items() if re.search("[а-яё]", v, re.I))
    slots = lambda s: sorted(re.findall(r"\{(\w+)\}", s)) + sorted(re.findall(r"%(\w+)%", s))
    mismatch = sorted(k for k, v in en.items() if slots(k) != slots(v))
    for title, items in (("без перевода", missing), ("кириллица в переводе", cyrillic),
                         ("подстановки не совпадают", mismatch)):
        if items:
            print(f"{title} ({len(items)}):", *items, sep="\n  ")
    print(f"ключей в коде: {len(keys)}, в en.json: {len(en)}")
    return 1 if lost or missing or cyrillic or mismatch else 0


if __name__ == "__main__":
    sys.exit(main())
