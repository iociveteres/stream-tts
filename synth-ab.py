"""A/B кандидатов в образцы: синтез одинаковых тестовых фраз и автоматическая оценка.

usage: python synth-ab.py <batch> [voice_id ...]

Каждый кандидат партии (voice-clips/wc3/<batch>/clips.json) регистрируется на сервере как голос
и озвучивает фразы TEST с фиксированным seed. Оценки:
  psim  сходство самого образца с голосом персонажа в целом
  sim   сходство синтеза с голосом персонажа в целом (среднее по фразам) — главная оценка
  cer   доля ошибок по буквам в расшифровке синтеза Whisper: невнятность, лишние звуки, обрывы
«Голос в целом» — центр эмбеддингов CAM++ до POOL реплик голоса (cut-clips.voice_pool),
сходство — косинус с ним. Эмбеддинги считаются локально той же моделью CAM++, что у сервера
(models/frontend-onnx/campplus*.onnx), и кешируются в voice-clips/wc3/embeddings.json.

Сначала все кандидаты озвучивают первые FIRST фраз, остальные фразы — только те, у кого sim не
дальше CLOSE от лучшего в голосе: различия больше этого видны и по первым фразам.
Оценка фразы (эмбеддинг, Whisper) идёт параллельно с синтезом следующей.

Результат в voice-clips/wc3/<batch>/ab/: <cid>.<n>.wav, ab.json, ab.txt (таблица по голосам).
Нужны cosyvoice-server на localhost:8080 (docker compose), numpy, onnxruntime, faster-whisper,
ffmpeg и curl. Запросы к серверу идут строго по одному: параллельную регистрацию голосов он не выдерживает.
"""
import concurrent.futures, glob, importlib.util, json, os, random, subprocess, sys, tempfile

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("cut_clips", os.path.join(ROOT, "cut-clips.py"))
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

API = "http://localhost:8080"
EMB = os.path.join(cc.CLIPS, "embeddings.json")
# Тестовые фразы: повествование, приказ, вопрос, крик, короткий ответ, длинная фраза с запятыми.
TEST = [
    "Работа никогда не заканчивается.",
    "Мы выступаем на рассвете. Соберите всех, кто способен держать оружие.",
    "Ты правда думаешь, что это сработает?",
    "Враги уже близко! К оружию!",
    "Я не знаю, что ждёт нас за этими горами, но назад дороги уже нет.",
    "Хорошо, я сделаю это.",
    "Когда-то здесь стоял город, а теперь остались только руины и пепел.",
    "Не стой у меня на пути!",
]
SEED = 7
FIRST = 4      # фраз в первом проходе
CLOSE = 0.03   # кто ближе к лучшему, озвучивает все фразы
POOL = 60      # реплик на «голос в целом»: центр уже устойчив, а считать быстрее


def curl(*args):
    r = subprocess.run(["curl", "-s", "-f", *args], capture_output=True)
    if r.returncode:
        raise RuntimeError(f"curl {args[:3]}: {r.returncode} {r.stdout[:200]!r}")
    return r.stdout


def register(name, wav, text):
    curl("-X", "POST", f"{API}/speaker?type=extract", "-F", f"name={name}",
         "-F", f"text={text}", "-F", f"audio=@{wav}")


def unregister(name):
    subprocess.run(["curl", "-s", "-X", "DELETE", f"{API}/speaker/{name}"], capture_output=True)


def tts(voice, text, dst):
    body = dict(text=text, voice=voice, mode="zero-shot", response_format="wav", seed=SEED)
    open(dst, "wb").write(curl("-X", "POST", f"{API}/tts", "-H", "Content-Type: application/json",
                               "-d", json.dumps(body, ensure_ascii=False)))


def mel_banks(n_mels=80, n_fft=512, sr=16000, low=20.0):
    """Треугольные мел-фильтры как в kaldi (torchaudio.compliance.kaldi.fbank)."""
    mel = lambda f: 1127.0 * np.log(1.0 + f / 700.0)
    lo, hi = mel(low), mel(sr / 2)
    centers = lo + (hi - lo) / (n_mels + 1) * np.arange(n_mels + 2)
    fft_mel = mel(sr / n_fft * np.arange(n_fft // 2))
    left, center, right = centers[:-2, None], centers[1:-1, None], centers[2:, None]
    banks = np.maximum(0.0, np.minimum((fft_mel - left) / (center - left), (right - fft_mel) / (right - center)))
    return np.pad(banks, ((0, 0), (0, 1)))  # бин Найквиста с нулевым весом


BANKS = mel_banks()
WINDOW = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 399)) ** 0.85  # окно Пови


def fbank(x):
    """80 лог-мел полос, кадр 25 мс, шаг 10 мс, минус среднее по времени — вход CAM++ в CosyVoice."""
    n = 1 + (len(x) - 400) // 160
    frames = x[np.arange(400)[None, :] + 160 * np.arange(n)[:, None]].astype(np.float64)
    frames -= frames.mean(1, keepdims=True)
    frames = np.concatenate([frames[:, :1] * 0.03, frames[:, 1:] - 0.97 * frames[:, :-1]], 1)
    power = np.abs(np.fft.rfft(frames * WINDOW, 512)) ** 2
    feat = np.log(np.maximum(power @ BANKS.T, np.finfo(np.float32).eps))
    return (feat - feat.mean(0)).astype(np.float32)


class Embedder:
    """Эмбеддинги CAM++ локально, с кешем по ключу (путь исходника или файла синтеза)."""

    def __init__(self):
        import onnxruntime
        model = sorted(glob.glob(os.path.join(ROOT, "models", "frontend-onnx", "campplus*.onnx")))[0]
        self.session = onnxruntime.InferenceSession(model, providers=["CPUExecutionProvider"])
        self.cache = cc.load_json(EMB, {})

    def __call__(self, key, wav):
        if key not in self.cache:
            # как на сервере: сначала 24 кГц (так режутся образцы), затем 16 кГц для CAM++
            pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", wav, "-ac", "1", "-ar", "24000", "-f", "wav", "-"],
                                 capture_output=True, check=True).stdout
            raw = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-ar", "16000", "-f", "f32le", "-"],
                                 input=pcm, capture_output=True, check=True).stdout
            feat = fbank(np.frombuffer(raw, np.float32))
            emb = self.session.run(None, {"input": feat[None]})[0][0]
            self.cache[key] = [round(float(x), 5) for x in emb]
        return unit(np.array(self.cache[key]))

    def save(self):
        cc.save_json(EMB, self.cache)


def unit(v):
    return v / np.linalg.norm(v)


def cer(ref, hyp):
    """Доля ошибок по буквам (расстояние Левенштейна / длина эталона)."""
    a, b = cc.letters(ref), cc.letters(hyp)
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        prev, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (x != y))
    return row[-1] / max(len(a), 1)


def synth(cid, wav, text, phrases, abdir, batch, embed, pieces, center):
    """Озвучивает фразы (номер, текст) кандидатом; оценка фразы идёт, пока синтезируется следующая."""

    def score(n, phrase, dst):
        segs = pieces.whisper().transcribe(dst, language="ru", beam_size=1, condition_on_previous_text=False)[0]
        hyp = " ".join(s.text.strip() for s in segs)
        return n, float(embed(f"{batch}/ab/{cid}.{n}", dst) @ center), round(cer(phrase, hyp), 3)

    register("ab-voice", wav, text)
    try:
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            jobs = []
            for n, phrase in phrases:
                dst = os.path.join(abdir, f"{cid}.{n}.wav")
                tts("ab-voice", phrase, dst)
                jobs.append(pool.submit(score, n, phrase, dst))
            return [j.result() for j in jobs]
    finally:
        unregister("ab-voice")


def main():
    batch = sys.argv[1]
    only = set(sys.argv[2:])
    out = os.path.join(cc.CLIPS, batch)
    rows = cc.load_json(os.path.join(out, "clips.json"), {})
    ratings = cc.parse_ratings(out)
    abdir = os.path.join(out, "ab")
    os.makedirs(abdir, exist_ok=True)
    results = cc.load_json(os.path.join(abdir, "ab.json"), {})
    cc.save_json(os.path.join(abdir, "phrases.json"), TEST)  # тексты <id>.<n>.wav для pick.py
    tmp = tempfile.mkdtemp()
    pieces = cc.Pieces(tmp)
    embed = Embedder()
    try:
        for vid in sorted({r["voice"] for r in rows.values()}):
            if only and vid not in only:
                continue
            pool = cc.voice_pool(pieces, vid)
            pool = random.Random(vid).sample(pool, min(POOL, len(pool)))
            center = unit(np.mean([embed(rel, pieces.wav(rel)) for rel in pool], axis=0))
            embed.save()
            print(f"== {vid}: голос в целом — {len(pool)} реплик", flush=True)
            cands = {}
            for cid, r in rows.items():
                if r["voice"] != vid:
                    continue
                wav = os.path.join(out, vid, cid + ".wav")
                text = open(os.path.join(out, vid, cid + ".txt"), encoding="utf-8").read().strip()
                cands[cid] = dict(wav=wav, text=text, psim=float(embed(f"{batch}/{cid}", wav) @ center),
                                  scores=synth(cid, wav, text, list(enumerate(TEST, 1))[:FIRST],
                                               abdir, batch, embed, pieces, center))
            mean = lambda c: np.mean([s for _, s, _ in cands[c]["scores"]])
            best = max(mean(c) for c in cands)
            for cid, c in cands.items():
                if mean(cid) >= best - CLOSE:
                    c["scores"] += synth(cid, c["wav"], c["text"], list(enumerate(TEST, 1))[FIRST:],
                                         abdir, batch, embed, pieces, center)
                r = rows[cid]
                sims = [s for _, s, _ in c["scores"]]
                cers = [e for _, _, e in c["scores"]]
                results[cid] = dict(voice=vid, tag=r["tag"], dur=r["dur"], psim=round(c["psim"], 3),
                                    sim=round(float(np.mean(sims)), 3), cer=round(float(np.mean(cers)), 3),
                                    sims=[round(s, 3) for s in sims], cers=cers,
                                    rating=ratings.get(cid, ("", "", ""))[0],
                                    comment=ratings.get(cid, ("", "", ""))[2])
                print(f"  {cid:22s} psim {c['psim']:.3f}  sim {results[cid]['sim']:.3f}  "
                      f"cer {results[cid]['cer']:.3f}  фраз {len(sims)}", flush=True)
            embed.save()
            cc.save_json(os.path.join(abdir, "ab.json"), results)
    finally:
        embed.save()
        pieces.save()
    write_table(abdir, results)


def write_table(abdir, results):
    lines = ["# A/B кандидатов: одинаковые фразы (synth-ab.py TEST), seed %d.\n" % SEED,
             "# sim — сходство синтеза с голосом персонажа (выше лучше), cer — ошибки разборчивости (ниже лучше),\n",
             "# psim — сходство самого образца, n — сколько фраз озвучено (дальние от лучшего — только первые %d).\n" % FIRST,
             "# Разница sim меньше 0,02 — в пределах шума. Внутри голоса отсортировано по sim.\n"]
    for vid in sorted({r["voice"] for r in results.values()}):
        lines.append(f"\n# ===== {vid} =====\n")
        lines.append(f"# {'id':22s} {'тег':28s} {'длит':>5s} {'psim':>5s} {'sim':>5s} {'cer':>5s}  n  оценка  комментарий\n")
        for cid, r in sorted(((c, r) for c, r in results.items() if r["voice"] == vid), key=lambda x: -x[1]["sim"]):
            lines.append(f"{cid:24s} {r['tag']:28s} {r['dur']:5.1f} {r['psim']:5.3f} {r['sim']:5.3f} "
                         f"{r['cer']:5.3f} {len(r['sims']):2d}  {r['rating']:6s}  {r['comment']}\n")
    with open(os.path.join(abdir, "ab.txt"), "w", encoding="utf-8") as f:
        f.writelines(lines)


if __name__ == "__main__":
    main()
