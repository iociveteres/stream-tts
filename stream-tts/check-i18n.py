"""Проверка английского словаря stream-tts (streamtts/static/en.json, механизм — streamtts/i18n.py).

Собирает все строки, которым нужен перевод, из всех их источников:
- tr("…") в streamtts/**/*.py, T("…") в static/*.js и в страницах с i18n.js, {{t:…}} в static/*.html (voices.html);
- разметка страниц с i18n.js (admin.html, login.html, overlay.html) так, как её переводит translatePage(): текстовые
  узлы, атрибуты title/placeholder/aria-label/value, у элемента с data-t — innerHTML целиком; script, style,
  textarea и translate="no" — мимо;
- константы модулей, которые переводятся при показе: причины возврата R_*, ответы зрителю REPLIES и ACCESS_WHO,
  подсказки Helix HELIX_HINTS, группы голосов GROUPS, подписи примеров SAMPLES, названия и описания пакетов PACKAGES;
- схема настроек модели: title и help в PARAMS серверов моделей (espeech/*.py, supertonic/*.py) и в DEFAULT_PARAMS
  клиентов (streamtts/engines/), их переводит web/state.py при отдаче /api/state.
Константы ищутся по имени во всех модулях, а не в конкретном файле: перенос в другой модуль проверку не выключает.
Если известная константа не найдена нигде (переименована или задана не так, как ждёт разбор), это ошибка: иначе её
строки молча выпали бы из проверки, а их переводы — из словаря как лишние.

Ошибки (код выхода 1): ключи без перевода, лишние ключи en.json (строки нет ни в одном источнике — удали ключ или
научи проверку новому источнику), переводы с кириллицей, расхождения подстановок {имя}. Страницу целиком
проверяет и tests/e2e/test_admin_smoke.py: админка на английском в headless-браузере, [...I18N_MISSING].

--missing — только ключи без перевода, готовыми строками JSON (`"ключ": "",`, отступ как в en.json): вписать
переводы и вставить в конец en.json после последнего ключа (ему — запятую); сам en.json целиком читать не нужно.

usage: python stream-tts/check-i18n.py [--missing]   (любой Python 3, только стандартная библиотека)"""
import ast, glob, json, os, re, sys
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.join(HERE, "streamtts")
STATIC = os.path.join(PKG, "static")
# серверы моделей: схема PARAMS
SERVERS = [os.path.join(os.path.dirname(HERE), d) for d in ("espeech", "supertonic")]
STR = r'"((?:[^"\\]|\\.)*)"'
RU = re.compile("[а-яё]", re.I)
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
# Схема настроек модели ({параметр: {title, help, …}}): PARAMS — у сервера, DEFAULT_PARAMS — копия в клиенте,
# пока сервер не ответил; присваивание где угодно, в том числе в теле класса
SCHEMAS = ("PARAMS", "DEFAULT_PARAMS")


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


def schema_keys(tree, found):
    """title и help из схем SCHEMAS."""
    keys = set()
    for n in ast.walk(tree):
        name = getattr(n.targets[0], "id", None) if isinstance(n, ast.Assign) and len(n.targets) == 1 else None
        if name not in SCHEMAS:
            continue
        got = [s for d in ast.walk(n.value) if isinstance(d, ast.Dict) for k, v in zip(d.keys, d.values)
               if isinstance(k, ast.Constant) and k.value in ("title", "help") for s in consts(v)]
        if got:
            keys.update(got)
            found.add(name)
    return keys


def python_keys(found):
    keys = set()
    for p in glob.glob(os.path.join(PKG, "**", "*.py"), recursive=True):
        tree = ast.parse(open(p, encoding="utf-8").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "tr" and n.args:
                keys.update(consts(n.args[0]))
        keys |= module_consts(tree, found) | schema_keys(tree, found)
    for d in SERVERS:
        for p in glob.glob(os.path.join(d, "*.py")):   # без подпапок: там .venv
            keys |= schema_keys(ast.parse(open(p, encoding="utf-8").read()), found)
    return keys


class Markup(HTMLParser):
    """Строки разметки так, как их ищет translatePage() в static/i18n.js."""
    ATTRS = ("title", "placeholder", "aria-label", "value")
    SKIP = ("script", "style", "textarea")
    VOID = ("area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr")

    def __init__(self, src):
        super().__init__(convert_charrefs=True)
        self.src, self.keys, self.stack = src, set(), []   # stack: (тег, мимо ли, начало innerHTML у data-t)
        self.starts = [0]
        for line in src.splitlines(keepends=True):
            self.starts.append(self.starts[-1] + len(line))
        self.feed(src)
        self.close()

    def at(self):  # offset занят HTMLParser
        line, col = self.getpos()
        return self.starts[line - 1] + col

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        skip = any(s for _, s, _ in self.stack) or tag in self.SKIP or a.get("translate") == "no"
        inside_t = any(t is not None for _, _, t in self.stack)   # data-t переводится целиком, с атрибутами
        if not skip and not inside_t:
            self.keys.update(v for k, v in attrs if k in self.ATTRS and v)
        if tag not in self.VOID:
            data_t = "data-t" in a and not skip and not inside_t
            self.stack.append((tag, skip, self.at() + len(self.get_starttag_text()) if data_t else None))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                start = self.stack[i][2]
                del self.stack[i:]
                if start is not None:
                    self.keys.add(self.src[start:self.at()])
                return

    def handle_data(self, data):
        if not any(s or t is not None for _, s, t in self.stack):
            self.keys.add(data)


def static_keys():
    """Ключи из static/: T("…") в *.js и в страницах с i18n.js, разметка этих страниц, {{t:…}} в шаблонах."""
    keys = set()
    pages = sorted(glob.glob(os.path.join(STATIC, "*.html")))
    for path in sorted(glob.glob(os.path.join(STATIC, "*.js"))) + pages:
        src = open(path, encoding="utf-8").read()
        keys.update(re.findall(r"\{\{t:(.*?)\}\}", src, re.S))   # шаблон voices.html: переводит сервер
        if path.endswith(".html"):
            if "i18n.js" not in src:
                continue
            keys |= Markup(src).keys
        for m in re.finditer(r'\bT\(\s*((?:' + STR + r'\s*\+?\s*)+)', src):  # T("…" + "…")
            keys.add("".join(json.loads('"' + s + '"') for s in re.findall(STR, m[1])))
        for m in re.finditer(r'\bT\([\w.]+ \? ' + STR + ' : ' + STR, src):   # T(x ? "…" : "…")
            keys.update([m[1], m[2]])
    return keys


def main():
    sys.stdout.reconfigure(encoding="utf-8")  # консоль Windows: кириллица в ключах
    en = json.load(open(os.path.join(STATIC, "en.json"), encoding="utf-8"))
    found = set()
    every = {norm(k) for k in python_keys(found) | static_keys()}
    keys = {k for k in every if RU.search(k)}   # перевод нужен строкам с кириллицей
    missing = sorted(k for k in keys if k not in en)
    if "--missing" in sys.argv[1:]:
        lines = [f' {json.dumps(k, ensure_ascii=False)}: ""' for k in missing]
        print(",\n".join(lines))
        return 0
    lost = [name for name in (*REASONS, *CONSTS, *SCHEMAS) if name not in found]
    if lost:
        print("константы не найдены ни в одном модуле — их строки не проверяются; поправь разбор "
              "в check-i18n.py или имя в REASONS/CONSTS/SCHEMAS:", *lost, sep="\n  ")
    unused = sorted(k for k in en if k not in every)   # «LoRA {voice}: {pct}%» — ключ и без кириллицы
    cyrillic = sorted(k for k, v in en.items() if RU.search(v))
    slots = lambda s: sorted(re.findall(r"\{(\w+)\}", s)) + sorted(re.findall(r"%(\w+)%", s))
    mismatch = sorted(k for k, v in en.items() if slots(k) != slots(v))
    for title, items in (("без перевода (--missing — строками для en.json)", missing),
                         ("лишние в en.json: строки нет ни в одном источнике", unused),
                         ("кириллица в переводе", cyrillic), ("подстановки не совпадают", mismatch)):
        if items:
            print(f"{title} ({len(items)}):", *items, sep="\n  ")
    print(f"ключей в источниках: {len(keys)}, в en.json: {len(en)}")
    return 1 if lost or missing or unused or cyrillic or mismatch else 0


if __name__ == "__main__":
    sys.exit(main())
