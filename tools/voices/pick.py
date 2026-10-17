"""Подбор голосов: варианты образцов, их синтез и выбор на слух — для всех наборов сразу.

usage: python tools/voices/pick.py [--port 8766]                     страница: http://localhost:8766
       python tools/voices/pick.py --add <набор> <голос> <id> <аудио...> --text "…" [--label "…"]
                                   [--install <имя>] [--title "…"]   новый вариант (аудио склеиваются)
                                   [--post "<скрипт> <аргументы…>"]  с эффектом поверх синтеза
       python tools/voices/pick.py --add-batch <набор> <партия>      кандидаты cut-clips.py как варианты
       python tools/voices/pick.py --list [набор ...]                решения текстом, спорные помечены «?»
       python tools/voices/pick.py --install [набор ...]             поставить выбранное в voice-refs/<набор>

Процесс целиком описан в docs/voice-pipeline.md. Данные набора лежат в voice-clips/<набор>/:
  set.json    {title, phrases}: название и тестовые фразы синтеза
  alts.json   {голос: {title, ref, phrase, note, pool, alts: {id: {label, source, file, ab, install, post, kind}}}}
              ref — эталон для сравнения (пример JeetBot), phrase — его текст, он идёт первой тестовой фразой;
              pool — глобы реплик голоса для оценки sim (у WC3 вместо него pools.json от cut-clips.py);
              file — образец без расширения (по умолчанию <голос>/<id>), ab — префикс синтеза
              (по умолчанию ab/<голос>.<id>), оба от папки набора; install — имя голоса при установке
              (по умолчанию сам голос), так на одной карточке сравниваются, например, neco-arc и neco-arc-ja;
              post — эффект поверх синтеза ([скрипт, аргументы…], путь от корня, {text} — фраза).
  picks.json  {голос: {pick: [id…] или ["keep"], note, seen}}: выбор на слух, он важнее автоматики;
              seen — варианты на момент выбора: появились новые — голос снова ждёт выбора
  ab/         синтез <голос>.<id>.<n>.wav и scores.json (оценки synth.py)
Эмоции читаются из общего кеша voice-clips/emotions.json (tools/voices/emotions.py).

Синтез и оценки делает synth.py (venv проекта); кнопка «Озвучить недостающее» запускает его сама.
Автоматика (там, где у вариантов есть sim): adj = sim − CER_PENALTY·cer по общим для вариантов фразам;
  - стоящий сейчас образец не хуже лучшего нового (разница меньше NOISE) — остаётся;
  - новый лучше на NOISE и больше, cer ≤ CER_MAX, psim ≥ PSIM_MIN, текст не помечен «сверить» — заменяет;
  - иначе голос спорный и ждёт выбора на слух.
--install копирует выбранное в voice-refs/<набор>/<имя>.wav/.txt (заменённое — в voice-refs/<набор>/replaced/<дата>/),
обновляет manifest.json и печатает команду scripts/import-voices.sh для изменённых голосов.
Только стандартная библиотека Python.
"""
import datetime, filecmp, glob, importlib.util, json, os, shlex, shutil, subprocess, sys, tempfile, threading
from http.server import ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # tools/<группа>/ -> корень проекта
spec = importlib.util.spec_from_file_location("review", os.path.join(ROOT, "tools", "wc3", "review.py"))
review = importlib.util.module_from_spec(spec)  # http-обработчик с выдачей файлов и защитой путей
spec.loader.exec_module(review)

CLIPS = os.path.join(ROOT, "voice-clips")
REFS = os.path.join(ROOT, "voice-refs")
EMOTIONS = os.path.join(CLIPS, "emotions.json")
GAP = 0.25          # пауза между склеенными кусками, как в cut-clips.py
LISTEN = 3          # тестовых фраз на странице (и по умолчанию в synth.py)
NOISE = 0.02        # разница sim в пределах шума (несколько фраз, один seed)
CER_PENALTY = 0.5   # штраф за ошибки разборчивости: cer 0,12 стоит 0,06 sim
CER_MAX = 0.05
PSIM_MIN = 0.85
SPREAD_MAX = 0.3    # разброс энергии по окнам: выше — подача внутри образца скачет
KINDS = {"S": "образец на момент нарезки", "D": "диалог", "U": "ответы юнита",
         "C": "спокойная подача", "A": "подача с напором"}
PY = next((p for p in (os.path.join(ROOT, ".venv", "Scripts", "python.exe"), os.path.join(ROOT, ".venv", "bin", "python"))
           if os.path.exists(p)), sys.executable)   # synth.py нужен venv проекта

load = review.cc.load_json
job = {"proc": None, "text": "", "log": []}


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    review.cc.save_json(path, data)


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, check=True)
    return float(r.stdout)


def rel(path):
    return os.path.relpath(os.path.abspath(path), ROOT).replace("\\", "/")


def stamp(path):
    st = os.stat(path)
    return f"{st.st_size}-{int(st.st_mtime)}"


def sets():
    return sorted(d for d in os.listdir(CLIPS) if os.path.isfile(os.path.join(CLIPS, d, "set.json")))


class Set:
    def __init__(self, name):
        self.name = name
        self.dir = os.path.join(CLIPS, name)
        self.refs = os.path.join(REFS, name)
        self.conf = load(os.path.join(self.dir, "set.json"), {})
        self.title = self.conf.get("title", name)
        self.alts_path = os.path.join(self.dir, "alts.json")
        self.picks_path = os.path.join(self.dir, "picks.json")
        self.scores_path = os.path.join(self.dir, "ab", "scores.json")
        self.manifest_path = os.path.join(self.refs, "manifest.json")

    def alts(self):
        return load(self.alts_path, {})

    def phrases(self, g):
        return ([g["phrase"]] if g.get("phrase") else []) + self.conf.get("phrases", [])

    def sample(self, voice, aid, meta):
        base = os.path.join(self.dir, meta.get("file", f"{voice}/{aid}"))
        return base + ".wav", base + ".txt"

    def synth(self, voice, aid, meta, n):
        return os.path.join(self.dir, meta.get("ab", f"ab/{voice}.{aid}") + f".{n}.wav")

    def installed(self, voice, aid, meta, man):
        """Стоит ли вариант сейчас: тот же образец, текст и эффект."""
        name = meta.get("install", voice)
        if man.get(name, {}).get("post") != meta.get("post"):
            return False
        dst = os.path.join(self.refs, name)
        wav, txt = self.sample(voice, aid, meta)
        if not (os.path.exists(dst + ".wav") and os.path.exists(dst + ".txt")):
            return False
        same_text = open(txt, encoding="utf-8").read().strip() == open(dst + ".txt", encoding="utf-8").read().strip()
        return same_text and filecmp.cmp(wav, dst + ".wav", shallow=False)


def emotion(cache, path):
    """Энергия и тон из кеша emotions.py, если запись свежая."""
    e = cache.get(rel(path)) if os.path.exists(path) else None
    if not e or e["stamp"] != stamp(path):
        return None
    win = sorted(e.get("win") or [])
    spread = round(win[int(0.9 * (len(win) - 1))] - win[int(0.1 * (len(win) - 1))], 3) if len(win) > 1 else 0.0
    return dict(a=e["a"], v=e["v"], spread=spread)


def fresh(entry, path):
    return entry if entry and os.path.exists(path) and entry.get("stamp") == stamp(path) else None


def mean(xs):
    return round(sum(xs) / len(xs), 3) if xs else None


def decide(rows):
    """Решение по голосу: dict(pick, auto, reason) или None, если оценок sim нет."""
    main = [r for r in rows if r["main"]]
    if len(main) == 1 and main[0]["current"]:
        return dict(pick=main[0]["id"], auto=True, reason="единственный вариант, уже стоит")
    rows = [r for r in main if r["sims"]]
    if not rows:
        return None
    adj = lambda r, k: mean(r["sims"][:k]) - CER_PENALTY * mean(r["cers"][:k])
    cur = next((r for r in rows if r["current"]), None)
    new = [r for r in rows if r is not cur]
    if not new:
        return dict(pick=cur["id"], auto=True, reason="других вариантов нет")
    # Сравнение — по общим тестовым фразам: варианты озвучены разным их числом (synth-ab.py дальним от
    # лучшего давал 4 фразы из 8, synth.py по умолчанию даёт 3). Лучший — по фразам, что есть у всех,
    # разница с текущим — по всем фразам, что есть у обоих.
    k_all = min(len(r["sims"]) for r in rows)
    best = max(new, key=lambda r: adj(r, k_all))
    k = min(len(best["sims"]), len(cur["sims"])) if cur else len(best["sims"])
    cer = mean(best["cers"][:k])
    problems = []
    if cer > CER_MAX:
        problems.append(f"невнятный синтез (cer {cer:.2f})")
    if best["psim"] is not None and best["psim"] < PSIM_MIN:
        problems.append(f"образец нетипичен для голоса (psim {best['psim']:.2f})")
    if best["check"]:
        problems.append("текст образца надо сверить в review.py")
    if cur is None:
        return dict(pick=best["id"], auto=False,
                    reason="голос ещё не стоит: тот ли персонаж и актёр" + "".join("; " + p for p in problems))
    gain = adj(best, k) - adj(cur, k)
    if gain < NOISE:
        cur_cer = mean(cur["cers"][:k])
        if cur_cer > CER_MAX:
            return dict(pick=cur["id"], auto=False,
                        reason=f"стоящий не хуже новых ({gain:+.3f}), но синтез невнятный (cer {cur_cer:.2f})")
        return dict(pick=cur["id"], auto=True, reason=f"стоящий не хуже лучшего нового ({gain:+.3f})")
    if problems:
        return dict(pick=best["id"], auto=False, reason=f"{best['id']} лучше на {gain:.3f}, но " + ", ".join(problems))
    return dict(pick=best["id"], auto=True, reason=f"{best['id']} лучше стоящего на {gain:.3f}")


def cards(s, emo=None):
    emo = load(EMOTIONS, {}) if emo is None else emo
    alts, picks, scores = s.alts(), load(s.picks_path, {}), load(s.scores_path, {})
    man = load(s.manifest_path, {})
    out = []
    for voice, g in alts.items():
        ph = s.phrases(g)
        rows = []
        for aid, meta in g["alts"].items():
            wav, txt = s.sample(voice, aid, meta)
            if not os.path.exists(wav):
                continue
            sc = scores.get(voice, {}).get(aid, {})
            smp = fresh(sc.get("sample"), wav) or {}
            synth, sims, cers, rsims = [], [], [], []
            for n in range(1, len(ph) + 1):
                path = s.synth(voice, aid, meta, n)
                if not os.path.exists(path):
                    synth.append(None)
                    continue
                f = fresh(sc.get("synth", {}).get(str(n)), path) or {}
                synth.append(dict(sim=f.get("sim"), cer=f.get("cer"), rsim=f.get("rsim"), emo=emotion(emo, path)))
            for f in synth:  # подряд с первой фразы: так варианты сравниваются по одним и тем же фразам
                if not f or f["sim"] is None or f["cer"] is None:
                    break
                sims.append(f["sim"])
                cers.append(f["cer"])
            rsims = [f["rsim"] for f in synth if f and f["rsim"] is not None]
            install = meta.get("install", voice)
            rows.append(dict(id=aid, label=meta.get("label", aid), install=install, main=install == voice,
                             file=meta.get("file", f"{voice}/{aid}"), ab=meta.get("ab", f"ab/{voice}.{aid}"),
                             kind=meta.get("kind", ""), dur=meta.get("dur") or round(duration(wav), 1),
                             text=open(txt, encoding="utf-8").read().strip(), check=bool(meta.get("check")),
                             current=s.installed(voice, aid, meta, man), post=" ".join(meta.get("post", [])),
                             psim=smp.get("psim"), sims=sims, cers=cers, sim=mean(sims), cer=mean(cers),
                             rsim=mean(rsims), emo=emotion(emo, wav), synth=synth[:LISTEN]))
        if not rows:
            continue
        d = decide(rows)
        if d:
            adj = lambda r: (r["sim"] or 0) - CER_PENALTY * (r["cer"] or 0)
            rows.sort(key=lambda r: (not r["current"], not r["main"], -adj(r)))
        else:
            rows.sort(key=lambda r: (not r["current"], r["id"]))
        p = picks.get(voice, {})
        # Новые варианты — те, что появились после выбора на слух; без выбора — отдельные голоса
        # (другой install), которые ещё не стоят: автоматика их не ставит, решать надо на слух.
        seen = p.get("seen") if p.get("pick") else None
        unseen = [r["id"] for r in rows if (r["id"] not in seen if seen is not None
                                             else not r["main"] and not r["current"])]
        out.append(dict(set=s.name, voice=voice, title=g.get("title", voice), note=g.get("note", ""),
                        ref=bool(g.get("ref")), phrases=ph[:LISTEN], alts=rows, decision=d, fresh=unseen,
                        pick=p.get("pick", []), comment=p.get("note", "")))
    return out


def state():
    emo = load(EMOTIONS, {})
    groups, titles = [], {}
    for name in sets():
        s = Set(name)
        titles[name] = s.title
        groups += cards(s, emo)
    busy = job["text"] if job["proc"] and job["proc"].poll() is None else ""
    return dict(sets=titles, groups=groups, busy=busy, log=job["log"][-5:],
                limits=dict(noise=NOISE, cer=CER_MAX, psim=PSIM_MIN, spread=SPREAD_MAX))


def set_pick(set_name, voice, pick=None, comment=None):
    s = Set(set_name)
    with review.lock:
        picks = load(s.picks_path, {})
        p = picks.setdefault(voice, {})
        if pick is not None:
            p["pick"] = pick
            p["seen"] = sorted(s.alts().get(voice, {}).get("alts", {}))  # что было на странице при выборе
        if comment is not None:
            p["note"] = comment
        save(s.picks_path, picks)
        return p


def drop_synth(s, voice, aid, meta, n_max):
    for n in range(1, n_max + 1):  # образец сменился — старый синтез недействителен
        path = s.synth(voice, aid, meta, n)
        if os.path.exists(path):
            os.remove(path)


def add(set_name, voice, aid, sources, text, label=None, install=None, title=None, post=None):
    """Склеивает аудио в вариант <голос>/<id> набора (24 кГц моно, тишина по краям обрезана)."""
    s = Set(set_name)
    if not os.path.isfile(os.path.join(s.dir, "set.json")):
        sys.exit(f"Нет набора {set_name}: создайте voice-clips/{set_name}/set.json ({{\"title\": …, \"phrases\": […]}})")
    meta = dict(label=label or aid, source=[rel(p) for p in sources])
    wav, txt = s.sample(voice, aid, meta)
    os.makedirs(os.path.dirname(wav), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="alt-")
    parts = []
    for i, src in enumerate(sources):
        dst = os.path.join(tmp, f"{i}.wav")
        ffmpeg("-i", src, "-af", "silenceremove=start_periods=1:start_threshold=-50dB,areverse,"
               "silenceremove=start_periods=1:start_threshold=-50dB,areverse", "-ac", "1", "-ar", "24000", dst)
        parts.append(dst)
    inputs, filt = [], []
    for i, p in enumerate(parts):
        inputs += ["-i", p]
        filt.append(f"[{i}:a]{'anull' if i == len(parts) - 1 else f'apad=pad_dur={GAP}'}[a{i}]")
    filt.append("".join(f"[a{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[out]")
    ffmpeg(*inputs, "-filter_complex", ";".join(filt), "-map", "[out]", "-ac", "1", "-ar", "24000", wav)
    shutil.rmtree(tmp, ignore_errors=True)
    with open(txt, "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    with review.lock:
        alts = s.alts()
        g = alts.setdefault(voice, {"title": title or voice, "alts": {}})
        if title:
            g["title"] = title
        if install:
            meta["install"] = install
        if post:
            meta["post"] = post
        g["alts"][aid] = meta
        save(s.alts_path, alts)
    drop_synth(s, voice, aid, meta, len(s.phrases(g)))
    print(f"{rel(wav)}  {duration(wav):.1f} с")


def add_batch(set_name, batch):
    """Кандидаты партии cut-clips.py (voice-clips/<набор>/<партия>/clips.json) становятся вариантами
    <партия>.<вид><n>; файлы остаются в партии, поэтому правки текста в review.py видны сразу.
    Оценки из старого synth-ab.py (<партия>/ab/ab.json), если есть, переносятся в scores.json."""
    s = Set(set_name)
    bdir = os.path.join(s.dir, batch)
    clips = load(os.path.join(bdir, "clips.json"), None)
    if clips is None:
        sys.exit(f"Нет партии {rel(bdir)}: сначала tools/wc3/cut-clips.py {batch}")
    old_ab = load(os.path.join(bdir, "ab", "ab.json"), {})
    origs = rel(review.cc.ORIGS)
    with review.lock:
        alts, scores = s.alts(), load(s.scores_path, {})
        for cid, r in clips.items():
            voice, kind = r["voice"], r["kind"]
            aid = f"{batch}.{cid.rsplit('.', 1)[1]}"
            tag = r["tag"].split(":", 1)[-1] if ":" in r["tag"] else ""
            meta = dict(label=" · ".join(x for x in (KINDS.get(kind, kind), tag) if x), kind=kind, dur=r["dur"],
                        file=f"{batch}/{voice}/{cid}", ab=f"{batch}/ab/{cid}",
                        source=[p["rel"] if p["rel"].startswith("voice-refs/") else f"{origs}/{p['rel']}"
                                for p in r["parts"]])
            if any(p.get("alt") for p in r["parts"]):
                meta["check"] = True
            if kind == "A":  # подача с напором — отдельный голос, как neco-arc-angry
                meta["install"] = voice + "-angry"
            g = alts.setdefault(voice, {"title": r["title"], "alts": {}})
            g["alts"][aid] = meta
            ab = old_ab.get(cid)
            if ab:
                wav = s.sample(voice, aid, meta)[0]
                sc = scores.setdefault(voice, {}).setdefault(aid, {})
                sc["sample"] = dict(stamp=stamp(wav), psim=ab["psim"])
                for n, (sim, cer) in enumerate(zip(ab["sims"], ab["cers"]), 1):
                    path = s.synth(voice, aid, meta, n)
                    if os.path.exists(path):
                        sc.setdefault("synth", {})[str(n)] = dict(stamp=stamp(path), sim=sim, cer=cer)
        save(s.alts_path, dict(sorted(alts.items())))
        save(s.scores_path, scores)
    print(f"{len(clips)} кандидатов из {rel(bdir)} → {rel(s.alts_path)}")


def final(s):
    """{голос: ([id…], как решено)} — выбор на слух, иначе автоматика."""
    out = {}
    for c in cards(s):
        if c["pick"]:
            out[c["voice"]] = ([] if c["pick"] == ["keep"] else c["pick"], "на слух")
        elif c["decision"] and c["decision"]["auto"]:
            out[c["voice"]] = ([c["decision"]["pick"]], "авто")
        else:
            out[c["voice"]] = ([], "не решено")
    return out


def install(names):
    stamp_day = datetime.date.today().isoformat()
    for name in names:
        s = Set(name)
        alts, man = s.alts(), load(s.manifest_path, {})
        changed = []
        for voice, (ids, how) in final(s).items():
            g = alts[voice]
            targets = [g["alts"][aid].get("install", voice) for aid in ids]
            if len(targets) != len(set(targets)):
                print(f"{voice}: выбрано несколько вариантов под одним именем ({', '.join(ids)}), оставьте один — пропущено")
                continue
            for aid in ids:
                meta = g["alts"][aid]
                target = meta.get("install", voice)
                if s.installed(voice, aid, meta, man):
                    continue
                wav, txt = s.sample(voice, aid, meta)
                os.makedirs(s.refs, exist_ok=True)
                dst = os.path.join(s.refs, target)
                if os.path.exists(dst + ".wav"):
                    old = os.path.join(s.refs, "replaced", stamp_day)
                    os.makedirs(old, exist_ok=True)
                    for ext in (".wav", ".txt"):
                        shutil.move(dst + ext, os.path.join(old, target + ext))
                shutil.copy(wav, dst + ".wav")
                shutil.copy(txt, dst + ".txt")
                man[target] = dict(title=g.get("title", voice), duration=round(duration(wav), 2), installed=stamp_day,
                                   candidate=f"{name}/{voice}.{aid}", picked=how, source=meta.get("source", []),
                                   text=open(txt, encoding="utf-8").read().strip())
                if meta.get("post"):  # голос звучит как надо только с этим эффектом поверх синтеза
                    man[target]["post"] = meta["post"]
                changed.append(target)
                print(f"{target} ← {voice}.{aid} ({how})")
        save(s.manifest_path, man)
        if changed:
            print(f"Дальше: scripts/import-voices.sh voice-refs/{name} --force {' '.join(changed)}")
        else:
            print(f"{name}: менять нечего")


def start_synth(set_name, voices):
    if job["proc"] and job["proc"].poll() is None:
        return "синтез уже идёт"
    cmd = [PY, os.path.join(HERE, "synth.py"), set_name] + [x for v in voices for x in ("--voice", v)]
    job["proc"] = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
    job["text"], job["log"] = "запуск synth.py", []

    def follow(proc):
        for line in proc.stdout:
            line = line.strip()
            if line:
                job["text"] = line
                job["log"].append(line)
        if proc.wait():
            job["log"].append(f"synth.py завершился с кодом {proc.returncode}")

    threading.Thread(target=follow, args=(job["proc"],), daemon=True).start()
    return None


class Handler(review.Handler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self.send_file(os.path.join(HERE, "pick.html"), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send_json(state())
        if path.startswith("/audio/"):  # /audio/<набор>/<путь внутри набора>
            name, _, sub = unquote(path[7:]).partition("/")
            return self.send_file(review.inside(Set(name).dir, sub) if name in sets() else None)
        if path.startswith("/ref/"):    # /ref/<набор>/<голос> — эталон (пример JeetBot)
            name, _, voice = unquote(path[5:]).partition("/")
            g = Set(name).alts().get(voice, {}) if name in sets() else {}
            return self.send_file(review.inside(ROOT, g["ref"]) if g.get("ref") else None)
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if data.get("set") not in sets():
                raise ValueError("нет такого набора")
            if path == "/api/pick":
                p = set_pick(data["set"], data["voice"], data.get("pick"), data.get("note"))
                return self.send_json(dict(ok=True, **p))
            if path == "/api/synth":
                err = start_synth(data["set"], data.get("voices") or [])
                return self.send_json(dict(ok=not err, error=err))
            self.send_error(404)
        except (KeyError, ValueError) as e:
            self.send_json(dict(ok=False, error=str(e)), 400)


def main():
    args = sys.argv[1:]
    if args[:1] == ["--add"]:
        set_name, voice, aid, rest = args[1], args[2], args[3], args[4:]
        opts = {}
        for key in ("--text", "--label", "--install", "--title", "--post"):
            if key in rest:
                i = rest.index(key)
                opts[key[2:]] = rest[i + 1]
                del rest[i:i + 2]
        post = shlex.split(opts["post"]) if "post" in opts else None
        return add(set_name, voice, aid, rest, opts["text"], opts.get("label"), opts.get("install"), opts.get("title"),
                   post)
    if args[:1] == ["--add-batch"]:
        return add_batch(args[1], args[2])
    if args[:1] in (["--install"], ["--list"]):
        names = args[1:] or sets()
        unknown = set(names) - set(sets())
        if unknown:
            sys.exit(f"Нет наборов: {' '.join(sorted(unknown))}. Есть: {' '.join(sets())}")
        if args[0] == "--install":
            return install(names)
        for name in names:
            s = Set(name)
            print(f"== {s.title} ({name})")
            for c in cards(s):
                d = c["decision"]
                mark = "  " if (c["pick"] or (d and d["auto"])) and not c["fresh"] else "? "
                pick = ",".join(c["pick"]) if c["pick"] else (d["pick"] if d else "—")
                why = "на слух" if c["pick"] else (d["reason"] if d else "нет оценок sim, только на слух")
                if c["fresh"]:
                    why += "; новые варианты: " + ", ".join(c["fresh"])
                print(f"{mark}{c['voice']:22s} {pick:20s} {why}")
        return
    port = int(args[args.index("--port") + 1]) if "--port" in args else 8766
    print(f"http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
