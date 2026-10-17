"""Синтез тестовых фраз вариантами образцов и их оценки — для страницы pick.py.

usage: .venv/Scripts/python.exe tools/voices/synth.py [набор ...] [--voice <голос> ...] [--phrases N] [--score-only]
  без наборов — все из voice-clips/*/set.json; --phrases — сколько первых тестовых фраз озвучить (по умолчанию 3);
  --score-only — только досчитать оценки уже готового синтеза (сервер не нужен).

Каждый вариант, у которого нет синтеза, регистрируется на сервере как голос и озвучивает тестовые фразы набора
(set.json, перед ними — phrase голоса, например демо-текст JeetBot) с одним seed, так разница остаётся в образце,
а не в случайности. Если у варианта есть post, эффект применяется к синтезу. Затем оценки (ab/scores.json набора):
  cer   доля ошибок по буквам в расшифровке синтеза Whisper: невнятность, лишние звуки, обрывы
  sim   сходство синтеза с голосом в целом — центром эмбеддингов CAM++ реплик голоса (pool голоса в alts.json
        или pools.json набора, у WC3 его пишет cut-clips.py); psim — то же для самого образца
  rsim  сходство синтеза с эталоном ref (пример JeetBot): «похоже ли на то, что мы хотим повторить»
  эмоции образца и синтеза — в общем кеше voice-clips/emotions.json (emotions.py)
Оценка пересчитывается, только если файл изменился. Разница sim меньше 0,02 — в пределах шума.

Нужны cosyvoice-server на localhost:8080, numpy, onnxruntime, faster-whisper, torch, transformers, ffmpeg и curl.
Запросы к серверу идут строго по одному: параллельную регистрацию голосов он не выдерживает, поэтому
synth.py нельзя запускать одновременно с scripts/import-voices.sh и с синтезом в stream-tts.
"""
import glob, importlib.util, json, os, re, site, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # tools/<группа>/ -> корень проекта


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pick = module("pick", os.path.join(HERE, "pick.py"))
campplus = module("campplus", os.path.join(HERE, "campplus.py"))
emotions = module("emotions", os.path.join(HERE, "emotions.py"))

API = "http://127.0.0.1:8080"   # не localhost: из Python на Windows он сначала пробует IPv6 (+2 с на запрос)
SEED = 7
NAME = "ab-voice"


def curl(*args):
    r = subprocess.run(["curl", "-s", "-f", *args], capture_output=True)
    if r.returncode:
        raise RuntimeError(f"curl {args[:3]}: {r.returncode} {r.stdout[:200]!r}")
    return r.stdout


def register(wav, text):
    subprocess.run(["curl", "-s", "-X", "DELETE", f"{API}/speaker/{NAME}"], capture_output=True)
    curl("-X", "POST", f"{API}/speaker?type=extract", "-F", f"name={NAME}", "-F", f"text={text}", "-F", f"audio=@{wav}")


def tts(text, dst):
    body = dict(text=text, voice=NAME, mode="zero-shot", response_format="wav", seed=SEED)
    data = curl("-X", "POST", f"{API}/tts", "-H", "Content-Type: application/json",
                "-d", json.dumps(body, ensure_ascii=False))
    with open(dst, "wb") as f:
        f.write(data)


def letters(text):
    return re.sub(r"[^а-я]", "", text.lower().replace("ё", "е"))


def cer(ref, hyp):
    """Доля ошибок по буквам (расстояние Левенштейна / длина эталона)."""
    a, b = letters(ref), letters(hyp)
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1] / max(len(a), 1)


class Whisper:
    def __init__(self):
        self.model = None

    def __call__(self, wav):
        if self.model is None:
            sp = site.getsitepackages()[-1]  # cuBLAS/cuDNN: из пакетов nvidia-* или из torch (cu124 везёт их в torch/lib)
            for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")) + glob.glob(os.path.join(sp, "torch", "lib")):
                os.add_dll_directory(d)
                os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
            from faster_whisper import WhisperModel
            try:
                self.model = WhisperModel("large-v3", device="cuda", compute_type="float16")
            except Exception as e:
                print("CUDA недоступна, Whisper на CPU:", e, file=sys.stderr)
                self.model = WhisperModel("large-v3", device="cpu", compute_type="int8")
        segs = self.model.transcribe(wav, language="ru", beam_size=1, condition_on_previous_text=False)[0]
        return " ".join(s.text.strip() for s in segs)


def pool(s, voice, g, pools):
    paths = [p for pat in g.get("pool", []) for p in sorted(glob.glob(os.path.join(pick.ROOT, pat)))]
    return paths or [os.path.join(pick.ROOT, p) for p in pools.get(voice, [])]


def synthesize(s, voices, n_max):
    alts = s.alts()
    for voice, g in alts.items():
        if voices and voice not in voices:
            continue
        ph = s.phrases(g)[:n_max]
        for aid, meta in g["alts"].items():
            wav, txt = s.sample(voice, aid, meta)
            todo = [(n, p) for n, p in enumerate(ph, 1) if not os.path.exists(s.synth(voice, aid, meta, n))]
            if not todo or not os.path.exists(wav):
                continue
            register(wav, open(txt, encoding="utf-8").read().strip())
            try:
                for n, p in todo:
                    print(f"синтез {s.name}/{voice} {aid}: фраза {n}", flush=True)
                    dst = s.synth(voice, aid, meta, n)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    post = meta.get("post")
                    tts(p, dst + ".raw.wav" if post else dst)
                    if post:  # эффект поверх синтеза, например effects/villager-fx.py --mode cut
                        subprocess.run([sys.executable, os.path.join(pick.ROOT, post[0]), dst + ".raw.wav", dst,
                                        *(x.replace("{text}", p) for x in post[1:])], check=True)
                        os.remove(dst + ".raw.wav")
            finally:
                subprocess.run(["curl", "-s", "-X", "DELETE", f"{API}/speaker/{NAME}"], capture_output=True)


def score(s, voices, whisper, embed, emo):
    alts = s.alts()
    scores = pick.load(s.scores_path, {})
    pools = pick.load(os.path.join(s.dir, "pools.json"), {})
    try:
        for voice, g in alts.items():
            if voices and voice not in voices:
                continue
            ph = s.phrases(g)
            paths = pool(s, voice, g, pools)
            center = embed.center(paths) if paths else None
            ref = os.path.join(pick.ROOT, g["ref"]) if g.get("ref") else None
            ref_emb = embed(ref) if ref and os.path.exists(ref) else None
            for aid, meta in g["alts"].items():
                wav = s.sample(voice, aid, meta)[0]
                if not os.path.exists(wav):
                    continue
                sc = scores.setdefault(voice, {}).setdefault(aid, {})
                emo(wav)
                smp = pick.fresh(sc.get("sample"), wav) or {"stamp": pick.stamp(wav)}
                if center is not None and smp.get("psim") is None:
                    smp["psim"] = round(float(embed(wav) @ center), 3)
                sc["sample"] = smp
                for n, phrase in enumerate(ph, 1):
                    path = s.synth(voice, aid, meta, n)
                    if not os.path.exists(path):
                        continue
                    f = pick.fresh(sc.get("synth", {}).get(str(n)), path) or {"stamp": pick.stamp(path)}
                    if f.get("cer") is None:
                        print(f"оценка {s.name}/{voice} {aid}: фраза {n}", flush=True)
                        f["cer"] = round(cer(phrase, whisper(path)), 3)
                    if center is not None and f.get("sim") is None:
                        f["sim"] = round(float(embed(path) @ center), 3)
                    if ref_emb is not None and f.get("rsim") is None:
                        f["rsim"] = round(float(embed(path) @ ref_emb), 3)
                    emo(path)
                    sc.setdefault("synth", {})[str(n)] = f
            pick.save(s.scores_path, scores)
            embed.save()
            emo.save()
    finally:
        pick.save(s.scores_path, scores)


def main():
    args = sys.argv[1:]
    voices, n_max, score_only = [], pick.LISTEN, False
    while "--voice" in args:
        i = args.index("--voice")
        voices.append(args[i + 1])
        del args[i:i + 2]
    if "--phrases" in args:
        i = args.index("--phrases")
        n_max = int(args[i + 1])
        del args[i:i + 2]
    if "--score-only" in args:
        args.remove("--score-only")
        score_only = True
    names = args or pick.sets()
    unknown = set(names) - set(pick.sets())
    if unknown:
        sys.exit(f"Нет наборов: {' '.join(sorted(unknown))}. Есть: {' '.join(pick.sets())}")
    if not score_only:
        try:
            curl(f"{API}/ping")
        except RuntimeError:
            sys.exit(f"Сервер {API} не отвечает (docker compose up -d)")
        for name in names:
            synthesize(pick.Set(name), voices, n_max)
    whisper, embed, emo = Whisper(), campplus.Embedder(), emotions.Emotions()
    try:
        for name in names:
            score(pick.Set(name), voices, whisper, embed, emo)
    finally:
        embed.save()
        emo.save()
    print("Готово", flush=True)


if __name__ == "__main__":
    main()
