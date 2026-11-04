"""Замер навигации для ИИ-агентов (docs/refactor-brief.md, §3): сколько файлов и токенов надо прочитать на каждую
задачу из docs/index.md — сейчас и на базе рефакторинга — и «стоимость входа».

usage: .venv/Scripts/python scripts/measure-docs.py [--base REV]

Задачи — строки таблицы docs/index.md с номером В1… (эталонные вопросы §3) или З1… (частые задачи): файлы — пути в
обратных кавычках в колонках «Файлы сейчас» и «На базе» («=» — те же пути). База — коммит из docs/index.md («База:
`…`») или --base. Файлы сейчас читаются из рабочего дерева, на базе — `git show REV:путь`. Вход — AGENTS.md +
docs/index.md (README агент не читает); для сравнения печатается и прежнее определение §2 — AGENTS.md + README.md +
docs/development.md. Токены — tiktoken, o200k_base (корневой .venv); разбор таблицы (parse) обходится без tiktoken:
его берёт tests/e2e/test_index.py."""
import argparse, os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = "docs/index.md"
ENTRY = ("AGENTS.md", INDEX)
ENTRY_OLD = ("AGENTS.md", "README.md", "docs/development.md")
TASK_ID = re.compile(r"[ВЗ]\d+")
CODE = re.compile(r"`([^`]+)`")
EXT = re.compile(r"[\w.-]+\.(md|py|js|json|html|css|yml|txt|sh|iss|def|map)")


def is_path(token):
    """Путь, а не имя функции или константы: с «/» или имя файла с расширением (`Engine.chat` — не путь)."""
    return " " not in token and ("/" in token or bool(EXT.fullmatch(token)) or token == "Dockerfile")


def paths(cell):
    return [t for t in CODE.findall(cell) if is_path(t)]


def parse(text):
    """docs/index.md → (база, [задачи]); задача — dict(id, title, now, base, tests)."""
    m = re.search(r"База:\s*`([0-9a-f]{7,40})`", text)
    tasks = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5 or not TASK_ID.fullmatch(cells[0]):
            continue
        now = paths(cells[2])
        tasks.append(dict(id=cells[0], title=CODE.sub(r"\1", cells[1]), now=now,
                          base=now if cells[3] == "=" else paths(cells[3]), tests=paths(cells[4])))
    return (m.group(1) if m else None), tasks


def num(x):
    return f"{x:,}".replace(",", " ")


def read_now(path):
    try:
        with open(os.path.join(ROOT, path), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def read_rev(rev, path):
    r = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=ROOT, capture_output=True)
    return r.stdout.decode("utf-8") if r.returncode == 0 else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", help="коммит базы (по умолчанию — из docs/index.md)")
    a = ap.parse_args()
    import tiktoken
    enc = tiktoken.get_encoding("o200k_base")
    text = read_now(INDEX)
    if text is None:
        sys.exit(f"нет {INDEX}")
    base, tasks = parse(text)
    base = a.base or base
    if not base:
        sys.exit(f"в {INDEX} нет строки «База: `<коммит>`», укажите --base")

    def cost(files, read):
        """(файлов, токенов, [недостающие])"""
        total, missing = 0, []
        for f in files:
            body = read(f)
            if body is None:
                missing.append(f)
            else:
                total += len(enc.encode(body, disallowed_special=()))
        return len(files) - len(missing), total, missing

    now_read, base_read = read_now, lambda p: read_rev(base, p)
    print(f"База: {base}. Токены — o200k_base.\n")
    for title, files in (("Вход", ENTRY), ("Вход по §2 ТЗ", ENTRY_OLD)):
        for label, read in (("сейчас", now_read), ("база", base_read)):
            _, tok, missing = cost(files, read)
            note = f" (нет: {', '.join(missing)})" if missing else ""
            print(f"{title} ({' + '.join(files)}), {label}: {num(tok)}{note}")
    print()
    w = max(len(t["title"]) for t in tasks)
    w = min(w, 60)
    print(f"{'№':<4}{'Задача':<{w}}  {'файлов':>6} {'токенов':>8}  | {'база: файлов':>12} {'токенов':>8}")
    sums = {}
    for t in tasks:
        n, tok, miss = cost(t["now"], now_read)
        bn, btok, bmiss = cost(t["base"], base_read)
        title = t["title"] if len(t["title"]) <= w else t["title"][:w - 1] + "…"
        line = f"{t['id']:<4}{title:<{w}}  {n:>6} {num(tok):>8}  | {bn:>12} {num(btok):>8}"
        if miss or bmiss:
            line += f"  нет: {', '.join(miss + [f'база:{m}' for m in bmiss])}"
        print(line)
        group = "В" if t["id"].startswith("В") else "З"
        s = sums.setdefault(group, [0, 0, 0, 0, 0])
        s[0] += 1; s[1] += n; s[2] += tok; s[3] += bn; s[4] += btok
    print()
    for group, label in (("В", "эталонные вопросы"), ("З", "частые задачи")):
        if group in sums:
            k, n, tok, bn, btok = sums[group]
            print(f"Итого {label} ({k}): файлов {n}, токенов {num(tok)}; на базе — файлов {bn}, токенов {num(btok)}; "
                  f"в среднем на задачу {num(tok // k)} против {num(btok // k)}")


if __name__ == "__main__":
    main()
