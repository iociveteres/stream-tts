"""Инварианты репозитория без процесса stream-tts (секунды): то, что раньше проверялось вручную командами и grep
(AGENTS.md, «Проверка» и «Подводные камни»; docs/refactor-brief.md, §5).

- все .py из git компилируются (вместо python -m py_compile по списку файлов);
- английский словарь полон (stream-tts/check-i18n.py);
- compose-файлы разбираются (docker compose config -q; без docker — пропуск);
- флаги GPU cosyvoice-server: GGML_CUDA_DISABLE_GRAPHS=1 у встроенного сервера и в образе, --llm-flash-attn 0 на CUDA
  у встроенного сервера и у профиля server-only — без них тихий шум вместо речи и без ошибок в логах (test_real.py
  ловит это только на видеокарте);
- keep-alive к серверу синтеза короче 5 с, через которые cosyvoice-server закрывает простаивающее соединение;
- версия программы задаётся только в streamtts/__init__.py;
- ключ варианта CosyVoice в черновике редактора прежний (иначе у пользователей пропадает синтез черновиков);
- espeech: RUAccent.load не вызывается (сеть и ~1 ГБ RuleEngine), espeech/__init__.py не импортирует torch (он
  подключает библиотеки CUDA до torch);
- архив для внешнего исполнителя (scripts/pack-handoff.py) не берёт данные и окружения Python."""
import ast, importlib.util, os, re, shutil, subprocess, sys

import pytest

from harness import STREAM_TTS

ROOT = os.path.dirname(STREAM_TTS)
sys.path.insert(0, STREAM_TTS)


def git_files(*patterns):
    out = subprocess.run(["git", "ls-files", *patterns], cwd=ROOT, check=True, capture_output=True, text=True).stdout
    return [f for f in out.splitlines() if os.path.isfile(os.path.join(ROOT, f))]


def read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def test_python_compiles():
    """Синтаксис всех .py из git, включая tools/ и espeech/, которые e2e не импортирует. compile() без записи
    __pycache__."""
    bad = []
    for name in git_files("*.py"):
        try:
            compile(read(name), name, "exec")
        except SyntaxError as e:
            bad.append(f"{name}:{e.lineno}: {e.msg}")
    assert not bad, "\n".join(bad)


def test_i18n_complete():
    r = subprocess.run([sys.executable, os.path.join(STREAM_TTS, "check-i18n.py")], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stdout + r.stderr


def test_compose_config():
    if not shutil.which("docker"):
        pytest.skip("docker не найден")
    for files in (["docker-compose.yml"], ["docker-compose.yml", "docker-compose.gpu.yml"]):
        args = [a for f in files for a in ("-f", f)]
        r = subprocess.run(["docker", "compose", *args, "config", "-q"], cwd=ROOT, capture_output=True, text=True)
        if r.returncode and "is not a docker command" in r.stderr:
            pytest.skip("нет docker compose")
        assert r.returncode == 0, f"{files}: {r.stderr}"


def test_gpu_flags():
    from streamtts.engines.cosyvoice import launch
    assert launch.Launcher("cosyvoice-server").env()["GGML_CUDA_DISABLE_GRAPHS"] == "1"
    args = launch.backend_args("CUDA0")
    assert args[args.index("--llm-flash-attn") + 1] == "0", args
    assert re.search(r"^\s*GGML_CUDA_DISABLE_GRAPHS=1\b", read("Dockerfile"), re.M), "Dockerfile: GGML_CUDA_DISABLE_GRAPHS=1"
    command = re.search(r"^\s*command:\s*(\[.*\])", read("docker-compose.gpu.yml"), re.M)
    assert command and '"--llm-flash-attn", "0"' in command.group(1), "docker-compose.gpu.yml: --llm-flash-attn 0"


def test_keepalive_shorter_than_server():
    from streamtts.engines import client
    assert 0 < client.KEEPALIVE < 5, client.KEEPALIVE


def test_version_only_in_init():
    """__version__ присваивается ровно в одном месте — по нему CI проверяет тег релиза, а программа ищет
    обновления."""
    where = []
    for name in git_files("*.py"):
        for node in ast.walk(ast.parse(read(name), name)):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == "__version__" for t in targets):
                    where.append(name)
    assert where == ["stream-tts/streamtts/__init__.py"], where


def test_cosyvoice_draft_key_unchanged():
    """Эталон — ключи черновиков, которые уже лежат у пользователей: менять нельзя, даже «на равноценный»."""
    from streamtts import editor
    from streamtts.engines.cosyvoice import editor as cosy_editor
    maker = cosy_editor.Maker.__new__(cosy_editor.Maker)   # key_parts от состояния не зависит
    v = dict(src="src1", start=1.234, end=5.678, text="  Привет,  мир ", denoise=True)
    assert editor.Editor.key(None, v, maker=maker) == "be90cb7f98c3"
    assert editor.Editor.key(None, dict(v, music=True), maker=maker) == "835931b8876c"
    assert editor.Editor.vocals_name(v) == "vocals-0d5be482beb6"


def test_espeech_imports():
    init = ast.parse(read("espeech/__init__.py"))
    imported = {a.name.split(".")[0] for n in ast.walk(init) if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in (n.names if isinstance(n, ast.Import) else [ast.alias(n.module or "")])}
    assert "torch" not in imported, "espeech/__init__.py импортирует torch раньше библиотек CUDA"
    calls = []
    for name in git_files("espeech/*.py", "tools/espeech/*.py"):
        tree = ast.parse(read(name), name)
        made = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "attr", getattr(n.value.func, "id", "")) == "RUAccent"
                for t in n.targets if isinstance(t, ast.Name)}
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr == "load":
                base = n.value
                if (isinstance(base, ast.Name) and base.id in made | {"RUAccent"}) or \
                        (isinstance(base, ast.Attribute) and base.attr == "RUAccent") or \
                        (isinstance(base, ast.Call) and getattr(base.func, "attr", getattr(base.func, "id", "")) == "RUAccent"):
                    calls.append(f"{name}:{n.lineno}")
    assert not calls, f"RUAccent.load: {calls}"


def test_handoff_skips_data():
    spec = importlib.util.spec_from_file_location("pack_handoff", os.path.join(ROOT, "scripts", "pack-handoff.py"))
    pack = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(pack)
    blocked = {"espeech/.venv311/Lib/site.py": "espeech/.venv311/", ".venv/Scripts/python.exe": ".venv/",
               "stream-tts/.venv/Lib/x.py": "stream-tts/.venv/", "models/espeech/a.safetensors": "models/",
               "espeech-runtime/python/python.exe": "espeech-runtime/", "voices/wc3/wc3-thrall.gguf": "voices/",
               "stream-tts-data/native/settings.json": "stream-tts-data/"}
    for name, where in blocked.items():
        assert pack.forbidden(name) == where, name
    for name in ("espeech/server.py", "docs/refactor-brief.md", "tools/voices/pick.py", "stream-tts/.venv-notes.md"):
        assert pack.forbidden(name) is None, name
    files, _ = pack.file_list()
    assert "AGENTS.md" in files and "docs/refactor-brief.md" in files
    assert not [f for f in files if pack.forbidden(f)]
