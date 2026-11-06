"""Общий код инструментов — только в tools/common.py (секунды, без процессов).

- в tools/ нет своих определений функций common.py (ffmpeg, duration, decode, frames_db, read_json, write_json и
  прежних load_json/save_json cut-clips.py) и своих загрузчиков модуля по пути: spec_from_file_location вне
  common.py — только загрузка самого common.py, соседи — через common.load;
- common.py при импорте тянет только стандартную библиотеку: review.py и pick.py работают без .venv (numpy — внутри
  функций)."""
import ast, glob, os, sys

from harness import STREAM_TTS

ROOT = os.path.dirname(STREAM_TTS)
TOOLS = os.path.join(ROOT, "tools")
COMMON = os.path.join(TOOLS, "common.py")
OWN = {"ffmpeg", "duration", "decode", "frames_db", "read_json", "write_json", "load_json", "save_json"}


def parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read(), path)


def tools():
    return [p for p in glob.glob(os.path.join(TOOLS, "**", "*.py"), recursive=True) if p != COMMON]


def is_spec_call(node):
    f = node.func if isinstance(node, ast.Call) else None
    return isinstance(f, ast.Attribute) and f.attr == "spec_from_file_location"


def test_common_has_helpers():
    names = {n.name for n in parse(COMMON).body if isinstance(n, ast.FunctionDef)}
    assert (OWN - {"load_json", "save_json"}) | {"load"} <= names


def test_no_own_copies():
    bad = []
    for path in tools():
        rel = os.path.relpath(path, ROOT)
        for node in ast.walk(parse(path)):
            if isinstance(node, ast.FunctionDef):
                if node.name in OWN:
                    bad.append(f"{rel}:{node.lineno} def {node.name} — есть в tools/common.py")
                elif any(is_spec_call(n) for n in ast.walk(node)):
                    bad.append(f"{rel}:{node.lineno} def {node.name} — грузит модуль по пути, есть common.load")
            elif is_spec_call(node):
                target = node.args[1] if len(node.args) > 1 else None
                if not (isinstance(target, ast.Call) and target.args
                        and isinstance(target.args[-1], ast.Constant) and target.args[-1].value == "common.py"):
                    bad.append(f"{rel}:{node.lineno} spec_from_file_location — соседа грузить через common.load")
    assert not bad, "\n".join(bad)


def test_common_stdlib_only():
    mods = []
    for node in parse(COMMON).body:
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            mods.append(node.module or "")
    extra = [m for m in mods if m.split(".")[0] not in sys.stdlib_module_names]
    assert not extra, f"tools/common.py импортирует не из stdlib на верхнем уровне: {extra}"
