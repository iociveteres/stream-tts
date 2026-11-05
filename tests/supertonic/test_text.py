"""supertonic: подготовка текста и звук без модели — нормализация русского, ударения, нарезка на куски,
пересчёт частоты 44,1 → 24 кГц без наложения спектров. Секунды."""
import os, sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from supertonic import model as M
from supertonic.text import prepare


def test_prepare_ru():
    s = prepare("Спасибо, xXx_Killer_2007, за 500 баллов! Зал+ип в 18:30, gg", "ru")
    assert s == "Спасибо, киллер две тысячи семь, за пятьсот баллов! Залип в восемнадцать тридцать, гэгэ"
    assert "+" not in prepare("Старый з+амок", "ru")
    assert prepare("Hello, 5 cats", "en") == "Hello, 5 cats"   # не русский — как есть


def test_no_torch():
    import subprocess
    code = "import sys, supertonic.server; print('torch' in sys.modules or 'ruaccent' in sys.modules)"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    assert r.stdout.strip() == "False", r.stderr


def test_preprocess():
    assert M.preprocess("Ёж  — ёлка", "ru") == "<ru>Ёж - ёлка.</ru>"
    assert M.preprocess("Привет!", "ru").endswith("!</ru>")


def test_chunks():
    text = "Первое. " + "Слово " * 60 + "конец. Третье предложение!"
    chunks = M.chunk_text(text, 300)   # предложение длиннее max_len — целиком отдельным куском
    assert [len(c) for c in chunks] == [7, 366, 19]
    assert M.chunk_text("Раз. Два. Три.", 9) == ["Раз. Два.", "Три."]
    assert M.chunk_text("Раз. Два.\n\nТри.") == ["Раз. Два.", "Три."]


def test_resample():
    sr = 44100
    t = np.arange(sr) / sr
    low = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
    y = M.resample(low, sr, 24000)
    assert len(y) == 24000
    ref = np.sin(2 * np.pi * 1000 * np.arange(24000) / 24000)
    assert np.abs(y[1000:-1000] - ref[1000:-1000]).max() < 0.01
    # 15 кГц выше новой частоты Найквиста: исчезает, а не возвращается призраком на 9 кГц, как у np.interp
    high = np.sin(2 * np.pi * 15000 * t).astype(np.float32)
    assert np.sqrt(np.mean(M.resample(high, sr, 24000)[1000:-1000] ** 2)) < 0.01
