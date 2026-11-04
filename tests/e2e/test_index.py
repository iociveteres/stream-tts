"""docs/index.md — навигация для ИИ-агентов (docs/refactor-brief.md, §3): без процесса stream-tts, секунды.

- в таблице есть строки на все 9 эталонных вопросов (В1–В9), у каждой задачи — файлы и тест;
- каждая строка таблицы разбирается (scripts/measure-docs.py считает по ней файлы и токены);
- все пути таблицы — файлы из git, пути колонки «На базе» — файлы базового коммита;
- остальные пути документа (с «/», в папках из git) существуют: переименовал модуль — поправь индекс."""
import importlib.util, os, re, subprocess

import pytest

from harness import STREAM_TTS

ROOT = os.path.dirname(STREAM_TTS)
QUESTIONS = [f"В{i}" for i in range(1, 10)]


def load_measure():
    spec = importlib.util.spec_from_file_location("measure_docs", os.path.join(ROOT, "scripts", "measure-docs.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


measure = load_measure()


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8")


@pytest.fixture(scope="module")
def index():
    with open(os.path.join(ROOT, measure.INDEX), encoding="utf-8") as f:
        text = f.read()
    base, tasks = measure.parse(text)
    return text, base, tasks


@pytest.fixture(scope="module")
def tracked():
    return set(git("ls-files").stdout.splitlines())


def test_questions(index):
    text, base, tasks = index
    assert base, "нет строки «База: `<коммит>`»"
    ids = [t["id"] for t in tasks]
    assert [i for i in ids if i in QUESTIONS] == QUESTIONS, ids
    assert len(ids) == len(set(ids)), ids
    rows = [line for line in text.splitlines() if re.match(r"\|\s*[ВЗ]\d+\s*\|", line)]
    assert len(rows) == len(tasks), "строка таблицы не разобралась: нужно 5 колонок"
    empty = [t["id"] for t in tasks if not (t["now"] and t["base"] and t["tests"])]
    assert not empty, f"без файлов или теста: {empty}"


def test_paths_in_git(index, tracked):
    _, _, tasks = index
    missing = [f"{t['id']}: {p}" for t in tasks for p in t["now"] + t["tests"] if p not in tracked]
    assert not missing, "\n".join(missing)


def test_base_paths(index):
    _, base, tasks = index
    if git("cat-file", "-e", f"{base}^{{commit}}").returncode:
        pytest.skip(f"нет коммита {base}")
    listed = set(git("ls-tree", "-r", "--name-only", base).stdout.splitlines())
    missing = [f"{t['id']}: {p}" for t in tasks for p in t["base"] if p not in listed]
    assert not missing, "\n".join(missing)


def test_other_paths(index, tracked):
    text, _, tasks = index
    base = {p for t in tasks for p in t["base"]}   # пути базы сверяет test_base_paths
    tops = {p.split("/")[0] for p in tracked}
    dirs = {p.rsplit("/", 1)[0] + "/" for p in tracked if "/" in p}
    dirs |= {d[:i + 1] for d in dirs for i, c in enumerate(d) if c == "/"}
    bad = []
    for token in measure.CODE.findall(text):
        if "/" not in token or re.search(r"[<>*{} ]", token) or token.split("/")[0] not in tops or token in base:
            continue
        if token not in tracked and token.rstrip("/") + "/" not in dirs:
            bad.append(token)
    assert not bad, bad
