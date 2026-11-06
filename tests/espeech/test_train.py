"""Обучение LoRA (espeech/train.py, tools/espeech/train.py) с настоящей моделью: несколько шагов на паре реплик.
Пропускается без models/espeech или образцов voice-refs/cosyvoice/wc3/wc3-jaina и wc3-arthas (.wav + .txt).
Запуск: espeech/.venv/Scripts/python -m pytest tests/espeech/test_train.py (~1 мин; видеокарта, если есть).

Проверяется: шаги идут и лосс конечный, остановка по флагу и продолжение, gradient checkpointing даёт тот же лосс,
после обучения модель — в состоянии синтеза (база без requires_grad, синтез без адаптера и с нулевым адаптером бит в
бит как до обучения), обученный адаптер грузится реестром голосов и меняет звук; CLI пишет адаптер в файл голоса,
и espeech-server синтезирует им."""
import os, subprocess, sys, threading

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
MODELS = os.path.join(ROOT, "models", "espeech")
REFS = [os.path.join(ROOT, "voice-refs", "cosyvoice", "wc3", n) for n in ("wc3-jaina", "wc3-arthas")]

pytestmark = pytest.mark.skipif(not (os.path.isfile(os.path.join(MODELS, "espeech_fp16.safetensors")) and
                                     all(os.path.isfile(r + ".wav") and os.path.isfile(r + ".txt") for r in REFS)),
                                reason="нет models/espeech или образцов")


def read(path, sec=None):
    cmd = ["ffmpeg", "-v", "error", "-i", path] + (["-t", str(sec)] if sec else []) + \
        ["-ac", "1", "-ar", "24000", "-f", "f32le", "-"]
    return np.frombuffer(subprocess.run(cmd, check=True, capture_output=True).stdout, np.float32).copy()


@pytest.fixture(scope="module")
def tts():
    import torch
    from espeech import lora as L, model as M
    torch.backends.cudnn.enabled = False   # как в сервере
    t = M.ESpeech(MODELS, "cuda" if torch.cuda.is_available() else "cpu")
    yield t, L.Adapters(t.dit)
    del t
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


@pytest.fixture(scope="module")
def pairs():
    return [(read(r + ".wav"), open(r + ".txt", encoding="utf-8").read().strip()) for r in REFS]


def synth(t, ad, voice, key=None, lora=None, meta=None):
    ad.use(key, lora, meta)
    try:
        return t.sample(voice.cond, voice.ref_len, voice.text + "Пр+оверка обуч+ения.", 120, steps=12, seed=3).cpu()
    finally:
        ad.use(None)


def zeros(rank=16):
    import torch
    out = {}
    for i in range(22):
        for n, (inf, outf) in dict(q=(1024, 1024), k=(1024, 1024), v=(1024, 1024), o=(1024, 1024),
                                   ff1=(1024, 2048), ff2=(2048, 1024)).items():
            out[f"lora.{i}.{n}.A"] = torch.randn(rank, inf, dtype=torch.float16) * 0.02
            out[f"lora.{i}.{n}.B"] = torch.zeros(outf, rank, dtype=torch.float16)
    return out


def test_train_keeps_synthesis(tts, pairs, tmp_path):
    import torch
    from espeech import train as T, voices as V
    t, ad = tts
    ref = read(REFS[0] + ".wav", 6)
    path = tmp_path / "jaina.espeech"
    V.save(str(path), ref, "П+омни, Тр+алл!")
    reg = V.Registry(t, str(tmp_path))
    voice = reg.get("jaina")
    base = synth(t, ad, voice)
    assert torch.equal(base, synth(t, ad, voice, "zero", zeros(), dict(lora_rank=16, lora_alpha=16)))

    cfg = T.Config(steps=6, lr=1e-2, warmup=1, val_every=3, log_every=3)
    tr = T.Trainer(t, ad, T.prepare(t, pairs[:1]), T.prepare(t, pairs[1:]), cfg)
    v0 = tr.validate()
    stop = threading.Event()
    recs = []
    for r in tr.run(stop):
        recs.append(r)
        if r["step"] == 2:
            stop.set()
            mid = synth(t, ad, voice)   # синтез между шагами — как без обучения
            assert torch.equal(base, mid)
    assert [r["step"] for r in recs] == [1, 2] and not tr.done
    recs += list(tr.run())
    assert [r["step"] for r in recs] == [1, 2, 3, 4, 5, 6] and tr.done
    assert all(np.isfinite(r["loss"]) and r["sec"] > 0 for r in recs)
    assert np.isfinite(v0) and np.isfinite(recs[2]["val"]) and np.isfinite(recs[5]["val"])
    assert [h["step"] for h in tr.history["train"]] == [3, 6] and [h["step"] for h in tr.history["val"]] == [0, 3, 6]

    tensors, meta = tr.adapter()
    assert len(tensors) == 22 * 6 * 2 and all(x.dtype == torch.float16 and x.device.type == "cpu"
                                               for x in tensors.values())
    assert meta["lora_rank"] == 16 and meta["lora_steps"] == 6
    assert any(float(x.abs().max()) > 0 for k, x in tensors.items() if k.endswith(".B"))
    tr.close()

    # модель — в состоянии синтеза: база заморожена, обёртки без обучаемых параметров, звук бит в бит
    assert not any(p.requires_grad or p.grad is not None for p in t.dit.parameters())
    assert all(m.fit is None for m in ad.mods.values())
    assert torch.equal(base, synth(t, ad, voice))
    assert torch.equal(base, synth(t, ad, voice, "zero2", zeros(), dict(lora_rank=16, lora_alpha=16)))

    # обученный адаптер — в файл голоса, реестр его читает, звук другой
    V.save(str(tmp_path / "jaina-lora.espeech"), ref, "П+омни, Тр+алл!", meta=meta, lora=tensors)
    v2 = reg.get("jaina-lora")
    lora, md = reg.lora_of(v2)
    assert v2.has_lora and md["lora_steps"] == "6"
    trained = synth(t, ad, v2, v2, lora, md)
    assert not torch.equal(base, trained) and torch.isfinite(trained).all()


def test_checkpointing_same_loss(tts, pairs):
    from espeech import train as T
    t, ad = tts
    losses = []
    for ckpt in (False, True):
        tr = T.Trainer(t, ad, T.prepare(t, pairs), (), T.Config(steps=2, checkpointing=ckpt))
        losses.append([r["loss"] for r in tr.run()])
        tr.close()
    assert losses[0][0] == losses[1][0], losses          # тот же forward и тот же dropout при пересчёте
    assert abs(losses[0][1] - losses[1][1]) < 1e-3, losses


def test_cli_voice_in_server(tts, pairs, tmp_path):
    """tools/espeech/train.py: пары wav+txt (текст без «+» — ударения ставит RUAccent) → адаптер в новом файле
    голоса; espeech-server синтезирует им."""
    import wave
    from espeech import voices as V
    from test_server import Server
    data, voices = tmp_path / "data", tmp_path / "voices"
    data.mkdir()
    for r in REFS:
        name = os.path.basename(r)
        x = read(r + ".wav")
        with wave.open(str(data / (name + ".wav")), "wb") as w:   # 44,1 кГц стерео: CLI приводит через ffmpeg
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(44100)
            y =np.repeat(np.interp(np.arange(0, len(x), 24000 / 44100), np.arange(len(x)), x)[:, None], 2, 1)
            w.writeframes((y * 32767).astype("<i2").tobytes())
        (data / (name + ".txt")).write_text(open(r + ".txt", encoding="utf-8").read(), encoding="utf-8")
    src = voices / "jaina.espeech"
    V.save(str(src), read(REFS[0] + ".wav", 6), "П+омни, Тр+алл!", meta=dict(title="Джайна"))
    out = voices / "wc3" / "jaina-lora.espeech"
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "espeech", "train.py"), str(data), str(src),
                        "--out", str(out), "--steps", "3", "--lr", "1e-2", "--log", str(tmp_path / "log.json")],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=600)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "реплик: 2" in p.stdout and "шагов 3" in p.stdout, p.stdout
    ref, md, lora = V.load(str(out))
    assert md["title"] == "Джайна" and md["lora_steps"] == "3" and len(lora) == 264
    assert V.load(str(src))[2] == {}   # исходный голос не тронут

    srv = Server(str(voices))
    try:
        assert {"jaina", "jaina-lora"} <= set(srv.get("/speaker"))
        a, b = srv.tts(text="Проверка адаптера.", voice="jaina", seed=1), srv.tts(text="Проверка адаптера.",
                                                                                   voice="jaina-lora", seed=1)
        assert len(b) > 12000 and not (len(a) == len(b) and np.array_equal(a, b))
    finally:
        srv.close()
