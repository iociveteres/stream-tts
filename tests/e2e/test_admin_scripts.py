"""Скрипты админки (static/admin.html) без браузера, секунды.

Админка — классические скрипты (не модули) с общей глобальной областью, порядок загрузки важен:
- каждый <script src> из admin.html есть в static/;
- верхнего уровня объявления (function, class, let, const, var) не повторяются между файлами и внутри файла:
  повторный let/const роняет загрузку второго скрипта, а повторная function молча подменяет первую;
- синтаксис — node --check по каждому файлу и по всем вместе в порядке загрузки (без node — пропуск).
Что скрипты работают вместе на настоящей странице — test_admin_smoke.py."""
import collections, os, re, shutil, subprocess

import pytest

from harness import STREAM_TTS

STATIC = os.path.join(STREAM_TTS, "streamtts", "static")
DECL = re.compile(r"^(?:async\s+)?function\*?\s+([\w$]+)|^class\s+([\w$]+)|^(?:let|const|var)\s+([\w$]+)(.*)$", re.M)
# let a = 1, b = "x";  — несколько имён с простыми значениями (без скобок и запятых внутри)
MORE = re.compile(r"^(?:\s*=\s*[^,;()\[\]{}]+)?((?:,\s*[\w$]+\s*=\s*[^,;()\[\]{}]+)+);")


def scripts():
    with open(os.path.join(STATIC, "admin.html"), encoding="utf-8") as f:
        return re.findall(r'<script src="/static/([^"]+)"></script>', f.read())


def read(name):
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        return f.read()


def declared(src):
    for m in DECL.finditer(src):
        if m[1] or m[2]:
            yield m[1] or m[2]
            continue
        yield m[3]
        if more := MORE.match(m[4]):
            yield from re.findall(r",\s*([\w$]+)\s*=", more[1])


def test_scripts_exist():
    names = scripts()
    assert names[:2] == ["i18n.js", "admin.js"], names   # T() и перевод разметки, затем общее админки — до вкладок
    # Старт — последним из всех, не только из admin*.js: renderAll зовёт функции editor.js и train.js, а ответ
    # /api/state может прийти, пока они ещё не выполнились (1.0.1: trAvailable is not defined при первом открытии)
    assert names[-1] == "admin-start.js", names
    missing = [n for n in names if not os.path.isfile(os.path.join(STATIC, n))]
    assert not missing, missing


def test_no_duplicate_declarations():
    where = collections.defaultdict(list)
    for name in scripts():
        for d in declared(read(name)):
            where[d].append(name)
    assert len(where) > 100, f"разбор объявлений сломан: найдено {len(where)}"
    dup = {d: files for d, files in where.items() if len(files) > 1}
    assert not dup, f"объявлено дважды: {dup}"


def test_node_check(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node не найден")
    names = scripts()
    together = tmp_path / "admin-all.js"
    together.write_text("\n;\n".join(read(n) for n in names), encoding="utf-8")
    for path in [os.path.join(STATIC, n) for n in names] + [str(together)]:
        r = subprocess.run([node, "--check", path], capture_output=True, text=True, encoding="utf-8")
        assert r.returncode == 0, f"{path}:\n{r.stderr}"
