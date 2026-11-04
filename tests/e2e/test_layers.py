"""Границы слоёв stream-tts: разбор импортов streamtts/**/*.py через AST, без импорта модулей (секунды).

- aiohttp.web — только в streamtts/web/ и __main__.py: ядро (очередь, оверлей, движки) от веб-сервера не зависит;
- движки (streamtts/engines/**) не импортируют слои над собой (ABOVE_ENGINES): веб, загрузки и установку, редактор,
  ядро озвучки и чат; общие помощники загрузки fetch и partzip им можно (engines/espeech/download.py);
- нет циклов импорта между модулями streamtts, считая ленивые импорты внутри функций: такой цикл не виден при
  запуске и ломает импорт, когда функция впервые выполнится (были: install ↔ engines/espeech/download.py,
  download.py ↔ launch.py).

Ребро — модуль, который называет импорт: `from .engines import cosyvoice` — streamtts.engines.cosyvoice. Пакет-родитель,
который Python исполняет попутно, ребром не считается: иначе любой подмодуль был бы в цикле со своим __init__.py.
Последний тест проверяет сам разбор на маленьком пакете с нарушениями всех трёх правил."""
import ast, os

from harness import ROOT, STREAM_TTS

# Слои над движками: импортируют движки или работают поверх них (переименовали модуль — поправьте список)
ABOVE_ENGINES = ("web", "__main__", "desktop", "install", "downloads", "voicesets", "editor", "engine", "overlay",
                 "commands", "rules", "twitch", "chat_irc")
WEB_LAYER = ("web", "__main__")


def modules(root, pkg="streamtts"):
    """{имя модуля: путь} всех .py пакета pkg в папке root."""
    out = {}
    for d, _, files in os.walk(os.path.join(root, pkg)):
        for f in files:
            if f.endswith(".py"):
                parts = os.path.relpath(os.path.join(d, f), root)[:-3].split(os.sep)
                out[".".join(parts[:-1] if parts[-1] == "__init__" else parts)] = os.path.join(d, f)
    return out


def parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), path)


def imports(name, path):
    """[(кандидаты, строка, в функции ли)] — по импортируемому имени; кандидаты — от точного к общему: импорт
    достаётся первому, который окажется модулем (`from X import n` — X.n или X, `import a.b` — a.b или a)."""
    tree = parse(path)
    lazy = {id(n) for fn in ast.walk(tree) if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            for n in ast.walk(fn)}
    package = (name if path.endswith("__init__.py") else name.rpartition(".")[0]).split(".")
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            names = [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            base = ".".join((package[:len(package) - n.level + 1] if n.level else []) + ([n.module] if n.module else []))
            names = [f"{base}.{a.name}" for a in n.names]
        else:
            continue
        for full in names:
            parts = full.split(".")
            out.append(([".".join(parts[:i]) for i in range(len(parts), 0, -1)], n.lineno, id(n) in lazy))
    return out


def edges(mods):
    """{модуль: [(модуль пакета, строка, в функции ли)]}, по первому импорту каждого модуля."""
    graph = {}
    for name, path in mods.items():
        found = {}
        for cands, line, lazy in imports(name, path):
            t = next((c for c in cands if c in mods), None)
            if t and t != name:
                found.setdefault(t, (line, lazy))
        graph[name] = sorted((t, line, lazy) for t, (line, lazy) in found.items())
    return graph


def where(mods, name, line, lazy=False):
    path = mods[name]
    if os.path.abspath(path).startswith(ROOT + os.sep):
        path = os.path.relpath(path, ROOT)
    return f"{path.replace(os.sep, '/')}:{line}" + (" (в функции)" if lazy else "")


def in_layer(name, layers, pkg):
    return any(name == f"{pkg}.{l}" or name.startswith(f"{pkg}.{l}.") for l in layers)


def web_violations(mods, pkg="streamtts"):
    """aiohttp.web вне веб-слоя: импортом или обращением aiohttp.web.… после `import aiohttp`."""
    bad = []
    for name, path in sorted(mods.items()):
        if in_layer(name, WEB_LAYER, pkg):
            continue
        lines = {line for cands, line, _ in imports(name, path) if "aiohttp.web" in cands}
        lines |= {n.lineno for n in ast.walk(parse(path)) if isinstance(n, ast.Attribute) and n.attr == "web"
                  and isinstance(n.value, ast.Name) and n.value.id == "aiohttp"}
        bad += [f"{where(mods, name, line)}: aiohttp.web — только в {pkg}/web/ и __main__.py" for line in sorted(lines)]
    return bad


def engine_violations(mods, graph, pkg="streamtts"):
    return [f"{where(mods, name, line, lazy)}: движок импортирует {t} — это слой над движками"
            for name in sorted(mods) if in_layer(name, ("engines",), pkg)
            for t, line, lazy in graph[name] if in_layer(t, ABOVE_ENGINES, pkg)]


def cycles(mods, graph):
    """По циклу на компоненту сильной связности графа (алгоритм Тарьяна): путь с файлом и строкой каждого импорта."""
    index, low, stack, comps = {}, {}, [], []

    def visit(v):
        index[v] = low[v] = len(index)
        stack.append(v)
        for w, _, _ in graph[v]:
            if w not in index:
                visit(w)
                low[v] = min(low[v], low[w])
            elif w in stack:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            comp = set()
            while not comp or w != v:
                w = stack.pop()
                comp.add(w)
            if len(comp) > 1:
                comps.append(comp)

    for v in graph:
        if v not in index:
            visit(v)
    out = []
    for comp in comps:
        start = min(comp)
        path, seen = [], {start}

        def walk(v):   # путь внутри компоненты обратно к start: он есть, компонента сильно связна
            for w, line, lazy in graph[v]:
                if w == start or (w in comp and w not in seen):
                    seen.add(w)
                    path.append(f"\n  → {w}  ({where(mods, v, line, lazy)})")
                    if w == start or walk(w):
                        return True
                    path.pop()
            return False

        walk(start)
        out.append(f"цикл импорта: {start}" + "".join(path))
    return out


MODS = modules(STREAM_TTS)
GRAPH = edges(MODS)


def test_aiohttp_web_only_in_web_layer():
    assert not (bad := web_violations(MODS)), "\n".join(bad)


def test_engines_do_not_import_upper_layers():
    missing = [l for l in ABOVE_ENGINES if f"streamtts.{l}" not in MODS]
    assert not missing, f"модулей {missing} больше нет: поправьте ABOVE_ENGINES под новое дерево"
    assert not (bad := engine_violations(MODS, GRAPH)), "\n".join(bad)


def test_no_import_cycles():
    assert not (bad := cycles(MODS, GRAPH)), "\n\n".join(bad)


def test_rules_catch_violations(tmp_path):
    """Разбор видит нарушения всех трёх правил, в том числе в функциях, и пропускает разрешённое."""
    files = {
        "__init__.py": "",
        "__main__.py": "from aiohttp import web\n",
        "web/__init__.py": "import aiohttp.web\n",
        "core.py": "import aiohttp\n\n\ndef f():\n    return aiohttp.web.Response()\n",
        "overlay.py": "from aiohttp import ClientSession, web as w\n",
        "engine.py": "from . import util\n",
        "util.py": "def g():\n    from . import engine\n",
        "fetch.py": "",
        "engines/__init__.py": "from . import a\nfrom .. import fetch\n",
        "engines/a.py": "def h():\n    from ..engine import x\n",
    }
    for rel, text in files.items():
        (tmp_path / "pkg" / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "pkg" / rel).write_text(text, encoding="utf-8")
    mods = modules(str(tmp_path), "pkg")
    graph = edges(mods)
    assert graph["pkg.engines"] == [("pkg.engines.a", 1, False), ("pkg.fetch", 2, False)]
    web = web_violations(mods, "pkg")
    assert [b.split(": ")[0].rsplit("/", 1)[-1] for b in web] == ["core.py:5", "overlay.py:1"], web
    eng = engine_violations(mods, graph, "pkg")
    assert len(eng) == 1 and "a.py:2 (в функции): движок импортирует pkg.engine " in eng[0], eng
    cyc = cycles(mods, graph)
    assert len(cyc) == 1 and cyc[0].startswith("цикл импорта: pkg.engine\n  → pkg.util  (") \
        and cyc[0].endswith("\n  → pkg.engine  (" + where(mods, "pkg.util", 2, True) + ")"), cyc
