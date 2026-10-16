"""Сравнение альтернативных образцов мемных голосов: эталон JeetBot, сами образцы и их синтез.

usage: python meme-pick.py [--port 8767]           интерфейс: http://localhost:8767
       python meme-pick.py --add <голос> <id> <аудио...> --text "…" [--label "…"]
                                                   новая альтернатива (аудио склеиваются, тишина обрезается)
       python meme-pick.py --synth [голос ...]     озвучить тестовые фразы, где синтеза ещё нет
       python meme-pick.py --install               поставить выбранное в voice-refs/meme

Альтернативы лежат в voice-clips/meme/<голос>/<id>.wav + .txt, метаданные в voice-clips/meme/alts.json:
  {голос: {title, ref: пример JeetBot, phrase: его текст, alts: {id: {label, install, source}}}}
install — имя голоса при установке (по умолчанию сам голос), так две версии одного персонажа
(neco-arc и jeetbot-neco-arc) сравниваются на одной карточке. S00 — образец, который стоит сейчас.
post — эффект поверх синтеза ([скрипт, аргументы…], например villager-fx.py --mode hum; {text} в аргументах
заменяется фразой); образец у такого
варианта тот же, а при установке post записывается в manifest.json голоса.
Синтез: voice-clips/meme/ab/<голос>.<id>.<n>.wav, первая фраза — демо-текст JeetBot для голоса.
Выбор и заметки: voice-clips/meme/picks.json ({голос: {pick: [id…], note}}).
Синтезу нужен cosyvoice-server на localhost:8080; запросы к нему идут строго по одному.
После --install: ./import-voices.sh voice-refs/meme --force
"""
import datetime, filecmp, importlib.util, json, os, shutil, subprocess, sys, tempfile, threading
from http.server import ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("review", os.path.join(ROOT, "review.py"))
review = importlib.util.module_from_spec(spec)  # http-обработчик с выдачей файлов и защитой путей
spec.loader.exec_module(review)

DIR = os.path.join(ROOT, "voice-clips", "meme")
AB = os.path.join(DIR, "ab")
REFS = os.path.join(ROOT, "voice-refs", "meme")
ALTS = os.path.join(DIR, "alts.json")
PICKS = os.path.join(DIR, "picks.json")
# После демо-текста JeetBot: обычная стримерская реплика и длинная фраза с интонацией.
COMMON = [
    "Спасибо за подписку, дружище! Заходи почаще, тут всегда весело.",
    "Так, чат, слушайте сюда. Если ещё раз кто-нибудь напишет это слово, сразу получит бан.",
]
GAP = 0.25
API = "http://localhost:8080"
FX_PY = next((p for p in (os.path.join(ROOT, ".venv", "Scripts", "python.exe"), os.path.join(ROOT, ".venv", "bin", "python"))
              if os.path.exists(p)), sys.executable)   # эффектам (post) нужен numpy из venv проекта
SEED = 7    # как в synth-ab.py: одинаковый seed, чтобы разница была в образце, а не в случайности

load, save = review.cc.load_json, review.cc.save_json
synth_lock = threading.Lock()
busy = {"text": ""}


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True, check=True)
    return float(r.stdout)


def curl(*args):
    r = subprocess.run(["curl", "-s", "-f", *args], capture_output=True)
    if r.returncode:
        raise RuntimeError(f"curl {args[:3]}: {r.returncode} {r.stdout[:200]!r}")
    return r.stdout


def register(name, wav, text):
    curl("-X", "POST", f"{API}/speaker?type=extract", "-F", f"name={name}", "-F", f"text={text}",
         "-F", f"audio=@{wav}")


def unregister(name):
    subprocess.run(["curl", "-s", "-X", "DELETE", f"{API}/speaker/{name}"], capture_output=True)


def tts(voice, text, dst):
    body = dict(text=text, voice=voice, mode="zero-shot", response_format="wav", seed=SEED)
    data = curl("-X", "POST", f"{API}/tts", "-H", "Content-Type: application/json",
                "-d", json.dumps(body, ensure_ascii=False))
    with open(dst, "wb") as f:
        f.write(data)


def phrases(g):
    return ([g["phrase"]] if g.get("phrase") else []) + COMMON


def alt_paths(voice, aid):
    base = os.path.join(DIR, voice, aid)
    return base + ".wav", base + ".txt"


def ab_path(voice, aid, n):
    return os.path.join(AB, f"{voice}.{aid}.{n}.wav")


def add(voice, aid, sources, text, label=None, install=None):
    """Склеивает аудио в альтернативу <голос>/<id> (24 кГц моно, тишина по краям обрезана)."""
    wav, txt = alt_paths(voice, aid)
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
        alts = load(ALTS, {})
        g = alts.setdefault(voice, {"title": voice, "alts": {}})
        meta = dict(label=label or aid, source=[os.path.relpath(s, ROOT).replace("\\", "/") for s in sources])
        if install:
            meta["install"] = install
        g["alts"][aid] = meta
        save(ALTS, alts)
    for n in range(1, len(phrases(g)) + 1):  # образец сменился — старый синтез недействителен
        if os.path.exists(ab_path(voice, aid, n)):
            os.remove(ab_path(voice, aid, n))
    print(f"{wav}  {duration(wav):.1f} с")


def synth(only=None, log=print):
    """Озвучивает тестовые фразы всеми альтернативами, у которых синтеза ещё нет."""
    with synth_lock:
        os.makedirs(AB, exist_ok=True)
        for voice, g in load(ALTS, {}).items():
            if only and voice not in only:
                continue
            for aid in g["alts"]:
                todo = [(n, p) for n, p in enumerate(phrases(g), 1) if not os.path.exists(ab_path(voice, aid, n))]
                if not todo:
                    continue
                wav, txt = alt_paths(voice, aid)
                name = f"ab-{voice}-{aid}".lower()
                unregister(name)
                register(name, wav, open(txt, encoding="utf-8").read().strip())
                try:
                    for n, p in todo:
                        busy["text"] = f"синтез {voice} / {aid}: фраза {n}"
                        log(busy["text"])
                        post = g["alts"][aid].get("post")
                        dst = ab_path(voice, aid, n)
                        tts(name, p, dst + ".raw.wav" if post else dst)
                        if post:  # эффект поверх синтеза, например villager-fx.py --mode hum
                            subprocess.run([FX_PY, os.path.join(ROOT, post[0]), dst + ".raw.wav", dst,
                                            *(x.replace("{text}", p) for x in post[1:])],
                                           check=True)
                            os.remove(dst + ".raw.wav")
                finally:
                    unregister(name)
        busy["text"] = ""


def installed(voice, aid, meta, man=None):
    """Стоит ли вариант сейчас: тот же образец, текст и эффект."""
    name = meta.get("install", voice)
    man = load(os.path.join(REFS, "manifest.json"), {}) if man is None else man
    if man.get(name, {}).get("post") != meta.get("post"):
        return False
    dst = os.path.join(REFS, name)
    return all(os.path.exists(dst + ext) and filecmp.cmp(src, dst + ext, shallow=False)
               for src, ext in zip(alt_paths(voice, aid), (".wav", ".txt")))


def state():
    alts, picks = load(ALTS, {}), load(PICKS, {})
    man = load(os.path.join(REFS, "manifest.json"), {})
    groups = []
    for voice, g in alts.items():
        ph = phrases(g)
        rows = []
        for aid, meta in sorted(g["alts"].items(), key=lambda x: x[0] != "S00"):  # текущий первым
            wav, txt = alt_paths(voice, aid)
            if not os.path.exists(wav):
                continue
            rows.append(dict(id=aid, label=meta.get("label", aid), install=meta.get("install", voice),
                             dur=round(duration(wav), 1), text=open(txt, encoding="utf-8").read().strip(),
                             current=installed(voice, aid, meta, man), post=" ".join(meta.get("post", [])),
                             synth=[os.path.exists(ab_path(voice, aid, n)) for n in range(1, len(ph) + 1)]))
        p = picks.get(voice, {})
        groups.append(dict(voice=voice, title=g.get("title", voice), note=g.get("note", ""),
                           ref=bool(g.get("ref")), phrases=ph, alts=rows,
                           pick=p.get("pick", []), comment=p.get("note", "")))
    groups.sort(key=lambda g: -len(g["alts"]))  # сначала голоса, где есть из чего выбирать
    return dict(groups=groups, busy=busy["text"])


def set_pick(voice, pick=None, comment=None):
    with review.lock:
        picks = load(PICKS, {})
        p = picks.setdefault(voice, {})
        if pick is not None:
            p["pick"] = pick
        if comment is not None:
            p["note"] = comment
        save(PICKS, picks)
        return p


def install():
    alts, picks = load(ALTS, {}), load(PICKS, {})
    man_path = os.path.join(REFS, "manifest.json")
    man = load(man_path, {})
    stamp = datetime.date.today().isoformat()
    changed = []
    for voice, p in picks.items():
        g = alts.get(voice)
        names = [g["alts"][aid].get("install", voice) for aid in p.get("pick", [])]
        if len(names) != len(set(names)):
            print(f"{voice}: выбрано несколько вариантов под одним именем ({', '.join(p['pick'])}), оставьте один — пропущено")
            continue
        for aid in p.get("pick", []):
            meta = g["alts"][aid]
            name = meta.get("install", voice)
            wav, txt = alt_paths(voice, aid)
            if installed(voice, aid, meta, man):
                continue
            dst = os.path.join(REFS, name)
            if os.path.exists(dst + ".wav"):
                old = os.path.join(REFS, "replaced", stamp)
                os.makedirs(old, exist_ok=True)
                for ext in (".wav", ".txt"):
                    shutil.move(dst + ext, os.path.join(old, name + ext))
            shutil.copy(wav, dst + ".wav")
            shutil.copy(txt, dst + ".txt")
            man[name] = dict(duration=round(duration(wav), 2), installed=stamp, candidate=f"meme/{voice}.{aid}",
                             source=meta.get("source", []), text=open(txt, encoding="utf-8").read().strip())
            if meta.get("post"):  # голос звучит как надо только с этим эффектом поверх синтеза
                man[name]["post"] = meta["post"]
            changed.append(f"{name} ← {voice}.{aid}")
        for aid, meta in g["alts"].items():
            if aid not in p.get("pick", []) and meta.get("install", voice) != voice and installed(voice, aid, meta, man):
                print(f"не выбран, но стоит: {meta['install']} (удалить вручную, если не нужен)")
    save(man_path, man)
    print("\n".join(changed) or "Менять нечего")
    if changed:
        print("Дальше: ./import-voices.sh voice-refs/meme --force")


class Handler(review.Handler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self.send_file(os.path.join(ROOT, "meme-pick.html"), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send_json(state())
        if path.startswith("/alt/"):    # /alt/<голос>/<id>.wav
            return self.send_file(review.inside(DIR, unquote(path[5:])))
        if path.startswith("/ab/"):     # /ab/<голос>.<id>.<n>.wav
            return self.send_file(review.inside(AB, unquote(path[4:])))
        if path.startswith("/ref/"):    # /ref/<голос> — пример JeetBot
            g = load(ALTS, {}).get(unquote(path[5:]), {})
            return self.send_file(review.inside(ROOT, g["ref"]) if g.get("ref") else None)
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if path == "/api/pick":
                return self.send_json(dict(ok=True, **set_pick(data["voice"], data.get("pick"), data.get("note"))))
            if path == "/api/synth":
                if synth_lock.locked():
                    return self.send_json(dict(ok=False, error="синтез уже идёт"))
                threading.Thread(target=synth, args=(data.get("voices"),), daemon=True).start()
                return self.send_json(dict(ok=True))
            self.send_error(404)
        except (KeyError, ValueError) as e:
            self.send_json(dict(ok=False, error=str(e)), 400)


def main():
    args = sys.argv[1:]
    if args[:1] == ["--add"]:
        voice, aid, rest = args[1], args[2], args[3:]
        opts = {}
        for key in ("--text", "--label", "--install"):
            if key in rest:
                i = rest.index(key)
                opts[key[2:]] = rest[i + 1]
                del rest[i:i + 2]
        return add(voice, aid, rest, opts["text"], opts.get("label"), opts.get("install"))
    if args[:1] == ["--synth"]:
        return synth(args[1:] or None)
    if args[:1] == ["--install"]:
        return install()
    port = int(args[args.index("--port") + 1]) if "--port" in args else 8767
    print(f"http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
