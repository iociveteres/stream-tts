"""Выбор образцов по итогам A/B (synth-ab.py): где всё ясно — автоматически, на слух — только спорное.

usage: python tools/wc3/pick.py <batch> [--port 8766]   интерфейс: http://localhost:8766
       python tools/wc3/pick.py <batch> --list          решения текстом
       python tools/wc3/pick.py <batch> --install       поставить выбранные образцы в voice-refs/wc3

Правила (adj = sim − CER_PENALTY·cer, то есть невнятный синтез штрафуется):
  - текущий образец S00 не хуже лучшего нового (разница adj меньше NOISE) — остаётся, автоматически;
  - новый лучше на NOISE и больше, синтез разборчив (cer ≤ CER_MAX), образец типичен для голоса
    (psim ≥ PSIM_MIN) и текст проверен — заменяет S00, автоматически;
  - иначе спорно: новый голос без S00, невнятный синтез, нетипичный образец, текст «сверить».
Выбор на слух хранится в <batch>/picks.json ({голос: id кандидата или "none"}) и важнее автоматики.

--install копирует выбранные клипы в voice-refs/wc3/<голос>.wav/.txt (старые — в
voice-refs/wc3/replaced/<batch>/) и обновляет manifest.json. Затем голоса пересобираются:
scripts/import-voices.sh voice-refs/wc3 --force (не во время synth-ab.py: сервер не терпит параллельной регистрации).
Только стандартная библиотека Python.
"""
import datetime, importlib.util, json, os, shutil, sys
from http.server import ThreadingHTTPServer
from urllib.parse import unquote, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # tools/<группа>/ -> корень проекта


spec = importlib.util.spec_from_file_location("review", os.path.join(HERE, "review.py"))
review = importlib.util.module_from_spec(spec)  # http-обработчик с выдачей файлов, cut-clips — review.cc
spec.loader.exec_module(review)
cc = review.cc

NOISE = 0.02        # разница sim в пределах шума (8 фраз, один seed)
CER_PENALTY = 0.5   # штраф за ошибки разборчивости: cer 0,12 стоит 0,06 sim
CER_MAX = 0.05
PSIM_MIN = 0.85
LISTEN = 3          # сколько тестовых фраз показывать на странице


def adj(r):
    return r["sim"] - CER_PENALTY * r["cer"]


def decide(cands, clips):
    """Решение по голосу: dict(pick, auto, reason). cands: {id: строка ab.json}."""
    s00 = next((c for c in cands if c.endswith(".S00")), None)
    new = [c for c in cands if c != s00]
    if not new:
        return dict(pick=s00, auto=True, reason="новых кандидатов нет")
    best_id = max(new, key=lambda c: adj(cands[c]))
    best = cands[best_id]
    problems = []
    if best["cer"] > CER_MAX:
        problems.append(f"невнятный синтез (cer {best['cer']:.2f})")
    if best["psim"] < PSIM_MIN:
        problems.append(f"образец нетипичен для голоса (psim {best['psim']:.2f})")
    if any(p.get("alt") for p in clips[best_id]["parts"]):
        problems.append("текст образца надо сверить в review.py")
    if s00 is None:
        return dict(pick=best_id, auto=False,
                    reason="новый голос: тот ли персонаж и актёр" + "".join("; " + p for p in problems))
    gain = adj(best) - adj(cands[s00])
    if gain < NOISE:
        if cands[s00]["cer"] > CER_MAX:
            return dict(pick=s00, auto=False,
                        reason=f"текущий не хуже новых ({gain:+.3f}), но синтез невнятный (cer {cands[s00]['cer']:.2f})")
        return dict(pick=s00, auto=True, reason=f"текущий не хуже лучшего нового ({gain:+.3f})")
    if problems:
        return dict(pick=best_id, auto=False, reason=f"новый лучше на {gain:.3f}, но " + ", ".join(problems))
    return dict(pick=best_id, auto=True, reason=f"новый лучше текущего на {gain:.3f}")


class Batch:
    def __init__(self, name):
        self.name = name
        self.dir = os.path.join(cc.CLIPS, name)
        self.picks_path = os.path.join(self.dir, "picks.json")

    def state(self):
        clips = cc.load_json(os.path.join(self.dir, "clips.json"), {})
        ab = cc.load_json(os.path.join(self.dir, "ab", "ab.json"), {})
        picks = cc.load_json(self.picks_path, {})
        phrases = cc.load_json(os.path.join(self.dir, "ab", "phrases.json"), [])
        voices = []
        for vid in sorted({r["voice"] for r in ab.values()}):
            cands = {c: r for c, r in ab.items() if r["voice"] == vid and c in clips}
            if not cands:
                continue
            d = decide(cands, clips)
            rows = []
            for cid, r in sorted(cands.items(), key=lambda x: (not x[0].endswith(".S00"), -adj(x[1]))):
                rows.append(dict(id=cid, tag=r["tag"], dur=r["dur"], sim=r["sim"], cer=r["cer"], psim=r["psim"],
                                 n=len(r["sims"]), text=cc.clip_text(clips[cid]["parts"]),
                                 check=any(p.get("alt") for p in clips[cid]["parts"])))
            voices.append(dict(voice=vid, title=clips[next(iter(cands))]["title"], choice=picks.get(vid),
                               cands=rows, **d))
        return dict(batch=self.name, phrases=phrases[:LISTEN], voices=voices)

    def choose(self, vid, cid):
        with review.lock:
            picks = cc.load_json(self.picks_path, {})
            if cid:
                picks[vid] = cid
            else:
                picks.pop(vid, None)
            cc.save_json(self.picks_path, picks)
        return picks.get(vid)

    def audio(self, path):
        """/clip/<голос>/<id>.wav — образец, /ab/<id>.<n>.wav — синтез тестовой фразы."""
        if path.startswith("/clip/"):
            return review.inside(self.dir, unquote(path[6:]))
        if path.startswith("/ab/"):
            return review.inside(os.path.join(self.dir, "ab"), unquote(path[4:]))
        return None

    def final(self):
        """{голос: (id или None, как решено)} — выбор на слух, иначе автоматика."""
        out = {}
        for v in self.state()["voices"]:
            if v["choice"]:
                out[v["voice"]] = (None if v["choice"] == "none" else v["choice"], "на слух")
            elif v["auto"]:
                out[v["voice"]] = (v["pick"], "авто")
            else:
                out[v["voice"]] = (None, "спорно, не прослушано")
        return out


class Handler(review.Handler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self.send_file(os.path.join(HERE, "pick.html"), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send_json(self.batch.state())
        if path.startswith(("/clip/", "/ab/")):
            return self.send_file(self.batch.audio(path))
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if path == "/api/pick":
                return self.send_json(dict(ok=True, choice=self.batch.choose(data["voice"], data.get("id"))))
            self.send_error(404)
        except (KeyError, ValueError) as e:
            self.send_json(dict(ok=False, error=str(e)), 400)


def install(batch):
    clips = cc.load_json(os.path.join(batch.dir, "clips.json"), {})
    manifest_path = os.path.join(cc.REFS, "manifest.json")
    manifest = cc.load_json(manifest_path, {})
    backup = os.path.join(cc.REFS, "replaced", batch.name)
    changed = []
    for vid, (cid, how) in batch.final().items():
        if cid is None or cid.endswith(".S00"):
            continue
        src = os.path.join(batch.dir, vid, cid)
        dst = os.path.join(cc.REFS, vid)
        if os.path.exists(dst + ".wav"):
            os.makedirs(backup, exist_ok=True)
            for ext in (".wav", ".txt"):
                if not os.path.exists(os.path.join(backup, vid + ext)):
                    shutil.copy(dst + ext, os.path.join(backup, vid + ext))
        shutil.copy(src + ".wav", dst + ".wav")
        shutil.copy(src + ".txt", dst + ".txt")
        r = clips[cid]
        manifest[vid] = dict(title=r["title"], source=[p["rel"] for p in r["parts"]], duration=r["dur"],
                             text=open(src + ".txt", encoding="utf-8").read().strip(),
                             candidate=f"{batch.name}/{cid}", picked=how,
                             installed=datetime.date.today().isoformat())
        changed.append(f"{vid} ← {cid} ({how})")
    cc.save_json(manifest_path, manifest)
    print("\n".join(changed) or "Заменять нечего")
    if changed:
        print(f"Старые образцы: {backup}\nДальше: scripts/import-voices.sh voice-refs/wc3 --force")


def main():
    args = sys.argv[1:]
    port = 8766
    if "--port" in args:
        i = args.index("--port")
        port = int(args[i + 1])
        del args[i:i + 2]
    flags = {a for a in args if a.startswith("--")}
    names = [a for a in args if not a.startswith("--")]
    if not names or not os.path.isfile(os.path.join(cc.CLIPS, names[0], "ab", "ab.json")):
        sys.exit("Укажите партию с результатами A/B: python tools/wc3/synth-ab.py <партия>, затем python tools/wc3/pick.py <партия>")
    batch = Batch(names[0])
    if "--install" in flags:
        return install(batch)
    if "--list" in flags:
        for v in batch.state()["voices"]:
            mark = "  " if v["auto"] else "? "
            print(f"{mark}{v['voice']:20s} {v['pick']:26s} {v['reason']}" + (f"  [выбрано: {v['choice']}]" if v["choice"] else ""))
        return
    Handler.batch = batch
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Партия {batch.name}: http://localhost:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
