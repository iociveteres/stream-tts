"""Обучение LoRA голоса ESpeech на репликах одного голоса и запись адаптера в файл голоса .espeech.

Ядро обучения — espeech/train.py (устройство и гиперпараметры — там и в docs/espeech.md, «LoRA»): rank 16, alpha 16,
132 слоя DiT, потеря CFM f5-tts, AdamW lr 1e-4, батч до 3000 кадров мела. Шагов по умолчанию — по объёму
(espeech/train.py, default_steps): 1000 на минуту реплик, от 500 до 3000 (1 мин — 1000, от 3 мин — 3000; 3000 шагов — ~15 мин
на RTX 3060 Ti); пик видеопамяти ~3,4 ГБ, с --checkpointing ~1,1 ГБ и в 1,4 раза медленнее. Тот же код подготовки
реплик и записи адаптера зовёт сервер (POST /train, docs/espeech.md).

usage: train.py <реплики> <голос.espeech> [--out <другой.espeech>] [--val N | --val-dir <папка>]
                [--steps N] [--save-every 500] [--snapshots 1000,2000] [--checkpointing] [--log <лог.json>]
                [--rank 16] [--alpha 16] [--lr 1e-4] [--frames 3000] [--seed 1] [--models models/espeech]
                [--device auto|cuda|cpu] [--wav <запись> --text "<что звучит>" [--start С --end С]]
  <реплики>    папка пар <имя>.wav + <имя>.txt: текст ровно как звучит; без «+» он проходит путь фразы на синтезе —
               нормализатор (числа, латиница, знаки) и RUAccent (espeech/accent.py, полный режим), с «+» берётся как
               есть. Звук не 24 кГц моно PCM16 приводится через ffmpeg. Реплики короче 0,3 с и длиннее 30 с
               пропускаются (общие правила с сервером — espeech/pairs.py). Подпапка <реплики>/val/ (или --val-dir) —
               отложенные реплики для проверочного лосса; без неё в проверку уходит --val случайных реплик (по
               умолчанию — как у POST /train: от 30 реплик 10 %, от 5 до 12 и не меньше 20 с; меньше — без проверки).
  <голос>      существующий голос .espeech (tools/espeech/make-voice.py): образец и метаданные сохраняются,
               адаптер заменяется. Нового файла ещё нет — его сначала собирает make-voice.py из --wav/--text
               (--start/--end — кусок записи).
  --out        записать голос с адаптером в другой файл (образец — из <голос>).
  --save-every промежуточная запись адаптера в выходной файл (сервер подхватит его по времени изменения);
               в конце, по Ctrl+C (останавливается после текущего шага) и при ошибке лосса тоже пишется.
  --snapshots  шаги, на которых сохранить отдельную копию голоса <выход>-<шаг>.espeech (для сравнения на слух).
  --log        ход обучения в JSON: лосс, проверочный лосс, с/шаг, память (как lora/<голос>.log.json исследования).
Выход: файл голоса с тензорами lora.* (fp16) и метаданными lora_rank, lora_alpha, lora_steps (espeech/voices.py).
Нужен espeech/.venv (torch с CUDA), models/espeech (tools/espeech/prepare.py), ffmpeg в PATH."""
import argparse, glob, importlib.util, json, os, signal, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
from espeech import pairs as P, voices as V   # noqa: E402


def read_pairs(folder, accentor):
    """→ [(имя, звук, текст с ударениями)] по парам <имя>.wav + <имя>.txt."""
    from espeech import normalize as N
    out = []
    for wav in sorted(glob.glob(os.path.join(folder, "*.wav"))):
        stem = os.path.splitext(wav)[0]
        if not os.path.isfile(stem + ".txt"):
            print(f"  нет текста: {os.path.basename(wav)} — пропущено")
            continue
        text = P.clean_text(open(stem + ".txt", encoding="utf-8").read())
        if not text:
            continue
        try:
            audio = P.read_audio(wav)
        except ValueError as e:
            print(f"  {os.path.basename(wav)}: {e} — пропущено")
            continue
        bad = P.check_audio(audio)
        if bad:
            print(f"  {os.path.basename(wav)}: {bad} — пропущено")
            continue
        text = P.prepare_text(text, lambda t: accentor()(t), lambda t: N.normalize(t, accentor().lexicon))
        if text:
            out.append((os.path.basename(stem), audio, text))
    return out


def make_voice(path, a):
    """Новый голос из записи — make-voice.py (тот же кусок, ударения и темп)."""
    spec = importlib.util.spec_from_file_location("make_voice", os.path.join(HERE, "make-voice.py"))
    mv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mv)
    mv.make(path, a.wav, a.text, a.start, a.end)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("data", help="папка пар <имя>.wav + <имя>.txt")
    ap.add_argument("voice", help="голос .espeech: образец берётся из него, адаптер пишется в него (или в --out)")
    ap.add_argument("--out", help="записать в другой файл .espeech")
    ap.add_argument("--val", type=int, help="сколько реплик отложить для проверки (по умолчанию — espeech/pairs.py)")
    ap.add_argument("--val-dir", help="папка отложенных пар (по умолчанию <реплики>/val, если есть)")
    ap.add_argument("--steps", type=int, help="шагов (по умолчанию по объёму: 1000 на минуту реплик, от 500 до 3000)")
    ap.add_argument("--save-every", type=int, default=500)
    ap.add_argument("--snapshots", default="", help="шаги через запятую: копии <выход>-<шаг>.espeech")
    ap.add_argument("--checkpointing", action="store_true", help="gradient checkpointing блоков DiT (меньше памяти)")
    ap.add_argument("--log", help="ход обучения в JSON")
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=float, default=16)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--frames", type=int, default=3000, help="кадров мела в батче с паддингом (93,75 кадра/с)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--models", default=os.path.join(ROOT, "models", "espeech"))
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    ap.add_argument("--wav", help="новый голос: запись образца (make-voice.py)")
    ap.add_argument("--text", help="новый голос: что звучит в куске")
    ap.add_argument("--start", type=float)
    ap.add_argument("--end", type=float)
    a = ap.parse_args()

    out = a.out or a.voice
    if not os.path.isfile(a.voice):
        if not (a.wav and a.text):
            ap.error(f"{a.voice}: нет такого голоса — соберите его make-voice.py или передайте --wav и --text")
        make_voice(a.voice, a)

    import torch
    from espeech import lora as L, model as M, train as T

    acc = []

    def accentor():
        if not acc:
            from espeech.accent import Accentor
            acc.append(Accentor(os.path.join(a.models, "ruaccent")))
        return acc[0]

    pairs = read_pairs(a.data, accentor)
    val_dir = a.val_dir or (os.path.join(a.data, "val") if os.path.isdir(os.path.join(a.data, "val")) else None)
    if val_dir:
        val = read_pairs(val_dir, accentor)
    else:
        pairs, val = P.split_val(pairs, a.seed, a.val)
    if not pairs:
        raise SystemExit(f"{a.data}: нет пар <имя>.wav + <имя>.txt")
    minutes = sum(len(x) for _, x, _ in pairs) / 24000 / 60
    print(f"реплик: {len(pairs)} ({minutes:.1f} мин), в проверке: {len(val)}", flush=True)

    torch.backends.cudnn.enabled = False   # как в сервере (без cuDNN синтез на 20 % быстрее)
    dev = a.device if a.device != "auto" else ("cuda" if torch.cuda.is_available() else "cpu")
    t = time.perf_counter()
    tts = M.ESpeech(a.models, dev)
    adapters = L.Adapters(tts.dit)
    print(f"модель на {dev} за {time.perf_counter() - t:.1f} с", flush=True)
    train = T.prepare(tts, [(x, s) for _, x, s in pairs])
    steps = a.steps or T.default_steps([i.frames for i in train])
    print(f"шагов: {steps}" + ("" if a.steps else " (по объёму)"), flush=True)
    cfg = T.Config(steps=steps, rank=a.rank, alpha=a.alpha, lr=a.lr, frames=a.frames, seed=a.seed,
                   checkpointing=a.checkpointing)
    tr = T.Trainer(tts, adapters, train, T.prepare(tts, [(x, s) for _, x, s in val]), cfg)
    snaps = {int(s) for s in a.snapshots.split(",") if s.strip()}
    log = dict(args=vars(a), train_items=len(pairs), train_min=round(minutes, 2), val_items=len(val),
               history=tr.history)

    def dump():
        if a.log:
            with open(a.log, "w", encoding="utf-8") as f:
                json.dump(log, f, ensure_ascii=False, indent=1)

    def save(path):
        tensors, meta = tr.adapter()
        meta["lora_data_sec"] = f"{tr.train_sec:.1f}"
        V.save_adapter(a.voice, path, tensors, meta)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: (print("остановка после текущего шага…", flush=True), stop.set(),
                                              signal.signal(signal.SIGINT, signal.SIG_DFL)))
    v0 = tr.validate()
    print(f"шаг 0: проверка {v0}", flush=True)
    dump()
    t_all = time.perf_counter()
    try:
        for rec in tr.run(stop):
            s = rec["step"]
            if s % cfg.log_every == 0 or tr.done:
                h = tr.history["train"][-1]
                print(f"шаг {s}: лосс {h['loss']}, {h['s_per_step']} с/шаг, пик {h.get('peak_mb', 0)} МБ"
                      + (f", проверка {rec['val']}" if rec.get("val") is not None else ""), flush=True)
            elif rec.get("val") is not None:
                print(f"шаг {s}: проверка {rec['val']}", flush=True)
            if s in snaps:
                root, ext = os.path.splitext(out)
                save(f"{root}-{s}{ext}")
            if a.save_every and s % a.save_every == 0 and not tr.done:
                save(out)
            if rec.get("val") is not None:
                dump()
    finally:
        if tr.step_no:
            save(out)
            log["steps_done"] = tr.step_no
            log["minutes"] = round((time.perf_counter() - t_all) / 60, 1)
            dump()
            print(f"{out}: адаптер, шагов {tr.step_no}, {log['minutes']} мин", flush=True)
        tr.close()


if __name__ == "__main__":
    main()
