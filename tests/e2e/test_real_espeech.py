"""ESpeech с настоящей моделью (маркер real, по умолчанию пропускается): pytest tests/e2e -m real.

stream-tts сам запускает espeech-server интерпретатором espeech/.venv (ESPEECH_SERVER) на свободном
порту, со своими данными: модель — жёсткие ссылки (или копии) на models/espeech во временной папке, голос wc3-jaina
собирает tools/espeech/make-voice.py из voice-refs/cosyvoice/wc3/wc3-jaina.wav. Модель выбрана заранее (settings.json).
Если есть и cosyvoice-server с Q5_K_M (как в test_real.py), тот же процесс проверяет смену модели: голос dota2-pudge
есть только у CosyVoice, wc3-jaina — только у ESpeech. Серверы на видеокарте по одному (смена — перезапуск); Docker-
версию не трогает, при нехватке видеопамяти тесты пропускаются.

Речь проверяется как в test_real.py: длина, громкость и текст, распознанный faster-whisper. Время синтеза и первого
звука — в итог прогона, без порогов. Редактор голоса: из той же записи Джайны — кусок 6,4 с целой фразой (паузы — по
ffmpeg silencedetect), темп — по всей записи; словарь ударений — омограф «замок»."""
import json, os, shutil, subprocess, tempfile, time

import pytest

from harness import ROOT, StreamTTS, decode, wait_for
from test_real import ASR_PY, LONG, MODEL, NEED_VRAM_MB, SERVER, SHORT, VOICE, Real, free_vram, link

pytestmark = pytest.mark.real

ESPEECH_PY = os.path.join(ROOT, "espeech", ".venv", "Scripts", "python.exe") if os.name == "nt" else \
    os.path.join(ROOT, "espeech", ".venv", "bin", "python")
ESPEECH_MODELS = os.path.join(ROOT, "models", "espeech")
REF = os.path.join(ROOT, "voice-refs", "cosyvoice", "wc3", "wc3-jaina")
JAINA = "wc3-jaina"
# Фраза для ESpeech: «синтеза речи» из SHORT голос Джайны иногда смазывает так, что faster-whisper узнаёт меньше 60 %
# слов (57 % в одном прогоне из пяти)
PHRASE = "Привет, чат! Сегодня играем до победного, никто не уходит."
# Кусок записи wc3-jaina между паузами (1,48 и 7,64 с по silencedetect −40 дБ) и ровно то, что в нём звучит
PIECE = (1.35, 7.75, "Чтобы спасти своего друга Задиру, ты должен заключить его душу в Камень и принести его обратно "
                     "в лагерь.")


def link_tree(src, dst):
    for d, _, files in os.walk(src):
        for f in files:
            link(os.path.join(d, f), os.path.join(dst, os.path.relpath(os.path.join(d, f), src)))


def state(real):
    return real.api.state()["cosy"]


@pytest.fixture(scope="module")
def esp(loop, tmp_path_factory):
    for path, what in ((ESPEECH_PY, "espeech/.venv"), (os.path.join(ESPEECH_MODELS, "espeech_fp16.safetensors"),
                       "models/espeech (tools/espeech/prepare.py)"), (REF + ".wav", "образца voice-refs/cosyvoice/wc3/wc3-jaina"),
                       (ASR_PY, "корневого .venv с faster-whisper")):
        if not os.path.exists(path):
            pytest.skip(f"нет {what}: {path}")
    vram = free_vram()
    if vram is not None and vram < NEED_VRAM_MB:
        pytest.skip(f"свободно {vram} МБ видеопамяти, нужно {NEED_VRAM_MB}: занята (стрим?), Docker не трогаю")
    wd = str(tmp_path_factory.mktemp("real-espeech"))
    models, voices = os.path.join(wd, "models"), os.path.join(wd, "voices")
    link_tree(ESPEECH_MODELS, os.path.join(models, "espeech"))
    os.makedirs(os.path.join(voices, "wc3"))
    text = open(REF + ".txt", encoding="utf-8").read().strip()
    r = subprocess.run([ESPEECH_PY, os.path.join(ROOT, "tools", "espeech", "make-voice.py"),
                        os.path.join(voices, "wc3", JAINA + ".espeech"), "--wav", REF + ".wav", "--text", text],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    env = dict(ESPEECH_SERVER=ESPEECH_PY, STREAM_TTS_VOICES_CHECK="5")
    cosy = os.path.isfile(SERVER) and os.path.isfile(MODEL) and os.path.isfile(VOICE)
    if cosy:   # смена модели: CosyVoice с голосом, которого у ESpeech нет
        link(MODEL, os.path.join(models, os.path.basename(MODEL)))
        link(VOICE, os.path.join(voices, os.path.basename(VOICE)))
        env["COSYVOICE_SERVER"] = os.path.dirname(SERVER)
    os.makedirs(os.path.join(wd, "data"))
    with open(os.path.join(wd, "data", "settings.json"), "w", encoding="utf-8") as f:
        json.dump(dict(engine="espeech"), f)
    server = StreamTTS(wd, None, env)
    try:
        server.start(timeout=300)
        real = Real(loop, server, tempfile.mkdtemp(dir=wd))
        real.cosy = cosy
        real.idle()   # пример голоса синтезируется сам
        yield real
    finally:
        server.stop()
        shutil.rmtree(models, ignore_errors=True)


def test_espeech_startup(esp, report):
    c, rt = state(esp), esp.runtime()
    assert c["engine"] == "espeech" and rt["state"] == "running" and rt["model"] == "ESpeech-TTS-1_SFT-256K"
    assert [v["name"] for v in esp.api.state()["voices"]] == [JAINA]
    assert [(p["key"], p["choices"]) for p in c["params"]] == [("espeech_steps", [12, 16, 32])]
    assert c["capabilities"]["accent"] and [e["id"] for e in c["engines"] if e["available"]] == \
        (["cosyvoice", "espeech"] if esp.cosy else ["espeech"])
    report(f"ESpeech: устройство {rt['backend']}, CosyVoice для смены модели — {'есть' if esp.cosy else 'нет'}")


def test_espeech_synth(esp, report):
    """Короткая фраза — целиком (громкость по LUFS), длинная — кусками потоком (громкость по примеру голоса)."""
    for text, label, tol in ((PHRASE, "ESpeech, короткая", 1.0), (LONG, "ESpeech, длинная", 3.0)):
        x, _ = esp.speak(text, report, label, voice=JAINA)
        esp.check_speech(x, text, report, label, lufs_tol=tol)


def test_espeech_accent(esp, report):
    r = esp.api.ok("/api/accent", dict(text="В 2024 году мы купили 3 замка."))
    assert "2024" not in r["normalized"] and "+" in r["accented"]
    report(f"как прочитает: «{r['accented']}»")


def test_espeech_bench_steps(esp, report):
    """Проверка скорости — с выбранным числом шагов: замер на 32 шагах отдельной строкой."""
    esp.api.ok("/api/settings", dict(espeech_steps=32))
    try:
        esp.api.ok("/api/bench", dict(all=False))
        b = wait_for(lambda: (lambda b: b if b["state"] in ("done", "error") else None)(esp.api.state()["bench"]),
                     600, f"проверки скорости\n{esp.server.tail()}", step=1)
        assert b["state"] == "done", b["error"]
        row = next(r for r in b["results"] if r.get("steps") == 32)
        assert row["model"] == "ESpeech-TTS-1_SFT-256K" and row["rtf"] > 0 and row["first"] > 0
        report(f"скорость ESpeech, 32 шага [{row['backend']}]: RTF {row['rtf']}, первый звук {row['first']} с")
    finally:
        esp.api.ok("/api/settings", dict(espeech_steps=16))


def test_espeech_editor(esp, report):
    """Редактор с настоящей моделью: разметка расшифровки, синтез ячейки куском с темпом по всей записи, сохранение в
    .espeech — голос в списке и звучит."""
    api = esp.api
    api.ok("/api/draft/discard")
    r = api.upload("/api/draft/upload", open(REF + ".wav", "rb").read(), "wc3-jaina.wav")
    assert r.json()["ok"], r.text
    src = r.json()["source"]
    acc = api.ok("/api/draft/accent", dict(text=PIECE[2]))["accented"]
    assert acc.count("+") >= 10 and acc.replace("+", "") == PIECE[2]
    full = open(REF + ".txt", encoding="utf-8").read().strip()
    v = dict(id="a", src=src["id"], start=PIECE[0], end=PIECE[1], text=acc, denoise=False)
    api.ok("/api/draft", dict(variants=[v], tempo=dict(src=src["id"], start=0, end=src["dur"], text=full)))
    phrase = api.state()["settings"]["test_phrases"][0]
    t0 = time.monotonic()
    r = api.get("/api/draft/synth?variant=a&phrase=0", timeout=300)
    assert r.status == 200, r.text
    esp.check_speech(decode(r.body), phrase, report, f"редактор, ячейка за {time.monotonic() - t0:.1f} с")
    name = "jaina-editor"
    r = api.ok("/api/draft/save", dict(variant="a", name=name, set="editor"))
    path = os.path.join(esp.server.dir, "voices", "editor", name + ".espeech")
    assert r["path"] == f"editor/{name}.espeech" and os.path.isfile(path)
    with open(path, "rb") as f:   # заголовок safetensors: в stream-tts/.venv пакета safetensors нет
        n = int.from_bytes(f.read(8), "little")
        md = json.loads(f.read(n))["__metadata__"]
    assert md["text"] == acc and md["format"] == "espeech-voice"
    report(f"голос из редактора: темп {float(md['tempo']):.3f} кадров на байт (по всей записи)")
    wait_for(lambda: name in [x["name"] for x in api.state()["voices"]], 30, "голоса из редактора в списке")
    esp.idle()
    x, _ = esp.speak(PHRASE, report, "голос из редактора", voice=name)
    esp.check_speech(x, PHRASE, report, "голос из редактора")
    api.ok("/api/draft/discard")


def test_espeech_user_accents(esp, report):
    """Словарь ударений: слово перекрывает омограф в «Как прочитает» и хранится в <данные>/espeech/accents.json."""
    api = esp.api
    text = "Старый замок стоит на горе."
    word = api.ok("/api/accent", dict(text=text))["accented"].split()[1]
    other = "зам+ок" if word == "з+амок" else "з+амок"
    api.ok("/api/accents", dict(accented=other))
    try:
        assert api.ok("/api/accent", dict(text=text))["accented"].split()[1] == other
        saved = json.load(open(os.path.join(esp.server.data, "espeech", "accents.json"), encoding="utf-8"))
        assert saved["words"] == {"замок": other}
        report(f"словарь ударений: модель — «{word}», словарь — «{other}»")
    finally:
        api.ok("/api/accents/delete", dict(word="замок"))
    assert api.ok("/api/accent", dict(text=text))["accented"].split()[1] == word


def test_switch_model(esp, report):
    """Смена модели: перезапуск сервера другого движка на том же порту, голоса — свои у каждой.
    Последним: сервер перезапускается."""
    if not esp.cosy:
        pytest.skip("нет cosyvoice-server или модели Q5_K_M: смена модели не проверена")
    for engine, voice, model in (("cosyvoice", "dota2-pudge", os.path.basename(MODEL)),
                                 ("espeech", JAINA, "ESpeech-TTS-1_SFT-256K")):
        t0 = time.monotonic()
        esp.api.ok("/api/engine", dict(engine=engine))
        wait_for(lambda: (lambda c: c["engine"] == engine and c["ok"] and not c["switching"]
                          and c["runtime"]["model"] == model)(state(esp)), 300,
                 f"смены модели на {engine}\n{esp.server.tail()}", step=0.5)
        report(f"смена модели на {engine}: {time.monotonic() - t0:.1f} с")
        esp.idle()
        names = [v["name"] for v in esp.api.state()["voices"]]   # у ESpeech ещё голос из редактора
        assert voice in names and ({"dota2-pudge", JAINA} - {voice}).isdisjoint(names), names
        text = SHORT if engine == "cosyvoice" else PHRASE
        x, _ = esp.speak(text, report, engine, voice=voice)
        esp.check_speech(x, text, report, engine)
