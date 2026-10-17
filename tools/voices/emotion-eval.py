"""Сверка моделей эмоций с разметкой на слух: какая модель слышит подачу фразы так же, как человек.

usage: python tools/voices/emotion-eval.py [--port 8767]              страница разметки: http://localhost:8767
       .venv/Scripts/python.exe tools/voices/emotion-eval.py --score  прогнать модели по размеченным фразам
       python tools/voices/emotion-eval.py --report                   сводка текстом

Зачем. Модель эмоций (emotions.py) выбрана по 50 фразам WC3 с метками «спокойно» против «гнев/приказ/крик»: так
проверяются только крайности энергии, и все фразы — из одной игры. Здесь фразы идут вперемешку из разных источников
(SOURCES: WC3, STALKER, Dota и Portal, стримеры, синтез cosyvoice), а человек оценивает каждую по трём шкалам
1–5 и одной категории (EMOTIONS), не видя текста и имени файла:
  энергия — вяло … крик, напор — робко … властно, тон — злобно … радостно.
Модели (MODELS): msp-dim (audeering, оси arousal/dominance/valence прямо), Aniemore (wavlm, русский RESD,
7 категорий), emotion2vec+ large (9 категорий). У категориальных моделей оси — среднее координат категорий
на круге Рассела (CIRCUMPLEX) с весами вероятностей: грубо, зато одинаково для обеих.
Сводка: ρ Спирмена по каждой шкале с 95% интервалом, AUC энергии (1–2 против 4–5, как в прошлом сравнении),
совпадение категории (для сравнения — доля «нейтрально» в разметке: столько угадывает «всегда нейтрально»).

Данные: voice-clips/emotion-eval.json {pool: [путь…], ratings: {путь: {a, d, v, emo, bad, ts}}, dropped: [путь…]} —
        ручная разметка; dropped — выброшенные фразы (например, лишние «везде 3»), в очередь они больше не попадают;
        voice-clips/emotion-models.json {модель: {путь: {stamp, a, d, v} или {stamp, scores}}} — кеш выходов
        моделей (msp-dim заодно пишет в общий кеш emotions.json). Пути от корня проекта.
Страница и --report — только стандартная библиотека; --score — venv проекта (torch, transformers, funasr),
кнопка на странице запускает его сама. Модели работают на GPU по очереди, к cosyvoice-server не обращаются.
"""
import gc, glob, importlib.util, json, math, os, random, subprocess, sys, threading, time
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # tools/<группа>/ -> корень проекта
spec = importlib.util.spec_from_file_location("pick", os.path.join(HERE, "pick.py"))
pick = importlib.util.module_from_spec(spec)  # load/save, duration и http-обработчик review.py
spec.loader.exec_module(pick)
review = pick.review

DATA = os.path.join(ROOT, "voice-clips", "emotion-eval.json")
MODELS_CACHE = os.path.join(ROOT, "voice-clips", "emotion-models.json")
SOURCES = {  # группа -> глобы от корня; внутри группы сначала случайный голос (папка), потом фраза
    "wc3": None,  # реплики голосов из voice-clips/wc3/pools.json (их пишет cut-clips.py)
    "stalker": ["voice-origs/stalker-cop/sounds/characters_voice/**/*.ogg"],
    "games": ["voice-origs/dota2-pudge/*_ru.mp3", "voice-origs/portal-glados/*_ru.wav"],
    "streamers": ["voice-origs/***-*/*.wav", "voice-origs/***-altushka/*.wav"],
    "synth": ["voice-clips/*/ab/*.wav", "voice-clips/wc3/b0*/ab/*.wav"],
}
SKIP = ("/music/",)          # у STALKER рядом с речью лежат губная гармошка и гитара
DUR = (0.7, 15.0)            # короче — не успеть понять подачу, длиннее — подача внутри успевает смениться
BATCH = 20                   # столько фраз добавляется в очередь, когда неразмеченные кончаются
EMOTIONS = ["нейтрально", "злость", "радость", "грусть", "страх", "удивление", "отвращение", "другое"]
# Категории моделей -> общие. enthusiasm у Aniemore — та же радость, только живее.
LABELS = {"anger": "злость", "angry": "злость", "happiness": "радость", "happy": "радость",
          "enthusiasm": "радость", "sadness": "грусть", "sad": "грусть", "fear": "страх", "fearful": "страх",
          "surprised": "удивление", "disgust": "отвращение", "disgusted": "отвращение", "neutral": "нейтрально",
          "other": "другое", "<unk>": "другое"}
# Координаты категорий (энергия, напор, тон) в -1…1, по таблицам PAD (Russell & Mehrabian 1977) с округлением.
CIRCUMPLEX = {"нейтрально": (0, 0, 0), "злость": (0.8, 0.7, -0.7), "радость": (0.5, 0.4, 0.8),
              "грусть": (-0.6, -0.4, -0.6), "страх": (0.6, -0.6, -0.6), "удивление": (0.7, 0, 0.3),
              "отвращение": (0.2, 0.3, -0.6), "другое": (0, 0, 0)}
MODELS = {  # имя -> (подпись, модель на Hugging Face)
    "msp-dim": ("msp-dim (audeering), выбранная", "audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim"),
    "aniemore": ("Aniemore wavlm RESD", "Aniemore/wavlm-emotion-russian-resd"),
    "e2v": ("emotion2vec+ large", "emotion2vec/emotion2vec_plus_large"),
}
AXES = (("a", "энергия"), ("d", "напор"), ("v", "тон"))
PY = pick.PY
job = {"proc": None, "text": "", "log": []}


def load():
    return pick.load(DATA, {"pool": [], "ratings": {}})


# --- очередь фраз ---
def candidates():
    """{группа: {голос: [путь…]}} — все фразы источников."""
    out = {}
    pools = pick.load(os.path.join(ROOT, "voice-clips", "wc3", "pools.json"), {})
    out["wc3"] = {v: list(p) for v, p in pools.items() if p}
    for group, pats in SOURCES.items():
        if pats is None:
            continue
        voices = out.setdefault(group, {})
        for pat in pats:
            for path in glob.glob(os.path.join(ROOT, pat), recursive=True):
                r = pick.rel(path)
                if any(s in "/" + r for s in SKIP):
                    continue
                # голос синтеза — начало имени (wc3-arthas.b02.S00.1.wav), остальных — папка
                key = os.path.basename(r).split(".")[0] if group == "synth" else os.path.dirname(r)
                voices.setdefault(key, []).append(r)
    return {g: v for g, v in out.items() if v}


def extend(data, n=BATCH):
    """Добавить n фраз: группы по очереди (сначала те, которых в очереди меньше), голос и фраза — случайно."""
    cands, rnd, have = candidates(), random.Random(), set(data["pool"]) | set(data.get("dropped", []))
    count = {g: 0 for g in cands}
    for r in data["pool"]:
        g = group_of(r)
        if g in count:
            count[g] += 1
    added, tries = 0, 0
    while added < n and tries < n * 20:
        tries += 1
        g = min(count, key=lambda k: (count[k], rnd.random()))
        r = rnd.choice(rnd.choice(list(cands[g].values())))
        if r in have:
            continue
        try:
            if not DUR[0] <= pick.duration(os.path.join(ROOT, r)) <= DUR[1]:
                continue
        except (subprocess.CalledProcessError, ValueError):
            continue
        data["pool"].append(r)
        have.add(r)
        count[g] += 1
        added += 1
    return added


def group_of(r):
    if r.startswith("voice-clips/") and "/ab/" in r:
        return "synth"
    if r.startswith("voice-origs/Warcraft III"):
        return "wc3"
    for g, pats in SOURCES.items():
        if pats and any(r.startswith(p.split("*")[0]) for p in pats):
            return g
    return "?"


def next_item(data):
    todo = [r for r in data["pool"] if r not in data["ratings"]]
    if not todo:
        with review.lock:
            if extend(data):
                pick.save(DATA, data)
        todo = [r for r in data["pool"] if r not in data["ratings"]]
    return todo[0] if todo else None


# --- сводка ---
def ranks(xs):
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out, i = [0.0] * len(xs), 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):  # связанные значения (а в шкале 1–5 их много) — средний ранг
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(xs, ys):
    n = len(xs)
    if n < 5:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sx, sy = math.sqrt(sum((a - mx) ** 2 for a in rx)), math.sqrt(sum((b - my) ** 2 for b in ry))
    if not sx or not sy:
        return None
    r = max(min(sxy / sx / sy, 0.9999), -0.9999)
    se = math.sqrt(1.06 / (n - 3))  # Fieller и др. (1957): поправка для ρ Спирмена
    z = math.atanh(r)
    return dict(r=round(r, 3), lo=round(math.tanh(z - 1.96 * se), 2), hi=round(math.tanh(z + 1.96 * se), 2), n=n)


def auc(pos, neg):
    if not pos or not neg:
        return None
    s = sum((p > q) + 0.5 * (p == q) for p in pos for q in neg)
    return dict(auc=round(s / len(pos) / len(neg), 3), pos=len(pos), neg=len(neg))


def axes_of(out):
    """Выход модели -> ({a, d, v}, категория). Категориальные — через CIRCUMPLEX, оси в -1…1."""
    if "scores" not in out:
        return {k: out[k] for k in ("a", "d", "v")}, None
    probs = {}
    for lab, p in out["scores"].items():
        common = LABELS.get(lab.split("/")[-1], "другое")
        probs[common] = probs.get(common, 0) + p
    total = sum(probs.values()) or 1
    ax = {k: sum(p * CIRCUMPLEX[c][i] for c, p in probs.items()) / total for i, k in enumerate(("a", "d", "v"))}
    return ax, max(probs, key=probs.get)


def report(data=None):
    data = data or load()
    cache = pick.load(MODELS_CACHE, {})
    rated = {r: x for r, x in data["ratings"].items() if not x.get("bad")}
    emos = [x["emo"] for x in rated.values() if x.get("emo")]
    rows = []
    for name, (title, _) in MODELS.items():
        outs = cache.get(name, {})
        got = {r: axes_of(outs[r]) for r in rated if r in outs}
        row = dict(model=name, title=title, n=len(got), missing=len(rated) - len(got))
        for k, _ in AXES:
            pairs = [(rated[r][k], ax[k]) for r, (ax, _) in got.items() if rated[r].get(k)]
            row[k] = spearman([p[0] for p in pairs], [p[1] for p in pairs])
        row["auc"] = auc([ax["a"] for r, (ax, _) in got.items() if rated[r].get("a", 3) >= 4],
                         [ax["a"] for r, (ax, _) in got.items() if rated[r].get("a", 3) <= 2])
        hits = [cat == rated[r]["emo"] for r, (_, cat) in got.items() if cat and rated[r].get("emo")]
        row["cat"] = dict(hit=round(sum(hits) / len(hits), 3), n=len(hits)) if hits else None
        rows.append(row)
    groups = {}
    for r in rated:
        groups[group_of(r)] = groups.get(group_of(r), 0) + 1
    return dict(rows=rows, rated=len(rated), bad=len(data["ratings"]) - len(rated), groups=groups,
                neutral=round(emos.count("нейтрально") / len(emos), 3) if emos else None)


def print_report():
    rep = report()
    print(f"Размечено {rep['rated']} фраз (не годятся: {rep['bad']}), по источникам: "
          + ", ".join(f"{g} {n}" for g, n in sorted(rep["groups"].items())))
    fmt = lambda s: f"{s['r']:+.2f} [{s['lo']:+.2f}…{s['hi']:+.2f}]" if s else "—"
    print(f"{'модель':34s} {'энергия ρ':>19s} {'напор ρ':>19s} {'тон ρ':>19s} {'AUC энерг':>9s} {'категория':>9s}")
    for row in rep["rows"]:
        a = f"{row['auc']['auc']:.2f}" if row["auc"] else "—"
        c = f"{row['cat']['hit']:.0%}" if row["cat"] else "—"
        miss = f" (нет {row['missing']})" if row["missing"] else ""
        print(f"{row['title'] + miss:34s} {fmt(row['a']):>19s} {fmt(row['d']):>19s} {fmt(row['v']):>19s} {a:>9s} {c:>9s}")
    if rep["neutral"] is not None:
        print(f"«Всегда нейтрально» угадывает категорию в {rep['neutral']:.0%} случаев.")


# --- прогон моделей (venv) ---
def score():
    import numpy as np  # noqa: F401 — проверка, что запущено из venv проекта
    spec = importlib.util.spec_from_file_location("emotions", os.path.join(HERE, "emotions.py"))
    emotions = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(emotions)
    import torch
    data = load()
    paths = [r for r, x in data["ratings"].items() if not x.get("bad")]
    cache = pick.load(MODELS_CACHE, {})
    for name, (title, hf) in MODELS.items():
        outs = cache.setdefault(name, {})
        todo = [r for r in paths if outs.get(r, {}).get("stamp") != pick.stamp(os.path.join(ROOT, r))]
        if not todo:
            continue
        print(f"{title}: {len(todo)} фраз", flush=True)
        if name == "msp-dim":
            emo = emotions.Emotions()
            run = lambda path: {k: emo(path)[k] for k in ("a", "d", "v")}
        elif name == "aniemore":
            from transformers import pipeline
            clf = pipeline("audio-classification", model=hf, device=0 if torch.cuda.is_available() else -1,
                           trust_remote_code=True)
            run = lambda path: dict(scores={x["label"]: round(x["score"], 4) for x in
                                            clf({"raw": emotions.pcm(path)[:30 * emotions.SR], "sampling_rate": emotions.SR}, top_k=None)})
        else:
            from funasr import AutoModel
            m = AutoModel(model=hf, hub="hf", disable_update=True, device="cuda" if torch.cuda.is_available() else "cpu")

            def run(path):
                r = m.generate(emotions.pcm(path)[:30 * emotions.SR], granularity="utterance", extract_embedding=False)[0]
                return dict(scores={lab.split("/")[-1]: round(float(s), 4) for lab, s in zip(r["labels"], r["scores"])})
        for i, r in enumerate(todo, 1):
            path = os.path.join(ROOT, r)
            outs[r] = dict(stamp=pick.stamp(path), **run(path))
            print(f"{title}: {i}/{len(todo)}", flush=True)
        pick.save(MODELS_CACHE, cache)
        if name == "msp-dim":
            emo.save()
        run = m = clf = emo = None  # следующей модели нужна видеопамять, рядом работает cosyvoice-server
        gc.collect()
        torch.cuda.empty_cache()
    print("Готово", flush=True)


def start_score():
    if job["proc"] and job["proc"].poll() is None:
        return "модели уже считаются"
    job["proc"] = subprocess.Popen([PY, os.path.abspath(__file__), "--score"], cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    job["text"], job["log"] = "запуск моделей", []

    def follow(proc):
        for line in proc.stdout:
            if line.strip():
                job["text"] = line.strip()
                job["log"].append(line.strip())
        if proc.wait():
            job["log"].append(f"--score завершился с кодом {proc.returncode}")

    threading.Thread(target=follow, args=(job["proc"],), daemon=True).start()


# --- страница ---
def state(want=None):
    data = load()
    item = want if want in data["pool"] else next_item(data)
    recent = sorted(data["ratings"].items(), key=lambda kv: -kv[1].get("ts", 0))[:12]
    busy = job["text"] if job["proc"] and job["proc"].poll() is None else ""
    return dict(item=item, group=group_of(item) if item else "", rating=data["ratings"].get(item),
                recent=[dict(path=r, group=group_of(r), **x) for r, x in recent],
                emotions=EMOTIONS, report=report(data), busy=busy, log=job["log"][-5:])


def rate(body):
    path = body["path"]
    data = load()
    if path not in data["pool"]:
        raise ValueError("фразы нет в очереди")
    if body.get("bad"):
        entry = dict(bad=True)
    else:
        entry = {k: int(body[k]) for k, _ in AXES}
        if any(not 1 <= x <= 5 for x in entry.values()) or body.get("emo") not in EMOTIONS:
            raise ValueError("нужны три шкалы 1–5 и эмоция")
        entry["emo"] = body["emo"]
    entry["ts"] = round(time.time())
    with review.lock:
        data = load()
        data["ratings"][path] = entry
        pick.save(DATA, data)


class Handler(review.Handler):
    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path == "/":
            return self.send_file(os.path.join(HERE, "emotion-eval.html"), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send_json(state(parse_qs(url.query).get("path", [None])[0]))
        if path.startswith("/audio/"):  # только фразы из очереди
            r = unquote(path[7:])
            return self.send_file(review.inside(ROOT, r) if r in load()["pool"] else None)
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if path == "/api/rate":
                rate(body)
                return self.send_json(dict(ok=True))
            if path == "/api/score":
                err = start_score()
                return self.send_json(dict(ok=not err, error=err))
            self.send_error(404)
        except (KeyError, ValueError, TypeError) as e:
            self.send_json(dict(ok=False, error=str(e)), 400)


def main():
    args = sys.argv[1:]
    if args[:1] == ["--score"]:
        return score()
    if args[:1] == ["--report"]:
        return print_report()
    port = int(args[args.index("--port") + 1]) if "--port" in args else 8767
    print(f"http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
