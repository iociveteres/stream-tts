"""Наборы голосов из zip (install.py): коллекция «модель + источник» (voice-sets/<модель>/<источник>.zip). Голоса
обеих моделей одного источника лежат в одной папке набора; установка и удаление голосов одной модели не трогают
голоса другой, manifest.json набора — общий. Без процесса stream-tts: установка доступна только со встроенным
сервером (managed), поэтому функции, которые зовёт /api/voicesets/*, вызываются напрямую с папкой голосов во
временном каталоге."""
import json, os, struct, sys, zipfile

import pytest

from harness import STREAM_TTS

sys.path.insert(0, STREAM_TTS)
from streamtts import install, paths  # noqa: E402

GGUF = b"GGUF" + bytes(60)
_head = json.dumps({"__metadata__": {"format": "espeech-voice"}}).encode()
ESPEECH = struct.pack("<Q", len(_head)) + _head + bytes(16)   # safetensors: длина заголовка, JSON, данные


@pytest.fixture
def voices(tmp_path, monkeypatch):
    d = tmp_path / "voices"
    d.mkdir()
    monkeypatch.setattr(paths, "VOICES", str(d))
    return d


def make_zip(tmp_path, name, files):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        for k, v in files.items():
            z.writestr(k, v if isinstance(v, bytes) else json.dumps(v, ensure_ascii=False))
    return str(path)


def put(path):
    return install._install_set(path, install._check_zip(path, os.path.basename(path)))


def listing(d):
    return sorted(os.listdir(d))


def manifest(d):
    return json.loads((d / "manifest.json").read_text("utf-8"))


def test_collections_per_model(voices, tmp_path):
    """Коллекции CosyVoice и ESpeech одного источника ставятся в одну папку и заменяются и удаляются по отдельности."""
    r = put(make_zip(tmp_path, "cosyvoice-wc3.zip", {"wc3/alpha.gguf": GGUF, "wc3/bravo.gguf": GGUF,
                                                     "wc3/manifest.json": {"alpha": {"title": "Альфа"},
                                                                           "bravo": {"title": "Браво"}}}))
    assert (r["name"], r["models"], r["added"]) == ("wc3", ["cosyvoice"], ["alpha", "bravo"])
    r = put(make_zip(tmp_path, "espeech-wc3.zip", {"wc3/alpha.espeech": ESPEECH, "wc3/charlie.espeech": ESPEECH,
                                                   "wc3/manifest.json": {"alpha": {"title": "Альфа"},
                                                                         "charlie": {"title": "Чарли"}}}))
    assert (r["name"], r["models"], r["added"]) == ("wc3", ["espeech"], ["alpha", "charlie"])
    wc3 = voices / "wc3"
    assert listing(wc3) == ["alpha.espeech", "alpha.gguf", "bravo.gguf", "charlie.espeech", "manifest.json"]
    assert [(s["name"], s["model"], s["title"], s["voices"]) for s in install.list_sets()] == [
        ("wc3", "cosyvoice", "CosyVoice3", ["alpha", "bravo"]), ("wc3", "espeech", "ESpeech", ["alpha", "charlie"])]
    assert set(manifest(wc3)) == {"alpha", "bravo", "charlie"}

    # новая версия коллекции CosyVoice без bravo: bravo уходит вместе с записью манифеста, ESpeech на месте
    put(make_zip(tmp_path, "wc3.zip", {"alpha.gguf": GGUF}))
    assert listing(wc3) == ["alpha.espeech", "alpha.gguf", "charlie.espeech", "manifest.json"]
    assert set(manifest(wc3)) == {"alpha", "charlie"}

    install._delete_set("wc3", "espeech")
    assert listing(wc3) == ["alpha.gguf", "manifest.json"]
    assert set(manifest(wc3)) == {"alpha"}
    install._delete_set("wc3", "cosyvoice")   # голосов не осталось — папки нет
    assert not wc3.exists()


def test_taken_voice_per_model(voices, tmp_path):
    """Голос, который у этой модели уже есть без набора, пропускается; у другой модели то же имя ставится."""
    (voices / "alpha.gguf").write_bytes(GGUF)
    r = put(make_zip(tmp_path, "mw.zip", {"alpha.gguf": GGUF, "alpha.espeech": ESPEECH, "bravo.gguf": GGUF}))
    assert r["name"] == "mw" and sorted(r["added"]) == ["alpha", "bravo"]
    assert len(r["skipped"]) == 1 and r["skipped"][0].startswith("alpha.gguf")
    assert listing(voices / "mw") == ["alpha.espeech", "bravo.gguf"]


def test_bad_archives(voices, tmp_path):
    with pytest.raises(ValueError, match="не голос ESpeech"):
        install._check_zip(make_zip(tmp_path, "a.zip", {"a.espeech": GGUF}), "a.zip")
    with pytest.raises(ValueError, match="не голос CosyVoice3"):
        install._check_zip(make_zip(tmp_path, "b.zip", {"b.gguf": ESPEECH}), "b.zip")
    with pytest.raises(ValueError, match="нет голосов"):
        install._check_zip(make_zip(tmp_path, "c.zip", {"readme.txt": b"hi"}), "c.zip")
