"""Проверка с настоящей моделью (маркер real, по умолчанию пропускается): pytest tests/e2e -m real.

stream-tts в режиме managed запускает свой cosyvoice-server из runtime/ проекта на свободном порту, со своими
данными; модель (CosyVoice3-2512_Q5_K_M.gguf), ONNX-фронтенд и htdemucs — жёсткие ссылки (или копии, если папка
теста на другом диске) во временной папке моделей, голос dota2-pudge — копия во временной папке голосов. Оригиналы в
models/ и voices/ не меняются. Это второй сервер на той же видеокарте (1,5–1,9 ГБ видеопамяти), а Docker-версия
может работать на стриме: при нехватке видеопамяти тесты пропускаются, Docker не трогается.

Ошибки CUDA-бэкенда (без GGML_CUDA_DISABLE_GRAPHS=1 и --llm-flash-attn 0) дают тихий шум или мусор вместо речи без
ошибок в логах, поэтому фраза проверяется по звуку: длина в разумных пределах, громкость около −16 LUFS и текст,
распознанный faster-whisper (asr.py в корневом .venv), близкий к исходному. Время синтеза и первого звука — в итог
прогона (фикстура report), без порогов."""
import json, os, re, shutil, subprocess, sys, tempfile, time

import numpy as np
import pytest

from harness import ROOT, SR, Api, Overlay, StreamTTS, decode, make_wav, sine, wait_for

sys.path.insert(0, os.path.join(ROOT, "stream-tts"))
from streamtts import audio  # noqa: E402

pytestmark = pytest.mark.real

SERVER = os.path.join(ROOT, "runtime", "cosyvoice-server.exe" if os.name == "nt" else "cosyvoice-server")
MODEL = os.path.join(ROOT, "models", "CosyVoice3-2512_Q5_K_M.gguf")
FRONTEND = os.path.join(ROOT, "models", "frontend-onnx")
DEMUCS = os.path.join(ROOT, "models", "demucs", "htdemucs_int8.onnx")
VOICE = os.path.join(ROOT, "voices", "dota2-pudge.gguf")
ASR_PY = os.path.join(ROOT, ".venv", "Scripts", "python.exe") if os.name == "nt" else     os.path.join(ROOT, ".venv", "bin", "python")
NEED_VRAM_MB = 2500   # сервер на Q5_K_M — до 1,9 ГБ, faster-whisper small — ~0,5 ГБ
MIN_WORDS = 0.6       # доля слов фразы, которые должен узнать faster-whisper
CHARS_PER_SEC, MAX_LEN_FACTOR = 12.0, 1.8   # config.py: расчётный темп и жёсткая обрезка

SHORT = "Привет! Это проверка синтеза речи настоящей моделью."
LONG = ("Сегодня мы проверяем стриминг длинной фразы. Звук должен приходить по частям, а голос звучать чисто, "
        "без шума и посторонних звуков, от первого слова и до самого конца предложения.")
SAMPLE = ("Я мясник, и я иду за тобой. Свежее мясо всегда приходит само, нужно только немного подождать "
          "у реки.")


def words(text):
    return re.findall(r"[а-яa-z]+", text.lower().replace("ё", "е"))


def match(ref, got):
    """Доля слов ref, найденных в got (с учётом повторов)."""
    pool = words(got)
    hit = 0
    for w in words(ref):
        if w in pool:
            pool.remove(w)
            hit += 1
    return hit / max(1, len(words(ref)))


def recognize(tmp, *clips):
    paths = []
    for i, x in enumerate(clips):
        p = os.path.join(tmp, f"asr-{time.monotonic_ns()}-{i}.wav")
        with open(p, "wb") as f:
            f.write(make_wav(x))
        paths.append(p)
    r = subprocess.run([ASR_PY, os.path.join(os.path.dirname(__file__), "asr.py"), *paths], capture_output=True,
                       text=True, encoding="utf-8", timeout=600)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    return out["texts"], out["device"]


def free_vram():
    """Свободная видеопамять, МБ (первая видеокарта); None — nvidia-smi нет."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    r = subprocess.run([exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"], capture_output=True,
                       text=True, timeout=20)
    return int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout.strip() else None


def link(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:   # папка теста на другом диске
        shutil.copyfile(src, dst)


class Real:
    def __init__(self, loop, server, tmp):
        self.loop, self.server, self.tmp = loop, server, tmp
        self.api = Api(server.base)

    def runtime(self):
        return self.api.state()["cosy"]["runtime"]

    def idle(self, timeout=300):
        def done():
            s = self.api.state()
            return (s["cosy"]["ok"] and not s["engine"]["queue"] and not s["cosy"]["busy"]
                    and s["demos"]["todo"] == 0 and not s["demos"]["current"])
        wait_for(done, timeout, f"простоя stream-tts\n{self.server.tail()}", step=0.5)

    def speak(self, text, report, label, voice="dota2-pudge"):
        """Фраза через очередь и оверлей: (звук, событие). Замеры — в отчёт."""
        ov = Overlay(self.loop, self.server.base).connect()
        try:
            t0 = time.monotonic()
            rid = self.api.test(text, login="realtester", voice=voice)
            it = ov.played(rid, timeout=300)
            e = self.server.event(rid, 30)
        finally:
            ov.close()
        assert e["outcome"] == "played", e
        report(f"{label} [{self.runtime()['backend']}]: {it.seconds:.1f} с звука, синтез {e['synth_ms'] / 1000:.1f} с "
               f"(RTF {e['synth_ms'] / 1000 / it.seconds:.2f}), первый звук через {it.first_at - t0:.2f} с")
        return it.audio, e

    def check_speech(self, x, text, report, label, lufs_tol=1.0):
        """Речь, а не тишина, шум или мусор: длина, громкость, распознанный текст."""
        expect = len(text) / CHARS_PER_SEC
        sec = len(x) / SR
        assert 0.4 * expect < sec < 0.95 * MAX_LEN_FACTOR * expect, f"{sec:.1f} с при расчётных {expect:.1f} с"
        lufs = audio.loudness(x)
        assert abs(lufs - (-16)) < lufs_tol, f"{lufs:.1f} LUFS"
        (got,), device = recognize(self.tmp, x)
        share = match(text, got)
        report(f"{label}: {lufs:.1f} LUFS, распознано {share:.0%} слов ({device}): «{got}»")
        assert share >= MIN_WORDS, f"распознано «{got}»"


@pytest.fixture(scope="module")
def real(loop, tmp_path_factory):
    for path, what in ((SERVER, "cosyvoice-server в runtime/"), (MODEL, "модели Q5_K_M в models/"),
                       (VOICE, "голоса voices/dota2-pudge.gguf"), (ASR_PY, "корневого .venv с faster-whisper")):
        if not os.path.isfile(path):
            pytest.skip(f"нет {what}: {path}")
    vram = free_vram()
    if vram is not None and vram < NEED_VRAM_MB:
        pytest.skip(f"свободно {vram} МБ видеопамяти, нужно {NEED_VRAM_MB}: занята (стрим?), Docker не трогаю")
    wd = str(tmp_path_factory.mktemp("real"))
    models = os.path.join(wd, "models")
    link(MODEL, os.path.join(models, os.path.basename(MODEL)))
    if os.path.isdir(FRONTEND):
        for f in os.listdir(FRONTEND):
            link(os.path.join(FRONTEND, f), os.path.join(models, "frontend-onnx", f))
    if os.path.isfile(DEMUCS):
        link(DEMUCS, os.path.join(models, "demucs", os.path.basename(DEMUCS)))
    os.makedirs(os.path.join(wd, "voices"))
    shutil.copyfile(VOICE, os.path.join(wd, "voices", os.path.basename(VOICE)))
    server = StreamTTS(wd, None, dict(COSYVOICE_SERVER=os.path.dirname(SERVER), STREAM_TTS_VOICES_CHECK="5"))
    t0 = time.monotonic()
    try:
        server.start(timeout=300)
        r = Real(loop, server, tempfile.mkdtemp(dir=wd))
        r.idle()   # пример голоса синтезируется сам
        yield r
    finally:
        server.stop()
        shutil.rmtree(models, ignore_errors=True)   # копии моделей не копятся во временной папке


def test_startup(real, report):
    rt = real.runtime()
    assert rt["state"] == "running" and rt["model"] == os.path.basename(MODEL) and rt["voices"] == 1
    s = real.api.state()
    assert [v["name"] for v in s["voices"]] == ["dota2-pudge"] and s["voices"][0]["demo"]
    report(f"сервер: бэкенд {rt['backend']} (есть: {', '.join(rt['backends']) or 'CPU'}), фронтенд "
           f"{'есть' if os.path.isdir(FRONTEND) else 'нет'}")


def test_synth_whole(real, report):
    x, e = real.speak(SHORT, report, "целиком")
    real.check_speech(x, SHORT, report, "целиком")


def test_synth_stream(real, report):
    """Длинная фраза — стримингом: громкость выставлена заранее по примеру голоса, поэтому допуск шире."""
    x, e = real.speak(LONG, report, "стриминг")
    real.check_speech(x, LONG, report, "стриминг", lufs_tol=3.0)


def test_voice_from_recording(real, report):
    """Голос из записи через редактор: образец — фраза голосом pudge, из него извлекается новый голос, им
    синтезируется тестовая фраза. С пакетом htdemucs — то же для образца, смешанного с музыкой."""
    if not os.path.isdir(FRONTEND):
        pytest.skip(f"нет ONNX-фронтенда: {FRONTEND}")
    api = real.api
    x, _ = real.speak(SAMPLE, report, "образец")
    pad = np.zeros(SR // 2, np.float32)
    clean = np.concatenate([pad, x, pad])
    dur = len(clean) / SR
    r = api.upload("/api/draft/upload", make_wav(clean), "sample.wav")
    assert r.json()["ok"], r.text
    src = r.json()["source"]["id"]
    variants = [dict(id="clean", src=src, start=0.2, end=min(dur - 0.2, 30), text=SAMPLE, denoise=True)]
    music = os.path.isfile(DEMUCS)
    if music:
        # «музыка»: аккорд с ритмом, громкостью около речи
        t = np.arange(len(clean)) / SR
        chord = sum(np.sin(2 * np.pi * f * t) for f in (110, 138.6, 164.8, 220))
        beat = (0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 2 * t))).astype(np.float32)
        mix = clean + 0.06 * chord * beat
        r = api.upload("/api/draft/upload", make_wav(np.clip(mix, -1, 1).astype(np.float32)), "music.wav")
        assert r.json()["ok"], r.text
        variants.append(dict(id="music", src=r.json()["source"]["id"], start=0.2, end=min(dur - 0.2, 30),
                             text=SAMPLE, denoise=True, music=True))
    api.ok("/api/draft", dict(variants=variants))
    phrase = api.state()["settings"]["test_phrases"][0]
    try:
        for v in variants:
            t0 = time.monotonic()
            r = api.get(f"/api/draft/synth?variant={v['id']}&phrase=0", timeout=600)
            assert r.status == 200, r.text
            y = decode(r.body)
            report(f"редактор, вариант {v['id']}: {len(y) / SR:.1f} с звука за {time.monotonic() - t0:.1f} с "
                   f"(с извлечением голоса{' и очисткой от музыки' if v.get('music') else ''})")
            real.check_speech(y, phrase, report, f"редактор, вариант {v['id']}")
        # со встроенным сервером голос сохраняется в <голоса>/<набор>/ с manifest.json набора
        r = api.post("/api/draft/save", dict(variant="clean", name="pudge-copy", set="e2e"), timeout=300)
        assert r.status == 200 and r.json()["ok"], r.text
        folder = os.path.join(real.server.dir, "voices", "e2e")
        assert os.path.getsize(os.path.join(folder, "pudge-copy.gguf")) > 10000
        with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
            assert json.load(f) == {"pudge-copy": {"source": "editor"}}
        assert "pudge-copy" in [v["name"] for v in api.state()["voices"]]
    finally:
        api.ok("/api/draft/discard")
    if not music:
        pytest.skip(f"очистка от музыки не проверена: нет {DEMUCS}")


def test_cpu(real, report):
    """Тот же синтез на процессоре (смена устройства в админке перезапускает сервер). Последним: сервер
    перезапускается."""
    t0, pid = time.monotonic(), real.runtime()["pid"]

    def ready():   # новый процесс сервера ответил на /status
        rt = real.runtime()
        return rt["pid"] not in (None, pid) and rt["state"] == "running" and rt["backend"] == "CPU"
    real.api.ok("/api/backend", dict(backend="cpu"))
    wait_for(ready, 300, f"сервер на CPU\n{real.server.tail()}", step=0.5)
    report(f"перезапуск на CPU: {time.monotonic() - t0:.1f} с")
    real.idle()
    if os.path.isdir(FRONTEND):   # голос, сохранённый в редакторе, загружен с диска после перезапуска
        assert real.runtime()["voices"] == 2
    x, _ = real.speak(SHORT, report, "CPU")
    real.check_speech(x, SHORT, report, "CPU")
