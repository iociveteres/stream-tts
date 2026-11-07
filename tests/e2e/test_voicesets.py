"""Наборы голосов из zip (voicesets.py): коллекция «модель + источник» (voice-sets/<модель>/<источник>.zip). Голоса
обеих моделей одного источника лежат в одной папке набора; установка и удаление голосов одной модели не трогают
голоса другой, manifest.json набора — общий. Без процесса stream-tts: установка доступна только со встроенным
сервером, поэтому функции, которые зовёт /api/voicesets/*, вызываются напрямую с папкой голосов во
временном каталоге."""
import json, os, struct, sys, zipfile

import pytest

from harness import STREAM_TTS

sys.path.insert(0, STREAM_TTS)
from streamtts import paths, voicesets  # noqa: E402

GGUF = b"GGUF" + bytes(60)
_head = json.dumps({"__metadata__": {"format": "espeech-voice"}}).encode()
ESPEECH = struct.pack("<Q", len(_head)) + _head + bytes(16)   # safetensors: длина заголовка, JSON, данные
# голос Supertonic 3 — JSON стиля (style_ttl, style_dp: dims и data)
SUPERTONIC = {"style_ttl": {"dims": [1, 2, 2], "data": [[[0.1, 0.2], [0.3, 0.4]]]},
              "style_dp": {"dims": [1, 1, 2], "data": [[[0.5, 0.6]]]}}


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
    return voicesets._install_set(path, voicesets._check_zip(path, os.path.basename(path)))


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
    assert [(s["name"], s["model"], s["title"], s["voices"]) for s in voicesets.list_sets()] == [
        ("wc3", "cosyvoice", "CosyVoice3", ["alpha", "bravo"]), ("wc3", "espeech", "ESpeech", ["alpha", "charlie"])]
    assert set(manifest(wc3)) == {"alpha", "bravo", "charlie"}

    # новая версия коллекции CosyVoice без bravo: bravo уходит вместе с записью манифеста, ESpeech на месте
    put(make_zip(tmp_path, "wc3.zip", {"alpha.gguf": GGUF}))
    assert listing(wc3) == ["alpha.espeech", "alpha.gguf", "charlie.espeech", "manifest.json"]
    assert set(manifest(wc3)) == {"alpha", "charlie"}

    voicesets._delete_set("wc3", "espeech")
    assert listing(wc3) == ["alpha.gguf", "manifest.json"]
    assert set(manifest(wc3)) == {"alpha"}
    voicesets._delete_set("wc3", "cosyvoice")   # голосов не осталось — папки нет
    assert not wc3.exists()


def test_shared_manifest_entry(voices, tmp_path):
    """Запись манифеста голоса с файлами обеих моделей сливается по полям: коллекция ESpeech без post не стирает
    эффект голоса CosyVoice. Голос одной модели получает запись новой версии целиком (post можно убрать)."""
    put(make_zip(tmp_path, "wc3.zip", {"wc3/alpha.gguf": GGUF, "wc3/bravo.gguf": GGUF, "wc3/manifest.json": {
        "alpha": {"title": "Альфа", "post": ["effects/x.py"]}, "bravo": {"title": "Браво", "post": ["effects/x.py"]}}}))
    put(make_zip(tmp_path, "wc3e.zip", {"wc3/alpha.espeech": ESPEECH,
                                        "wc3/manifest.json": {"alpha": {"title": "Альфа (E)"}}}))
    wc3 = voices / "wc3"
    assert manifest(wc3)["alpha"] == {"title": "Альфа (E)", "post": ["effects/x.py"]}
    put(make_zip(tmp_path, "wc3v2.zip", {"wc3/bravo.gguf": GGUF, "wc3/manifest.json": {"bravo": {"title": "Браво"}}}))
    assert manifest(wc3)["bravo"] == {"title": "Браво"}
    # голос с файлами обеих моделей в одном zip — один установленный голос
    r = put(make_zip(tmp_path, "mix.zip", {"mix/b.gguf": GGUF, "mix/b.espeech": ESPEECH}))
    assert r["added"] == ["b"] and listing(voices / "mix") == ["b.espeech", "b.gguf"]


def test_taken_voice_per_model(voices, tmp_path):
    """Голос, который у этой модели уже есть без набора, пропускается; у другой модели то же имя ставится."""
    (voices / "alpha.gguf").write_bytes(GGUF)
    r = put(make_zip(tmp_path, "mw.zip", {"alpha.gguf": GGUF, "alpha.espeech": ESPEECH, "bravo.gguf": GGUF}))
    assert r["name"] == "mw" and sorted(r["added"]) == ["alpha", "bravo"]
    assert len(r["skipped"]) == 1 and r["skipped"][0].startswith("alpha.gguf")
    assert listing(voices / "mw") == ["alpha.espeech", "bravo.gguf"]


def test_supertonic_set(voices, tmp_path):
    """Набор supertonic (готовые голоса Supertonic 3): .supertonic ставится своей модели, рядом с голосами других
    моделей того же набора, и удаляется отдельно."""
    r = put(make_zip(tmp_path, "supertonic.zip", {"supertonic/F1.supertonic": SUPERTONIC,
                                                  "supertonic/M1.supertonic": SUPERTONIC,
                                                  "supertonic/manifest.json": {"F1": {"title": "Ф1"}}}))
    assert (r["name"], r["models"], r["added"]) == ("supertonic", ["supertonic"], ["F1", "M1"])
    put(make_zip(tmp_path, "supertonic-cosy.zip", {"supertonic/F1.gguf": GGUF}))
    d = voices / "supertonic"
    assert listing(d) == ["F1.gguf", "F1.supertonic", "M1.supertonic", "manifest.json"]
    assert [(s["model"], s["title"], s["voices"]) for s in voicesets.list_sets()] == [
        ("cosyvoice", "CosyVoice3", ["F1"]), ("supertonic", "Supertonic 3", ["F1", "M1"])]
    voicesets._delete_set("supertonic", "supertonic")
    assert listing(d) == ["F1.gguf", "manifest.json"]


def test_bad_archives(voices, tmp_path):
    with pytest.raises(ValueError, match="не голос ESpeech"):
        voicesets._check_zip(make_zip(tmp_path, "a.zip", {"a.espeech": GGUF}), "a.zip")
    with pytest.raises(ValueError, match="не голос CosyVoice3"):
        voicesets._check_zip(make_zip(tmp_path, "b.zip", {"b.gguf": ESPEECH}), "b.zip")
    for bad in (GGUF, {"style_ttl": {}}, {"style_ttl": {"dims": []}, "style_dp": [1]}, b"\xff\xfe"):
        with pytest.raises(ValueError, match="не голос Supertonic 3"):
            voicesets._check_zip(make_zip(tmp_path, "s.zip", {"s.supertonic": bad}), "s.zip")
    with pytest.raises(ValueError, match=r"нет голосов \(\*\.gguf, \*\.espeech, \*\.supertonic\)"):
        voicesets._check_zip(make_zip(tmp_path, "c.zip", {"readme.txt": b"hi"}), "c.zip")
    # zip-бомба в manifest.json: читается в память целиком, поэтому размер проверяется до чтения
    bomb = make_zip(tmp_path, "m.zip", {"m/a.gguf": GGUF, "m/manifest.json": b" " * (voicesets.MAX_MANIFEST + 1)})
    with pytest.raises(ValueError, match="manifest.json больше 1 МБ"):
        voicesets._check_zip(bomb, "m.zip")


@pytest.mark.parametrize("voice", ["Артес", "-thrall", ".thrall", "_thrall", "thrall\n", "a" * 65])
def test_voice_name_rule(voices, tmp_path, voice):
    """Имя голоса из zip — по тому же правилу, что в редакторе (voices.NAME): иначе ставится голос, который в
    редакторе не создать и не переименовать."""
    with pytest.raises(ValueError, match="недопустимое имя голоса"):
        voicesets._check_zip(make_zip(tmp_path, "s.zip", {f"{voice}.gguf": GGUF}), "s.zip")
    assert voicesets._check_zip(make_zip(tmp_path, "ok.zip", {"Thrall_2.v1-x.gguf": GGUF}), "ok.zip")
