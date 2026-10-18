"""Проверка английского словаря stream-tts (streamtts/static/en.json, механизм — streamtts/i18n.py).

Ищет строки, которым нужен перевод: tr("…") в streamtts/*.py, T("…") в static/admin.js и static/overlay.html,
{{t:…}} в static/voices.html, плюс константы, которые переводятся при показе (причины возврата R_*, REPLIES и ACCESS_WHO в engine.py,
HELIX_HINTS, GROUPS, подписи примеров и пакеты в install.py). Печатает ключи без перевода, переводы с кириллицей и
расхождения подстановок {имя}. Код выхода 1, если что-то нашлось.

Текст из разметки (admin.html, login.html) так не виден: его проверяет страница. Открыть админку на английском
(конфигурация stream-tts-en в .claude/launch.json), пройти вкладки и посмотреть в консоли [...I18N_MISSING].

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


def python_keys():
    keys = set()
    for p in glob.glob(os.path.join(PKG, "*.py")):
        src = open(p, encoding="utf-8").read()
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "tr" and n.args:
                keys.update(consts(n.args[0]))
        name = os.path.basename(p)
        if name == "engine.py":
            for m in re.finditer(r'^R_\w+(?:, R_\w+)* = \(?((?:"[^"]*"(?:,\s*)?)+)', src, re.M):
                keys.update(re.findall(r'"([^"]*)"', m[1]))
            for d in ("REPLIES", "ACCESS_WHO"):  # ответы зрителю при отказе
                keys.update(re.findall(r': "([^"]+)"', re.search(d + r" = \{(.*?)\}\n", src, re.S)[1]))
        elif name == "twitch.py":
            keys.update(re.findall(r'^\s+"[^"]+": "([^"]+)",$', src.split("HELIX_HINTS")[1].split("}")[0], re.M))
        elif name == "voices.py":
            keys.update(re.findall(r'"\w+": "([^"]+)"', src.split("GROUPS = ")[1].split("\n")[0]))
        elif name == "install.py":
            keys.update(re.findall(r'SAMPLES = \(\("[\w-]+", "([^"]+)"\),\)', src))
            for n in ast.walk(ast.parse(src)):  # названия и описания пакетов (PACKAGES), переводятся при показе
                if isinstance(n, ast.keyword) and n.arg in ("title", "about") and isinstance(n.value, ast.Constant):
                    keys.add(n.value.value)
    return keys


def js_keys():
    keys = set()
    for name in ("admin.js", "overlay.html"):
        src = open(os.path.join(STATIC, name), encoding="utf-8").read()
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
    keys = {norm(k) for k in python_keys() | js_keys() if re.search("[а-яё]", k, re.I)}
    missing = sorted(k for k in keys if k not in en)
    cyrillic = sorted(k for k, v in en.items() if re.search("[а-яё]", v, re.I))
    slots = lambda s: sorted(re.findall(r"\{(\w+)\}", s)) + sorted(re.findall(r"%(\w+)%", s))
    mismatch = sorted(k for k, v in en.items() if slots(k) != slots(v))
    for title, items in (("без перевода", missing), ("кириллица в переводе", cyrillic),
                         ("подстановки не совпадают", mismatch)):
        if items:
            print(f"{title} ({len(items)}):", *items, sep="\n  ")
    print(f"ключей в коде: {len(keys)}, в en.json: {len(en)}")
    return 1 if missing or cyrillic or mismatch else 0


if __name__ == "__main__":
    sys.exit(main())
