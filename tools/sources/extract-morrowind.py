"""Реплики голосов Morrowind (рас и Дагот Ура) с текстом из ESM: данные для LoRA ESpeech и образец ~6 с.

Использование (venv проекта: numpy, scipy, onnxruntime, faster-whisper, torch, transformers):
    .venv/Scripts/python.exe tools/sources/extract-morrowind.py "<Morrowind>/Data Files" [--out voice-clips/espeech/morrowind]
        [--voices mw-dunmer-m,mw-dagoth-ur] [--max-min 10] [--min-min 6] [--max-cer 0.15] [--min-sim 0.5]
        [--seed 1] [--root <корень с models/>] [--lang ru|en]

Откуда что. Озвучка — Sound/Vo/<раса>/<m|f>/*.mp3 (в BSA голосов нет), Дагот Ур — Sound/Vo/Misc (Dagoth Ur *,
dagoth_*, Hit_DU*). Текст — субтитры игры: записи INFO в *.esm/*.esp (подзапись SNAM — звук, NAME — текст) и команды
`Say "файл" "текст"` в скриптах (там путь с .wav, игра играет .mp3). Кодировка русских ESM — cp1251.
--lang en — английская версия (GotY): ESM в cp1252, распознавание по-английски, голоса с суффиксом -en
(mw-nord-m-en) в том же наборе, кеш <out>/asr-en.json. Текст реплик — английский как есть: tools/espeech/train.py
прогоняет текст без «+» через нормализатор, а тот переводит латиницу в кириллицу, — такие реплики учатся отдельным
скриптом с текстом как есть.

Отбор:
- раса — только небоевые реплики (приветствия, болтовня, услуги, вор); боевые (Atk, CrAtk, Hit, Fle) — лишь если
  без них не набирается --min-min; у Дагот Ура берётся всё;
- текст чистится: @ссылка# → «ссылка», ремарки [Ворчит] убираются, реплики с %PCName и т. п. отбрасываются
  (в записи звучит не переменная);
- голос сверяется CAM++ (tools/voices/campplus.py): центр 20 самых похожих реплик, ниже --min-sim — чужой голос;
- реплики в случайном порядке (--seed) распознаются faster-whisper large-v3 (имена игры — подсказкой), CER против
  текста > --max-cer — текст не тот, реплика отброшена (у файла бывает несколько записей INFO — берётся ближайший
  текст). Распознавание идёт, пока годных не наберётся --max-min с запасом: у данмеров речи 30 мин, а нужно 10.
  Файл без текста в игре (у орка — услуги, Srv) идёт с текстом распознавания, только если голосу не хватает
  --min-min, и только от 1 с и двух слов: на коротких whisper ошибается;
- реплика длиннее 30 с (предел train.py) режется по самой длинной паузе у середины, текст — по предложениям;
- тишина по краям обрезана (−40 дБ от пика, запас 60 мс), 24 кГц моно PCM16.
Образец — спокойная (энергия tools/voices/emotions.py < 0,6) реплика 5–7,5 с без «!», текст совпал с
распознаванием, ближе к центру голоса; в обучение он не входит. Отложенные для проверки отделяет
tools/espeech/train.py сам (10 %).

Выход <out>/<голос>/: lora/<имя>.wav + .txt (вход tools/espeech/train.py), ref.wav + ref.txt (образец для
make-voice.py или train.py --wav/--text), clips.json — все реплики с CER, сходством, причиной отказа;
<out>/manifest.json — итог по голосам; <out>/asr.json — кеш распознавания (повторный запуск быстрый).
Голоса: mw-<раса>-<m|f> (argonian, breton, dunmer, altmer, imperial, khajiit, nord, orc, redguard, bosmer) и
mw-dagoth-ur. --root — корень проекта с models/frontend-onnx (по умолчанию — этот; из worktree — основная копия).
"""
import argparse, glob, importlib.util, json, os, random, re, site, struct, subprocess, wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SR = 24000

RACES = {"a": ("argonian", "Аргонианин"), "b": ("breton", "Бретонец"), "d": ("dunmer", "Данмер"),
         "h": ("altmer", "Альтмер"), "i": ("imperial", "Имперец"), "k": ("khajiit", "Каджит"),
         "n": ("nord", "Норд"), "o": ("orc", "Орк"), "r": ("redguard", "Редгард"), "w": ("bosmer", "Босмер")}
COMBAT = {"atk", "cratk", "hit", "fle", "batk", "bfle", "bhit"}
DAGOTH = re.compile(r"^misc/(dagoth.*|hit_du\d+)\.mp3$")
REF_SEC = (5.0, 7.5)
MAX_SEC = 30.0  # train.py пропускает реплики длиннее
CALM = 0.6  # энергия спокойной речи (emotions.py)
SPARE = 90  # с годной речи сверх --max-min: образец и запас на отказы
# Без подсказки whisper пишет «на Ревар», «Мурувинд»: имена из реплик (hotwords faster-whisper)
NAMES = ("Неревар Дагот Ур Морроувинд Вварденфелл Вивек Альмалексия Сота Сил Азура Балмора Сейда Нин Вивек Эбенгард "
         "Морнхолд Солстейм Хаджит Каджит Данмер Альтмер Босмер Норд Имперец Редгард Бретонец Аргонианин н'вах с'вит "
         "Телванни Хлаалу Редоран Акулахан Ординатор Храм Шестой Дом")
NAMES_EN = ("Nerevar Dagoth Ur Morrowind Vvardenfell Vivec Almalexia Sotha Sil Azura Balmora Seyda Neen Ebonheart "
            "Mournhold Solstheim Khajiit Dunmer Altmer Bosmer Nord Imperial Redguard Breton Argonian n'wah s'wit "
            "Telvanni Hlaalu Redoran Akulakhan Ordinator Tribunal Sixth House Skaal Outlander")
# Английская версия: ESM в cp1252, голоса — с суффиксом -en в том же наборе, свой кеш распознавания
LANGS = {"ru": dict(enc="cp1251", names=NAMES, suffix="", title="", cache="asr.json"),
         "en": dict(enc="cp1252", names=NAMES_EN, suffix="-en", title=" (англ.)", cache="asr-en.json")}


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- ESM ----------

def records(path):
    data = open(path, "rb").read()
    i = 0
    while i + 16 <= len(data):
        tag, size = struct.unpack_from("<4sI", data, i)
        yield tag, data[i + 16:i + 16 + size]
        i += 16 + size


def subs(rec):
    i = 0
    while i + 8 <= len(rec):
        tag, size = struct.unpack_from("<4sI", rec, i)
        yield tag, rec[i + 8:i + 8 + size]
        i += 8 + size


def norm_path(s):
    s = s.replace("\\", "/").lower().strip()
    s = s[3:] if s.startswith("vo/") else s
    return re.sub(r"\.wav$", ".mp3", s)


SAY = re.compile(r'say\s*,?\s*"([^"]+)"\s*,?\s*"([^"]*)"', re.I)


def game_texts(data_dir, enc="cp1251"):
    """→ {путь от Sound/Vo в нижнем регистре: [тексты]} из INFO (SNAM/NAME) и Say в скриптах всех *.esm/*.esp."""
    out = {}
    for pl in sorted(x for x in os.listdir(data_dir) if x.lower().endswith((".esm", ".esp"))):
        for tag, rec in records(os.path.join(data_dir, pl)):
            if tag == b"INFO":
                snd = text = None
                for st, sd in subs(rec):
                    if st == b"SNAM":
                        snd = sd.split(b"\0")[0].decode(enc, "replace")
                    elif st == b"NAME":
                        text = sd.split(b"\0")[0].decode(enc, "replace")
                if snd and text:
                    out.setdefault(norm_path(snd), []).append(text)
            if tag in (b"SCPT", b"INFO"):
                for st, sd in subs(rec):
                    if st in (b"SCTX", b"BNAM"):
                        for m in SAY.finditer(sd.decode(enc, "replace")):
                            out.setdefault(norm_path(m.group(1)), []).append(m.group(2))
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


def clean(text):
    """Текст субтитра → что звучит, или None (переменная вроде %PCName — в записи её нет)."""
    if "%" in text:
        return None
    t = re.sub(r"\[[^\]]*\]", " ", text).replace("@", "").replace("#", "")
    t = " ".join(t.split()).strip(" -")
    return t or None


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?…])\s+", text) if s]


# ---------- звук ----------

def decode(path):
    """→ float32 24 кГц моно или None: в игре есть битые файлы (i/m/srv_im032.mp3 обрезан)."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                       capture_output=True)
    return np.frombuffer(p.stdout, np.float32).copy() if p.returncode == 0 and p.stdout else None


def frames_db(x, hop=SR // 100):
    n = len(x) // hop
    rms = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(1) + 1e-12)
    return 20 * np.log10(rms / rms.max())


def trim(x, db=40.0, pad=0.06):
    """Обрезать тишину по краям: кадры 10 мс тише пика на db — тишина, запас pad с."""
    hop = SR // 100
    if len(x) < 3 * hop:
        return x
    loud = np.nonzero(frames_db(x) > -db)[0]
    a = max(0, loud[0] * hop - int(pad * SR))
    b = min(len(x), (loud[-1] + 1) * hop + int(pad * SR))
    return x[a:b]


def split_at_pause(x, db=40.0):
    """Длинная запись → две половины по самой длинной паузе в средней трети."""
    hop = SR // 100
    quiet = frames_db(x) < -db
    n = len(quiet)
    best, run, start = None, 0, 0
    for i in range(n // 3, 2 * n // 3):
        if quiet[i]:
            if run == 0:
                start = i
            run += 1
            if best is None or run > best[1]:
                best = (start, run)
        else:
            run = 0
    cut = (best[0] + best[1] // 2) * hop if best else len(x) // 2
    return trim(x[:cut]), trim(x[cut:])


def write_wav(path, x):
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def to16k(x):
    from scipy.signal import resample_poly
    return resample_poly(x, 2, 3).astype(np.float32)


# ---------- текст ----------

def letters(t):
    t = t.lower().replace("ё", "е")
    return " ".join(re.sub(r"[^a-zа-я0-9]+", " ", t).split())


def cer(ref, hyp):
    a, b = letters(ref), letters(hyp)
    if not a:
        return 1.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] / len(a)


class ASR:
    def __init__(self, cache_path, lang="ru", names=NAMES):
        self.path, self.lang, self.names = cache_path, lang, names
        self.cache = json.load(open(cache_path, encoding="utf-8")) if os.path.exists(cache_path) else {}
        self.model = None
        self.dirty = 0

    def __call__(self, key, x16):
        if key in self.cache:
            return self.cache[key]
        if self.model is None:
            sp = site.getsitepackages()[-1]  # cuBLAS/cuDNN: из пакетов nvidia-* или из torch (cu124 везёт их в torch/lib)
            for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")) + glob.glob(os.path.join(sp, "torch", "lib")):
                os.add_dll_directory(d)
                os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
            from faster_whisper import WhisperModel
            try:
                self.model = WhisperModel("large-v3", device="cuda", compute_type="float16")
            except Exception as e:
                print("faster-whisper на CPU:", e)
                self.model = WhisperModel("large-v3", device="cpu", compute_type="int8")
        # temperature 0 и предел токенов: на мычании и стонах whisper зацикливается («Хммм…» на 400 токенов) и
        # перезапускается с другой температурой — 5 с на реплику вместо 1
        segs, _ = self.model.transcribe(x16, language=self.lang, beam_size=5, temperature=0.0,
                                        without_timestamps=True, condition_on_previous_text=False, hotwords=self.names,
                                        max_new_tokens=int(len(x16) / 16000 * 12) + 10)
        text = " ".join(s.text.strip() for s in segs).strip()
        self.cache[key] = text
        self.dirty += 1
        if self.dirty >= 100:
            self.save()
        return text

    def save(self):
        if self.dirty:
            tmp = self.path + ".tmp"
            json.dump(self.cache, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            os.replace(tmp, self.path)
            self.dirty = 0


# ---------- голоса ----------

def voices(vo, suffix="", tail=""):
    """→ {имя: (название, [пути от Vo])}; suffix — к имени (-en), tail — к названию."""
    out = {}
    for code, (race, title) in RACES.items():
        for sex, word in (("m", "мужчина"), ("f", "женщина")):
            d = os.path.join(vo, code, sex)
            if os.path.isdir(d):
                files = sorted(f"{code}/{sex}/{n.lower()}" for n in os.listdir(d) if n.lower().endswith(".mp3"))
                out[f"mw-{race}-{sex}{suffix}"] = (f"{title}, {word}{tail}", files)
    misc = os.path.join(vo, "misc")
    if os.path.isdir(misc):
        files = sorted(f"misc/{n.lower()}" for n in os.listdir(misc) if DAGOTH.match(f"misc/{n.lower()}"))
        out["mw-dagoth-ur" + suffix] = ("Дагот Ур" + tail, files)
    return out


def kind(rel):
    if rel.startswith("misc/"):
        return "talk"
    m = re.match(r"([a-z]+)_", rel.rsplit("/", 1)[-1])
    return "combat" if m and m.group(1) in COMBAT else "talk"


def pieces(rel, x, game):
    """Файл → реплики [(ключ, звук, [тексты])]: длиннее MAX_SEC режется надвое, тексты — по предложениям."""
    if len(x) / SR <= MAX_SEC:
        return [(rel, x, game)]
    a, b = split_at_pause(x)
    heads, tails = [], []
    for t in game:
        s = sentences(t)
        for k in range(1, len(s)):
            heads.append((" ".join(s[:k]), " ".join(s[k:])))
    return [(rel + "#1", a, [h for h, _ in heads]), (rel + "#2", b, [t for _, t in heads])]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("data")
    ap.add_argument("--out", default=os.path.join(ROOT, "voice-clips", "espeech", "morrowind"))
    ap.add_argument("--voices")
    ap.add_argument("--max-min", type=float, default=10.0)
    ap.add_argument("--min-min", type=float, default=6.0)
    ap.add_argument("--max-cer", type=float, default=0.15)
    ap.add_argument("--min-sim", type=float, default=0.5)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--lang", default="ru", choices=sorted(LANGS))
    a = ap.parse_args()
    L = LANGS[a.lang]

    vo = os.path.join(a.data, "sound", "vo")
    if not os.path.isdir(vo):
        vo = os.path.join(a.data, "Sound", "Vo")
    os.makedirs(a.out, exist_ok=True)
    print("тексты из ESM/ESP…")
    texts = game_texts(a.data, L["enc"])
    allv = voices(vo, L["suffix"], L["title"])
    names = a.voices.split(",") if a.voices else list(allv)

    cam = load("campplus", "tools/voices/campplus.py")
    cam.ROOT = a.root  # модель CAM++ — в <root>/models/frontend-onnx
    emb = cam.Embedder(cache=None)
    emo = load("emotions", "tools/voices/emotions.py").Emotions(cache=None)
    asr = ASR(os.path.join(a.out, L["cache"]), a.lang, L["names"])
    mpath = os.path.join(a.out, "manifest.json")
    manifest = json.load(open(mpath, encoding="utf-8")) if os.path.exists(mpath) else {}

    for name in names:
        title, files = allv[name]
        print(f"== {name} ({title}): {len(files)} файлов", flush=True)
        clips = []
        for rel in files:
            x = decode(os.path.join(vo, rel))
            if x is None:
                print(f"   не читается: {rel}")
                clips.append(dict(file=rel, kind=kind(rel), sec=0, game=texts.get(rel, []), cands=[],
                                  reject="файл не читается", _x=None, _16=None, _e=None, sim=None))
                continue
            for key, x, game in pieces(rel, trim(x), texts.get(rel, [])):
                sec = len(x) / SR
                c = dict(file=key, kind=kind(rel), sec=round(sec, 2), game=game,
                         cands=[t for t in (clean(t) for t in game) if t])
                if not 0.3 <= sec <= MAX_SEC:
                    c["reject"] = f"{sec:.1f} с"
                elif game and not c["cands"]:
                    c["reject"] = "переменная в тексте"
                elif sec < 0.5:
                    c["reject"] = "короче 0,5 с: голос не сверить"
                c["_x"], c["_16"] = x, to16k(x)
                c["_e"] = cam.unit(emb.session.run(None, {"input": cam.fbank(c["_16"])[None]})[0][0]) \
                    if sec >= 0.5 else None
                clips.append(c)

        # центр голоса: 20 самых похожих на центр всех небоевых реплик с текстом, дважды
        base = [c for c in clips if c["_e"] is not None and "reject" not in c and c["cands"] and c["kind"] == "talk"]
        center = cam.unit(np.mean([c["_e"] for c in base], 0))
        for _ in range(2):
            top = sorted(base, key=lambda c: -float(c["_e"] @ center))[:20]
            center = cam.unit(np.mean([c["_e"] for c in top], 0))
        for c in clips:
            c["sim"] = round(float(c["_e"] @ center), 3) if c["_e"] is not None else None
            if "reject" not in c and (c["sim"] is None or c["sim"] < a.min_sim):
                c["reject"] = f"другой голос (сходство {c['sim']})"

        # распознавание в случайном порядке: небоевые с текстом, без текста, боевые — пока не наберётся
        rnd = random.Random(a.seed)
        todo = [c for c in clips if "reject" not in c]
        rnd.shuffle(todo)
        todo.sort(key=lambda c: (c["kind"] != "talk", not c["cands"]))
        good = 0.0
        for c in todo:
            talk_game = c["kind"] == "talk" and c["cands"]
            if not talk_game and good >= a.min_min * 60 + REF_SEC[1]:  # образец в обучение не входит
                c["skip"] = True
                continue
            if good >= a.max_min * 60 + SPARE:
                c["skip"] = True
                continue
            heard = asr(c["file"], c["_16"])
            c["asr"] = heard
            if c["cands"]:
                best = min(c["cands"], key=lambda t: cer(t, heard))
                c.update(text=best, src="game", cer=round(cer(best, heard), 3))
                if c["cer"] > a.max_cer:
                    c["reject"] = f"текст не совпал (CER {c['cer']:.2f})"
            elif c["sec"] >= 1.0 and len(letters(heard).split()) >= 2:  # «Уотон», «Нам сода?» — короткие врут
                c.update(text=heard, src="asr", cer=None)
            else:
                c["reject"] = "нет текста"
            if "reject" not in c:
                good += c["sec"]
        asr.save()

        ok = [c for c in todo if "reject" not in c and not c.get("skip")]
        # образец: спокойный, 5–7,5 с, без «!», текст совпал с распознаванием
        refc = [c for c in ok if c["kind"] == "talk" and c["src"] == "game" and REF_SEC[0] <= c["sec"] <= REF_SEC[1]
                and "!" not in c["text"] and c["cer"] <= 0.03 and re.search(r"[.?…]$", c["text"])]
        refc = sorted(refc, key=lambda c: -c["sim"])[:15]  # энергия — на CPU, ~2 с на реплику
        if refc and emo.model is None:
            # на CPU: torch на видеокарте рядом с faster-whisper падает в cuDNN («cudnnGetLibConfig», код 127)
            emo.load()
            emo.model, emo.device = emo.model.float().cpu(), "cpu"
        for c in refc:
            c["arousal"] = emo.run(c["_16"])[0]
        calm = [c for c in refc if c["arousal"] < CALM] or sorted(refc, key=lambda c: c["arousal"])[:5]
        ref = max(calm, key=lambda c: (c["sim"], -abs(c["sec"] - 6.3))) if calm else None

        chosen, total = [], 0.0
        for c in ok:  # порядок todo: небоевые с текстом игры, затем с распознанным, затем боевые
            if c is ref or total + c["sec"] > a.max_min * 60:
                continue
            if c["kind"] == "combat" and total >= a.min_min * 60:
                continue
            chosen.append(c)
            total += c["sec"]

        vdir = os.path.join(a.out, name)
        ldir = os.path.join(vdir, "lora")
        os.makedirs(ldir, exist_ok=True)
        for f in glob.glob(os.path.join(ldir, "*.wav")) + glob.glob(os.path.join(ldir, "*.txt")):
            os.remove(f)
        for c in chosen:
            stem = re.sub(r"[^\w-]+", "_", c["file"].replace(".mp3", ""))
            write_wav(os.path.join(ldir, stem + ".wav"), c["_x"])
            open(os.path.join(ldir, stem + ".txt"), "w", encoding="utf-8").write(c["text"] + "\n")
            c["chosen"] = True
        if ref:
            write_wav(os.path.join(vdir, "ref.wav"), ref["_x"])
            open(os.path.join(vdir, "ref.txt"), "w", encoding="utf-8").write(ref["text"] + "\n")
            ref["ref"] = True
        for c in clips:
            for k in ("_x", "_16", "_e"):
                c.pop(k)
        json.dump(clips, open(os.path.join(vdir, "clips.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        rej = {}
        for c in clips:
            if "reject" in c:
                k = re.sub(r"\s*\(.*", "", c["reject"])
                rej[k] = rej.get(k, 0) + 1
        manifest[name] = dict(
            title=title, clips=len(chosen), minutes=round(total / 60, 2),
            combat=sum(c["kind"] == "combat" for c in chosen), asr_text=sum(c["src"] == "asr" for c in chosen),
            checked=sum("asr" in c for c in clips), files=len(files), rejected=rej,
            ref=dict(file=ref["file"], sec=ref["sec"], text=ref["text"], arousal=ref["arousal"], sim=ref["sim"])
            if ref else None)
        m = manifest[name]
        print(f"   {m['clips']} реплик, {m['minutes']} мин (распознано {m['checked']}), отказ: {rej}")
        print(f"   образец: {m['ref']}", flush=True)
        json.dump(manifest, open(mpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
