"""Локальный интерфейс отслушивания партии: слушать, оценивать и править текст в одном месте.

usage: python review.py [партия] [--port 8765]
  Без партии берётся последняя из voice-clips/wc3/. Открыть http://localhost:8765.

Оценки сразу пишутся в <партия>/ratings.txt, правки текста — в voice-clips/wc3/text-fixes.json
(как проверенные на слух) и во все клипы партии, где встречается эта реплика: их .txt,
clips.json и ratings.txt. Эмоции и «не брать» ставятся фразам и хранятся в общем
voice-clips/wc3/phrase-tags.json; эмоция клипа в ratings.txt — сводка меток его фраз.
Только стандартная библиотека Python.
"""
import importlib.util, json, mimetypes, os, re, sys, threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("cut_clips", os.path.join(ROOT, "cut-clips.py"))
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)

lock = threading.Lock()


def inside(base, rel):
    """Путь rel внутри base или None (защита от ../)."""
    path = os.path.realpath(os.path.join(base, rel))
    return path if os.path.commonpath([path, os.path.realpath(base)]) == os.path.realpath(base) else None


def clean(s):
    return re.sub(r"\s*[|\r\n]+\s*", " / ", str(s or "")).strip(" /")


def emotion_summary(parts, tags):
    """Эмоция клипа для ratings.txt — сводка меток его фраз (фразы «не брать» не считаются)."""
    used = [tags.get(p["rel"], {}) for p in parts]
    used = [t for t in used if not t.get("bad")]
    counts = Counter(e for t in used for e in t.get("emotions", []))
    if not counts:
        return ""
    if all(n == len(used) for n in counts.values()):
        return ", ".join(counts)
    untagged = sum(1 for t in used if not t.get("emotions"))
    text = ", ".join(f"{e} ×{n}" for e, n in counts.most_common())
    return text + (f", без метки ×{untagged}" if untagged else "") + " (смешанная)"


class Batch:
    def __init__(self, name):
        self.name = name
        self.dir = os.path.join(cc.CLIPS, name)
        self.clips_path = os.path.join(self.dir, "clips.json")
        self.fixes_path = os.path.join(cc.CLIPS, "text-fixes.json")
        self.tags_path = os.path.join(cc.CLIPS, "phrase-tags.json")

    def state(self):
        clips = cc.load_json(self.clips_path, {})
        ratings = {cid: dict(zip(("score", "emotion", "comment"), v))
                   for cid, v in cc.parse_ratings(self.dir).items()}
        return dict(batch=self.name, clips=clips, ratings=ratings,
                    tags=cc.load_json(self.tags_path, {}))

    def _adopt_clip_emotion(self, clip, text, tags):
        """Эмоция, введённая для клипа целиком, становится меткой его фраз без своих меток."""
        if not text or "×" in text:
            return
        emotions = [e.strip() for e in text.split(",") if e.strip()]
        for p in clip["parts"]:
            t = tags.setdefault(p["rel"], {})
            if not t.get("emotions"):
                t["emotions"] = emotions[:]

    def _save_tags(self, clips, tags, values=None):
        cc.save_json(self.tags_path, {r: t for r, t in tags.items() if t.get("emotions") or t.get("bad")})
        values = cc.parse_ratings(self.dir) if values is None else values
        for cid, clip in clips.items():
            score, _, comment = values.get(cid, ("", "", ""))
            values[cid] = (score, emotion_summary(clip["parts"], tags), comment)
        cc.write_ratings(self.dir, clips, values)
        return values

    def migrate(self):
        """Эмоции клипов из старого ratings.txt переносятся на фразы (один раз, при запуске)."""
        with lock:
            clips = cc.load_json(self.clips_path, {})
            tags = cc.load_json(self.tags_path, {})
            values = cc.parse_ratings(self.dir)
            for cid, (_, emotion, _) in values.items():
                if cid in clips:
                    self._adopt_clip_emotion(clips[cid], emotion, tags)
            self._save_tags(clips, tags, values)

    def rate(self, cid, score, comment, emotion=None):
        with lock:
            clips = cc.load_json(self.clips_path, {})
            if cid not in clips:
                raise KeyError(cid)
            tags = cc.load_json(self.tags_path, {})
            if emotion:  # старая страница без меток фраз
                self._adopt_clip_emotion(clips[cid], clean(emotion), tags)
            values = cc.parse_ratings(self.dir)
            values[cid] = (clean(score), "", clean(comment))
            values = self._save_tags(clips, tags, values)
            return dict(zip(("score", "emotion", "comment"), values[cid]))

    def tag(self, rels, add=None, remove=None, bad=None):
        """Метки фраз: добавить или убрать эмоцию, отметить «не брать». Общие для всех партий."""
        add, remove = clean(add).lower(), clean(remove).lower()
        with lock:
            clips = cc.load_json(self.clips_path, {})
            tags = cc.load_json(self.tags_path, {})
            for rel in rels:
                t = tags.setdefault(rel, {})
                emotions = t.setdefault("emotions", [])
                if add and add not in emotions:
                    emotions.append(add)
                if remove in emotions:
                    emotions.remove(remove)
                if bad is not None:
                    t["bad"] = bool(bad)
            values = self._save_tags(clips, tags)
            touched = {cid for cid, c in clips.items() if any(p["rel"] in rels for p in c["parts"])}
            return dict(tags={r: tags.get(r, {}) for r in rels},
                        ratings={cid: dict(zip(("score", "emotion", "comment"), values[cid])) for cid in touched})

    def fix_text(self, rel, text):
        """Проверенный на слух текст реплики: в text-fixes.json и во все клипы с ней."""
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            raise ValueError("пустой текст")
        with lock:
            fixes = cc.load_json(self.fixes_path, {})
            fixes[rel] = text
            cc.save_json(self.fixes_path, fixes)
            clips = cc.load_json(self.clips_path, {})
            changed = {}
            for cid, r in clips.items():
                hit = False
                for p in r["parts"]:
                    if p["rel"] == rel:
                        p.update(text=text, src="fix", alt=None)
                        hit = True
                if hit:
                    cc.write_text(os.path.join(self.dir, r["voice"], cid + ".txt"), r["parts"])
                    changed[cid] = r
            cc.save_json(self.clips_path, clips)
            cc.write_ratings(self.dir, clips)
        return changed

    def audio(self, path):
        """/clip/<голос>/<id>.wav — клип партии, /src/<путь> — исходная реплика."""
        if path.startswith("/clip/"):
            return inside(self.dir, unquote(path[6:]))
        if path.startswith("/src/"):
            rel = unquote(path[5:])
            if rel.startswith("voice-refs/"):
                return inside(ROOT, rel)
            return inside(cc.ORIGS, rel)
        return None


class Handler(BaseHTTPRequestHandler):
    batch = None

    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype=None):
        if not path or not os.path.isfile(path):
            return self.send_error(404)
        size = os.path.getsize(path)
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else end
            else:
                start = size - int(m.group(2))
            end = min(end, size - 1)
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            self.wfile.write(f.read(end - start + 1))

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self.send_file(os.path.join(ROOT, "review.html"), "text/html; charset=utf-8")
        if path == "/api/state":
            return self.send_json(self.batch.state())
        if path.startswith(("/clip/", "/src/")):
            return self.send_file(self.batch.audio(path))
        self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if path == "/api/rate":
                rating = self.batch.rate(data["id"], data.get("score"), data.get("comment"), data.get("emotion"))
                return self.send_json(dict(ok=True, rating=rating))
            if path == "/api/tags":
                res = self.batch.tag(list(data["rels"]), data.get("add"), data.get("remove"), data.get("bad"))
                return self.send_json(dict(ok=True, **res))
            if path == "/api/text":
                return self.send_json(dict(ok=True, clips=self.batch.fix_text(data["rel"], data["text"])))
            self.send_error(404)
        except (KeyError, ValueError) as e:
            self.send_json(dict(ok=False, error=str(e)), 400)


def main():
    args = sys.argv[1:]
    port = 8765
    if "--port" in args:
        i = args.index("--port")
        port = int(args[i + 1])
        del args[i:i + 2]
    batches = sorted(d for d in os.listdir(cc.CLIPS) if os.path.isfile(os.path.join(cc.CLIPS, d, "clips.json")))
    if not batches:
        sys.exit("Нет партий: сначала python cut-clips.py b01")
    name = args[0] if args else batches[-1]
    if name not in batches:
        sys.exit(f"Нет партии {name}. Есть: {' '.join(batches)}")
    Handler.batch = Batch(name)
    Handler.batch.migrate()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Партия {name}: http://localhost:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
