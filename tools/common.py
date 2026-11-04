"""Общее для инструментов tools/: корень проекта, загрузка соседнего скрипта по пути, ffmpeg и звук, JSON.

usage — в начале инструмента tools/<группа>/<скрипт>.py (common.py грузится по пути, как и соседи):
    HERE = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("common", os.path.join(os.path.dirname(HERE), "common.py"))
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)
    pick = common.load("tools/voices/pick.py")

При импорте — только стандартная библиотека: review.py и pick.py работают без .venv; numpy — внутри decode и
frames_db. Своих копий этих функций в tools/ нет — держит tests/e2e/test_tools_common.py.
"""
import importlib.util, json, os, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tools/ -> корень проекта
SR = 24000  # реплики для LoRA ESpeech (extract-morrowind.py, lora-lines.py): decode и frames_db по умолчанию


def load(rel):
    """Скрипт по пути от корня проекта как модуль: tools/wc3/cut-clips.py → модуль cut_clips."""
    name = os.path.splitext(os.path.basename(rel))[0].replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, check=True)
    return float(r.stdout)


def decode(path, sr=SR):
    """→ float32 моно с частотой sr или None: в играх есть битые файлы (Morrowind: i/m/srv_im032.mp3 обрезан)."""
    import numpy as np
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
                       capture_output=True)
    return np.frombuffer(p.stdout, np.float32).copy() if p.returncode == 0 and p.stdout else None


def frames_db(x, hop=SR // 100):
    """Громкость кадров по hop отсчётов (по умолчанию 10 мс) в дБ от самого громкого."""
    import numpy as np
    n = len(x) // hop
    rms = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(1) + 1e-12)
    return 20 * np.log10(rms / rms.max())


def read_json(path, default):
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else default


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
