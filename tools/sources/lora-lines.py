"""Реплики мемных голосов (Пудж, GLaDOS, бандит, Чувак, ***, ***, Neco Arc) для LoRA ESpeech и образец ~6 с.

Использование (venv проекта: numpy, scipy, onnxruntime, faster-whisper, torch, transformers; японский — pykakasi):
    .venv/Scripts/python.exe tools/sources/lora-lines.py [голос …] [--root <корень>] [--max-min 10]
        [--max-cer 0.15] [--seed 1] [--ref <голос>=<файл>[+<файл>…]] [--candidates N] [--list]
  голос   ключ VOICES ниже (dota2-pudge, portal-glados, stalker-bandit, postal-dude, ***, ***,
          neco-arc-ja); по умолчанию все.
  --root  корень проекта с voice-origs/, voice-clips/, models/ (по умолчанию этот; из worktree — основная копия).
  --ref   образец вручную: реплики (ключи clips.json, «<источник>/<файл>») подряд с паузой 0,25 с; без него —
          ref_parts голоса в VOICES, если есть.
  --candidates N  только подобрать образец: N лучших в <голос>/ref-cand/NN.wav + .txt, остальное не трогается.
  --list  показать голоса и источники.

Источники и текст (VOICES): папка с lines.json (fetch-wiki-lines.py, transcribe-lines.py) или папка звуков по глобу.
Текст бывает трёх видов:
  given — текст вики или игры (Пудж, GLaDOS): сверяется с faster-whisper large-v3, CER > --max-cer — отказ;
  long  — расшифровка длинной записи transcribe-lines.py (Чувак, ***, ***, Neco Arc): реплика распознаётся ещё раз
          отдельно, при совпадении (CER ≤ --max-cer, у японского — по чтению каной, ≤ 0,25) берётся новый текст —
          у него те же границы, что у звука; иначе отказ (края резали посреди слова, или распознано по-разному);
  asr     — текста нет (бандит S.T.A.L.K.E.R.): текст распознавания, если реплика от 1 с и в ней от двух слов.
Голос сверяется CAM++ (tools/voices/campplus.py): центр — эталоны голоса (center) или все реплики, затем дважды центр
20 самых похожих; ниже min_sim — чужой голос (в летсплее *** — сам скуф, в интервью *** — собеседник).
Отбрасываются: звуки по exclude (музыка, смех, дыхание, рация «_pda» у бандита), галлюцинации Whisper на тишине
(«Субтитры…», «Продолжение следует»), повторы одного текста, реплики короче 0,5 с. Длиннее 30 с (предел train.py)
режутся по самой длинной паузе у середины. Тишина по краям обрезана (−40 дБ от пика, запас 60 мс), 24 кГц моно PCM16.
Реплики берутся в порядке prefer (спокойная речь раньше боевых криков), до --max-min минут.

Японский (neco-arc-ja): в словаре ESpeech нет японских знаков, поэтому текст переводится в кириллицу по Поливанову
(чтение каной — pykakasi: 猫 → ねこ → «нэко»). Как английские голоса Morrowind (docs/espeech.md), это голос
с акцентом: синтез по-русски, адаптер учит тембр и манеру. Варианты (neco-arc, neco-arc-angry) делят адаптер
neco-arc-ja, у них свой только образец (пример JeetBot по-русски).

Образец — спокойная (энергия tools/voices/emotions.py < 0,6) реплика 5–7,5 с или 2–3 соседние реплики одной
записи, темп 10–15 букв/с, текст сверен с распознаванием; в обучение он не входит. ref_parts голоса задаёт
образец вручную, как --ref. Вариант голоса с parts (postal-dude-angry) — образец из реплик самого голоса, обычно
напористых: ref.wav варианта, адаптер — базового голоса (make-voice.py --lora). Текст реплики без знака в конце
получает точку.

Выход voice-clips/espeech/<набор>/<голос>/: lora/<имя>.wav + .txt (вход tools/espeech/train.py), ref.wav + ref.txt,
clips.json — все реплики с текстом, CER, сходством, причиной отказа; у варианта — только ref.wav + ref.txt.
voice-clips/espeech/<набор>/manifest.json — {голос: {title, clips, minutes, steps, src, rejected, ref, variants}},
у варианта {title, base, ref}; steps — по правилу пользователя (от 4 мин — 3000, ~2 мин — 2000, ~1 мин — 1000,
меньше — 500). Кеш распознавания — voice-clips/espeech/<набор>/asr.json. Нужен ffmpeg в PATH.
"""
import argparse, glob, importlib.util, json, os, random, re, site, wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("common", os.path.join(os.path.dirname(HERE), "common.py"))
common = importlib.util.module_from_spec(spec)  # ROOT, load, ffmpeg, звук, JSON
spec.loader.exec_module(common)
ROOT = common.ROOT
SR = 24000
MAX_SEC = 30.0  # train.py пропускает реплики длиннее
REF_SEC = (5.0, 7.5)
REF_RATE = (10.0, 15.0)  # букв/с: медленный образец даёт лишние слова в конце (docs/espeech.md)
CALM = 0.6  # энергия спокойной речи (emotions.py)
JB = "voice-origs/jeetbot"
PD = "voice-origs/postal-dude/lines"

# src: папка (от корня) с lines.json или dict(dir, glob, until — конец записи, с); prefer — регэкспы ключа по убыванию
# предпочтения; center — эталоны голоса для CAM++
VOICES = {
    "dota2-pudge": dict(set="dota2", title="Пудж (Dota 2)", text="given", src=["voice-origs/dota2-pudge"],
                        min_sim=0.5),
    "portal-glados": dict(set="portal", title="GLaDOS (Portal 2)", text="given",
                          src=["voice-origs/portal-glados-ru"],  # fetch-portal2.py; 8 реплик вики — в ней же
                          center=["voice-origs/portal-glados/000_GLaDOS_chellgladoswakeup05_ru.wav",
                                  "voice-origs/portal-glados/002_GLaDOS_sp_laser_powered_lift_completion02_ru.wav"],
                          min_sim=0.6),
    "stalker-bandit": dict(set="stalker", title="Бандит (S.T.A.L.K.E.R.)", text="asr",
                           src=[dict(dir="voice-origs/stalker-cop/sounds/characters_voice/human_03/bandit",
                                     glob="**/*.ogg")],
                           # музыка (гитара), дыхание, смех, рация (_pda — та же реплика через фильтр рации)
                           exclude=r"^music/|states/breath|reactions/(story|music)|_pda\.",
                           prefer=[r"talk/|states/(idle|meet|loot|sleep)|reactions/joke", r"threat/|help/",
                                   r"alife/", r"fight/"],
                           min_sim=0.5),
    # сборник «Postal 2 Все фразы Чувака» (LZNw8t1k1rI), озвучка Акеллы; transcribe-lines.py
    "postal-dude": dict(set="postal", title="Чувак (Postal 2)", text="long", src=["voice-origs/postal-dude/lines"],
                        min_sim=0.6,
                        # спокойных кусков 5–7,5 с у него один, и тот 9,7 букв/с: образец — две соседние реплики
                        ref_parts=[f"{PD}/394.wav", f"{PD}/395.wav"],
                        # злой: самые напористые реплики (энергия 0,93 и 0,80, emotions.py)
                        variants={"postal-dude-angry": dict(parts=[f"{PD}/298.wav", f"{PD}/295.wav"],
                                                            title="Злой Чувак (Postal 2)")}),
    "***": dict(set="streamers", title="***", text="long",
                   src=["voice-origs/***-qa", "voice-origs/***-nS29"],
                   center=["voice-origs/***-qa/Q01-093-112.wav", "voice-origs/***-nS29/P01-032-054.wav"],
                   min_sim=0.6),
    "***": dict(set="altushka", title="***, внутренний голос («Альтушка для скуфа»)", text="long",
                   src=["voice-origs/***-altushka"],
                   center=["voice-origs/***-altushka/018.wav", "voice-origs/***-altushka/080.wav",
                           "voice-origs/***-altushka/091.wav"],
                   min_sim=0.73),  # внутренний голос 0,74–0,95, сам скуф 0,45–0,72
    "neco-arc-ja": dict(set="neco-arc", title="Neco Arc (японский оригинал)", text="long", lang="ja",
                        src=["voice-origs/neco-arc-ja"], min_sim=0.5, max_cer=0.25,
                        variants={"neco-arc": dict(ref=f"{JB}/neco.mp3", title="Neco Arc"),
                                  "neco-arc-angry": dict(ref=f"{JB}/angry_neco_arc.mp3", title="Злая Neco Arc")}),
}

# галлюцинации Whisper на тишине и шуме — не текст реплики
JUNK = re.compile(r"субтитр|продолжение следует|dimatorzok|спасибо за просмотр|подписывайтесь|редактор", re.I)


def steps_for(minutes):
    """Правило пользователя: меньше 1 мин — 500, ~1 мин — 1000, ~2 мин — 2000, от 4 мин — 3000 (как wc3/lora-clips.py)."""
    s = 1000 * minutes if minutes <= 2 else 2000 + 500 * (minutes - 2)
    return int(min(3000, max(500, round(s / 500) * 500)))


# ---------- звук ----------

def trim(x, db=40.0, pad=0.06):
    hop = SR // 100
    if len(x) < 3 * hop:
        return x
    loud = np.nonzero(common.frames_db(x) > -db)[0]
    a = max(0, loud[0] * hop - int(pad * SR))
    b = min(len(x), (loud[-1] + 1) * hop + int(pad * SR))
    return x[a:b]


def split_at_pause(x, db=40.0):
    """Длинная запись → две половины по самой длинной паузе в средней трети."""
    hop = SR // 100
    quiet = common.frames_db(x) < -db
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
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())


def to16k(x):
    from scipy.signal import resample_poly
    return resample_poly(x, 2, 3).astype(np.float32)


# ---------- текст ----------

def letters(t):
    t = t.lower().replace("ё", "е")
    return " ".join(re.sub(r"[^\w]+", " ", t).replace("_", " ").split())


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


INTERJ = re.compile(r"^(?:[хк]?[хм]*[еэаыоу]?[хм]*|ням|ном|ом|ну|э+р*|а+|о+|у+|м+|р+)$")


def core(t):
    """Текст без междометий («М-м-м», «Хе-хе-хе», «Э-э»): Whisper их пропускает или пишет по-своему."""
    words = [w for w in letters(t).split() if not all(INTERJ.match(p) for p in w.split("-") if p)]
    return " ".join(words)


def wiki_text(t):
    """Пометки вики Dota 2 перед репликой: время восстановления («10 »), «п» — предмет, «н» — новое."""
    return re.sub(r"^(?:\d+|[пн])\s+", "", t.strip())


def ru_letters(t):
    return len(re.sub(r"[^а-яё]", "", t.lower()))


def sentences(text):
    return [s for s in re.split(r"(?<=[.!?…])\s+", text) if s]


# Поливанов: кана → кириллица (чтение — pykakasi). Двусложные (кя, сё) раньше одиночных; っ удваивает согласную,
# ー тянет гласную (повтор буквы), い/う после гласной — й/у.
KANA = {
    "きゃ": "кя", "きゅ": "кю", "きょ": "кё", "しゃ": "ся", "しゅ": "сю", "しょ": "сё", "ちゃ": "тя", "ちゅ": "тю",
    "ちょ": "тё", "にゃ": "ня", "にゅ": "ню", "にょ": "нё", "ひゃ": "хя", "ひゅ": "хю", "ひょ": "хё", "みゃ": "мя",
    "みゅ": "мю", "みょ": "мё", "りゃ": "ря", "りゅ": "рю", "りょ": "рё", "ぎゃ": "гя", "ぎゅ": "гю", "ぎょ": "гё",
    "じゃ": "дзя", "じゅ": "дзю", "じょ": "дзё", "びゃ": "бя", "びゅ": "бю", "びょ": "бё", "ぴゃ": "пя", "ぴゅ": "пю",
    "ぴょ": "пё", "ふぁ": "фа", "ふぃ": "фи", "ふぇ": "фэ", "ふぉ": "фо", "てぃ": "ти", "でぃ": "ди", "とぅ": "ту",
    "うぃ": "уи", "うぇ": "уэ", "うぉ": "уо", "しぇ": "сэ", "じぇ": "дзэ", "ちぇ": "тэ", "ゔぁ": "ва", "ゔぃ": "ви",
    "ゔぇ": "вэ", "ゔぉ": "во",
    "あ": "а", "い": "и", "う": "у", "え": "э", "お": "о", "か": "ка", "き": "ки", "く": "ку", "け": "кэ", "こ": "ко",
    "さ": "са", "し": "си", "す": "су", "せ": "сэ", "そ": "со", "た": "та", "ち": "ти", "つ": "цу", "て": "тэ",
    "と": "то", "な": "на", "に": "ни", "ぬ": "ну", "ね": "нэ", "の": "но", "は": "ха", "ひ": "хи", "ふ": "фу",
    "へ": "хэ", "ほ": "хо", "ま": "ма", "み": "ми", "む": "му", "め": "мэ", "も": "мо", "や": "я", "ゆ": "ю",
    "よ": "ё", "ら": "ра", "り": "ри", "る": "ру", "れ": "рэ", "ろ": "ро", "わ": "ва", "を": "о", "ん": "н",
    "が": "га", "ぎ": "ги", "ぐ": "гу", "げ": "гэ", "ご": "го", "ざ": "дза", "じ": "дзи", "ず": "дзу", "ぜ": "дзэ",
    "ぞ": "дзо", "だ": "да", "ぢ": "дзи", "づ": "дзу", "で": "дэ", "ど": "до", "ば": "ба", "び": "би", "ぶ": "бу",
    "べ": "бэ", "ぼ": "бо", "ぱ": "па", "ぴ": "пи", "ぷ": "пу", "ぺ": "пэ", "ぽ": "по", "ゔ": "ву",
    "ぁ": "а", "ぃ": "и", "ぅ": "у", "ぇ": "э", "ぉ": "о", "ゃ": "я", "ゅ": "ю", "ょ": "ё", "ゎ": "ва",
}
PUNCT = {"、": ", ", "。": ". ", "？": "? ", "！": "! ", "?": "? ", "!": "! ", "…": "… ", "・": " ", "　": " ", " ": " ",
         "「": "", "」": "", "『": "", "』": "", "〜": "", "~": ""}
VOWELS = "аиуэояюё"


class Kana:
    def __init__(self):
        import pykakasi
        self.k = pykakasi.kakasi()

    def reading(self, text):
        """Японский текст → хирагана, слова (токены pykakasi) через пробел; частицы は, へ читаются «ва», «э»."""
        particle = {"は": "わ", "へ": "え"}
        h = " ".join(particle.get(x["hira"], x["hira"]) for x in self.k.convert(text))
        return re.sub(r"([っッ])\s+", r"\1", h)  # 待っ|て — удвоение переходит на следующий токен

    def cyr(self, text):
        h = self.reading(text)
        out, i, dbl = [], 0, False
        while i < len(h):
            ch = h[i]
            if ch in "っッ":
                dbl = True
                i += 1
                continue
            if ch == "ー":
                last = next((c for c in reversed("".join(out)) if c in VOWELS), "")
                out.append({"я": "а", "ю": "у", "ё": "о"}.get(last, last))
                i += 1
                continue
            syl = KANA.get(h[i:i + 2]) or KANA.get(ch)
            step = 2 if KANA.get(h[i:i + 2]) else 1
            if syl is None:
                out.append(PUNCT.get(ch, " " if not ch.isalnum() else ""))
                i += 1
                dbl = False
                continue
            prev = "".join(out)[-1:]
            if ch == "い" and step == 1 and prev in VOWELS and prev not in "и":
                syl = "й"
            if dbl and syl[0] not in VOWELS:
                syl = syl[0] + syl
            dbl = False
            out.append(syl)
            i += step
        t = re.sub(r"\s+", " ", "".join(out)).strip()
        t = re.sub(r"\s+([,.?!…])", r"\1", t)
        return t[:1].upper() + t[1:]


class ASR:
    def __init__(self, path, lang):
        self.path, self.lang = path, lang
        self.cache = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
        self.model, self.dirty = None, 0

    def __call__(self, key, x16):
        k = f"{self.lang}:{key}"
        if k in self.cache:
            return self.cache[k]
        if self.model is None:
            sp = site.getsitepackages()[-1]  # cuBLAS/cuDNN — из torch/lib (cu124) или пакетов nvidia-*
            for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")) + glob.glob(os.path.join(sp, "torch", "lib")):
                os.add_dll_directory(d)
                os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
            from faster_whisper import WhisperModel
            self.model = WhisperModel("large-v3", device="cuda", compute_type="float16")
        # temperature 0 и предел токенов: на стонах и мяуканье whisper зацикливается (extract-morrowind.py)
        segs, _ = self.model.transcribe(x16, language=self.lang, beam_size=5, temperature=0.0,
                                        without_timestamps=True, condition_on_previous_text=False,
                                        max_new_tokens=int(len(x16) / 16000 * 12) + 10)
        text = " ".join(s.text.strip() for s in segs).strip()
        self.cache[k] = text
        self.dirty += 1
        if self.dirty >= 50:
            self.save()
        return text

    def save(self):
        if self.dirty:
            json.dump(self.cache, open(self.path + ".tmp", "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            os.replace(self.path + ".tmp", self.path)
            self.dirty = 0


# ---------- реплики ----------

def sources(root, cfg):
    """→ [(ключ, путь, текст или None, порядок записи)]; ключ — «<папка от корня>/<файл>»."""
    out = []
    rx = re.compile(cfg["exclude"]) if cfg.get("exclude") else None
    for s in cfg["src"]:
        s = s if isinstance(s, dict) else dict(dir=s)
        d = os.path.join(root, s["dir"])
        if not os.path.isdir(d):
            print(f"   нет папки {s['dir']}")
            continue
        lj = os.path.join(d, "lines.json")
        if "glob" not in s and os.path.exists(lj):
            items = [(x["file"], x.get("text")) for x in json.load(open(lj, encoding="utf-8"))
                     if not (s.get("until") and x.get("end", 0) > s["until"])]
        else:
            items = [(os.path.relpath(f, d).replace("\\", "/"), None)
                     for f in sorted(glob.glob(os.path.join(d, s.get("glob", "*.wav")), recursive=True))]
        for n, (rel, text) in enumerate(items):
            if rx and rx.search(rel):  # путь внутри папки источника
                continue
            out.append((f"{s['dir']}/{rel}", os.path.join(d, rel), text, (s["dir"], n)))
    return out


def pieces(key, x, text):
    """Длиннее MAX_SEC — надвое по паузе; данный текст делится по предложениям (подходящее деление — по ASR)."""
    if len(x) / SR <= MAX_SEC:
        return [(key, x, [text] if text else [])]
    a, b = split_at_pause(x)
    heads = []
    if text:
        s = sentences(text)
        heads = [(" ".join(s[:k]), " ".join(s[k:])) for k in range(1, len(s))]
    return [(key + "#1", a, [h for h, _ in heads]), (key + "#2", b, [t for _, t in heads])]


def prefer_rank(cfg, key):
    for i, rx in enumerate(cfg.get("prefer", [])):
        if re.search(rx, key):
            return i
    return len(cfg.get("prefer", []))


def build(name, cfg, a, cam, emb, emo, kana):
    out = os.path.join(a.root, "voice-clips", "espeech", cfg["set"])
    os.makedirs(out, exist_ok=True)
    lang = cfg.get("lang", "ru")
    max_cer = cfg.get("max_cer", a.max_cer)
    asr = ASR(os.path.join(out, "asr.json"), lang)
    srcs = sources(a.root, cfg)
    print(f"== {name}: {len(srcs)} файлов", flush=True)

    clips = []
    for key, path, text, order in srcs:
        x = common.decode(path)
        if x is None or not len(x):
            clips.append(dict(file=key, sec=0, reject="файл не читается"))
            continue
        for k, xx, cands in pieces(key, trim(x), text):
            sec = len(xx) / SR
            c = dict(file=k, sec=round(sec, 2), src_text=text, order=order, _x=xx, _16=to16k(xx), _cands=cands)
            if sec < 0.5:
                c["reject"] = "короче 0,5 с"
            elif sec > MAX_SEC:
                c["reject"] = f"{sec:.1f} с"
            c["_e"] = cam.unit(emb.session.run(None, {"input": cam.fbank(c["_16"])[None]})[0][0]) \
                if sec >= 0.5 else None
            clips.append(c)

    # центр голоса: эталоны (или все реплики), затем дважды — 20 самых похожих
    have = [c for c in clips if c.get("_e") is not None]
    if cfg.get("center"):
        center = cam.unit(np.mean([cam.unit(emb.session.run(None, {"input": cam.fbank(to16k(trim(common.decode(
            os.path.join(a.root, f)))))[None]})[0][0]) for f in cfg["center"]], 0))
    else:
        center = cam.unit(np.mean([c["_e"] for c in have], 0))
    for _ in range(2):
        top = sorted(have, key=lambda c: -float(c["_e"] @ center))[:20]
        center = cam.unit(np.mean([c["_e"] for c in top], 0))
    for c in have:
        c["sim"] = round(float(c["_e"] @ center), 3)
        if "reject" not in c and c["sim"] < cfg["min_sim"]:
            c["reject"] = f"другой голос (сходство {c['sim']})"
    sims = sorted(c["sim"] for c in have)
    print("   сходство: " + " ".join(f"{q:.0%} {sims[int(q * (len(sims) - 1))]:.2f}" for q in (.1, .25, .5, .75, .9)))

    seen = set()
    for c in clips:
        if "reject" in c:
            continue
        heard = asr(c["file"], c["_16"])
        c["asr"] = heard
        if JUNK.search(heard):
            c["reject"] = "галлюцинация Whisper"
        elif cfg["text"] == "given":
            cands = [wiki_text(t) for t in c["_cands"]]
            if not cands or not core(cands[0]) and not core(heard):
                c["reject"] = "нет текста"  # одни междометия: голос не сверить, текст не проверить
            else:
                best = min(cands, key=lambda t: cer(core(t), core(heard)))
                c.update(text=best, cer=round(cer(core(best), core(heard)), 3))
                if c["cer"] > max_cer:
                    c["reject"] = f"текст не совпал (CER {c['cer']:.2f})"
        elif cfg["text"] == "long":
            src = c["_cands"][0] if c["_cands"] else ""
            r = (lambda t: kana.reading(t)) if lang == "ja" else (lambda t: t)
            c["cer"] = round(cer(r(src), r(heard)), 3) if src else None
            if c["cer"] is None or c["cer"] > max_cer:
                c["reject"] = f"распознано по-разному (CER {c['cer']})"
            else:
                c["text"] = heard
        else:
            if c["sec"] >= 1.0 and len(letters(heard).split()) >= 2:
                c["text"] = heard
            else:
                c["reject"] = "нет текста"
        if "reject" not in c and lang == "ja":
            c["ja"] = c["text"]
            c["text"] = kana.cyr(c["text"])
            if not ru_letters(c["text"]):
                c["reject"] = "нет текста"
        if "reject" not in c:
            t = letters(c["text"])
            if t in seen:
                c["reject"] = "повтор текста"
            seen.add(t)
    asr.save()

    ok = [c for c in clips if "reject" not in c]
    for c in ok:
        c["rate"] = round(ru_letters(c["text"]) / c["sec"], 1)
    vdir = os.path.join(out, name)
    ref = pick_ref(name, ok, a, emo, vdir)
    if a.candidates:
        return None
    refkeys = {c["file"] for c in ref} if ref else set()

    rnd = random.Random(a.seed)
    pool = [c for c in ok if c["file"] not in refkeys]
    rnd.shuffle(pool)
    pool.sort(key=lambda c: (prefer_rank(cfg, c["file"]), -c["sim"]))
    chosen, total = [], 0.0
    for c in pool:
        if total + c["sec"] > a.max_min * 60:
            continue
        chosen.append(c)
        total += c["sec"]

    ldir = os.path.join(vdir, "lora")
    os.makedirs(ldir, exist_ok=True)
    for f in glob.glob(os.path.join(ldir, "*.wav")) + glob.glob(os.path.join(ldir, "*.txt")):
        os.remove(f)
    for c in chosen:
        stem = re.sub(r"[^\w-]+", "_", re.sub(r"\.(wav|mp3|ogg)", "", c["file"].split("/", 2)[-1]))
        write_wav(os.path.join(ldir, stem + ".wav"), c["_x"])
        open(os.path.join(ldir, stem + ".txt"), "w", encoding="utf-8").write(c["text"] + "\n")
        c["chosen"] = True
    refinfo = None
    if ref:
        x = join([c["_x"] for c in ref])
        write_wav(os.path.join(vdir, "ref.wav"), x)
        text = join_text(c["text"] for c in ref)
        open(os.path.join(vdir, "ref.txt"), "w", encoding="utf-8").write(text + "\n")
        for c in ref:
            c["ref"] = True
        refinfo = dict(parts=[c["file"] for c in ref], sec=round(len(x) / SR, 2), text=text,
                       rate=round(ru_letters(text) / (len(x) / SR), 1),
                       arousal=round(float(np.mean([c["arousal"] for c in ref])), 3))
        if lang == "ja":
            refinfo["ja"] = " ".join(c["ja"] for c in ref)
    for c in clips:
        for k in ("_x", "_16", "_e", "_cands"):
            c.pop(k, None)
    json.dump(clips, open(os.path.join(vdir, "clips.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    rej = {}
    for c in clips:
        if "reject" in c:
            k = re.sub(r"\s*\(.*", "", c["reject"])
            rej[k] = rej.get(k, 0) + 1
    minutes = total / 60
    m = dict(title=cfg["title"], clips=len(chosen), minutes=round(minutes, 2), steps=steps_for(minutes),
             files=len(srcs), src=[s if isinstance(s, str) else s["dir"] for s in cfg["src"]], text=cfg["text"],
             lang=lang, rejected=rej, ref=refinfo, variants=list(cfg.get("variants", {})))
    print(f"   {m['clips']} реплик, {m['minutes']} мин → {m['steps']} шагов; отказ: {rej}")
    print(f"   образец: {refinfo}", flush=True)
    return m


def join_text(texts):
    """Тексты реплик подряд; у реплики без знака в конце (Whisper его опускает) — точка."""
    return " ".join(t if re.search(r"[.!?…]$", t) else t + "." for t in (t.strip() for t in texts))


def join(parts, gap=0.25):
    sil = np.zeros(int(gap * SR), np.float32)
    out = []
    for i, x in enumerate(parts):
        out += ([sil] if i else []) + [x]
    return np.concatenate(out)


def emo_cpu(emo):
    if emo.model is None:
        # на CPU: torch на видеокарте рядом с faster-whisper падает в cuDNN (extract-morrowind.py)
        emo.load()
        emo.model, emo.device = emo.model.float().cpu(), "cpu"


def pick_ref(name, ok, a, emo, vdir):
    """Образец: одна реплика 5–7,5 с или 2–3 соседние одной записи; спокойный, темп 10–15 букв/с, ближе к центру."""
    byfile = {c["file"]: c for c in ok}
    manual = dict(r.split("=", 1) for r in a.ref).get(name) or "+".join(VOICES[name].get("ref_parts", []))
    if manual:
        grp = [byfile[k] for k in manual.split("+")]
        emo_cpu(emo)
        for g in grp:
            g["arousal"] = round(float(emo.run(g["_16"])[0]), 3)
        return grp
    cands = []
    seq = sorted(ok, key=lambda c: c["order"])
    for i in range(len(seq)):
        for n in (1, 2, 3):
            grp = seq[i:i + n]
            if len(grp) < n or any(g["order"][0] != grp[0]["order"][0] or g["order"][1] - grp[0]["order"][1] != j
                                   for j, g in enumerate(grp)):
                break
            sec = sum(g["sec"] for g in grp) + 0.25 * (n - 1)
            if not REF_SEC[0] <= sec <= REF_SEC[1]:
                continue
            text = " ".join(g["text"] for g in grp)
            rate = ru_letters(text) / sec
            if "!" in text or not re.search(r"[.?…]$", text.strip()):
                continue
            if any(g.get("cer") is not None and g["cer"] > 0.05 for g in grp):
                continue
            off = max(0.0, REF_RATE[0] - rate, rate - REF_RATE[1])
            cands.append(dict(grp=grp, sec=sec, rate=rate, off=off, sim=float(np.mean([g["sim"] for g in grp]))))
    if not cands:
        print("   образца нет: нет спокойных кусков 5–7,5 с с точкой на конце")
        return None
    cands.sort(key=lambda r: (round(r["off"]), len(r["grp"]), -r["sim"]))
    cands = cands[:12]
    emo_cpu(emo)
    for r in cands:
        for g in r["grp"]:
            if "arousal" not in g:
                g["arousal"] = round(float(emo.run(g["_16"])[0]), 3)
        r["arousal"] = float(np.mean([g["arousal"] for g in r["grp"]]))
    calm = [r for r in cands if r["arousal"] < CALM] or sorted(cands, key=lambda r: r["arousal"])[:4]
    calm.sort(key=lambda r: (round(r["off"]), len(r["grp"]), -r["sim"]))
    if a.candidates:
        cd = os.path.join(vdir, "ref-cand")
        os.makedirs(cd, exist_ok=True)
        for n, r in enumerate(calm[:a.candidates], 1):
            write_wav(os.path.join(cd, f"{n:02d}.wav"), join([g["_x"] for g in r["grp"]]))
            open(os.path.join(cd, f"{n:02d}.txt"), "w", encoding="utf-8").write(
                " ".join(g["text"] for g in r["grp"]) + "\n")
            print(f"   {n:02d}: {r['sec']:.1f} с, {r['rate']:.1f} букв/с, энергия {r['arousal']:.2f}, сходство "
                  f"{r['sim']:.2f} — {'+'.join(g['file'] for g in r['grp'])}")
        return None
    return calm[0]["grp"]


def variant(name, cfg, base, base_name, a):
    """Вариант: свой образец, адаптер — базового голоса. Образец — пример JeetBot с его текстом (ref) или реплики
    самого голоса подряд (parts — ключи clips.json базового голоса, текст оттуда же)."""
    out = os.path.join(a.root, "voice-clips", "espeech", base["set"])
    if cfg.get("parts"):
        clips = json.load(open(os.path.join(out, base_name, "clips.json"), encoding="utf-8"))
        texts = {c["file"]: c["text"] for c in clips if c.get("text")}
        parts = cfg["parts"]
        x = join([trim(common.decode(os.path.join(a.root, p))) for p in parts])
        text = join_text(texts[p] for p in parts)
    else:
        lines = {x["file"]: x["text"] for x in json.load(open(os.path.join(a.root, JB, "lines.json"),
                                                               encoding="utf-8"))}
        parts = [cfg["ref"]]
        x = trim(common.decode(os.path.join(a.root, cfg["ref"])))
        text = lines[os.path.basename(cfg["ref"])]
    vdir = os.path.join(out, name)
    os.makedirs(vdir, exist_ok=True)
    write_wav(os.path.join(vdir, "ref.wav"), x)
    open(os.path.join(vdir, "ref.txt"), "w", encoding="utf-8").write(text + "\n")
    sec = len(x) / SR
    print(f"   вариант {name}: {sec:.1f} с, {ru_letters(text) / sec:.1f} букв/с — {text}")
    return dict(title=cfg["title"], base=None, ref=dict(parts=parts, sec=round(sec, 2), text=text,
                                                         rate=round(ru_letters(text) / sec, 1)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("voices", nargs="*")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--max-min", type=float, default=10.0)
    ap.add_argument("--max-cer", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--ref", action="append", default=[])
    ap.add_argument("--candidates", type=int, default=0)
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list:
        for n, c in VOICES.items():
            print(f"{c['set']}/{n}: {c['title']}, текст {c['text']}, {[s if isinstance(s, str) else s['dir'] for s in c['src']]}")
        return
    names = a.voices or list(VOICES)
    cam = common.load("tools/voices/campplus.py")
    cam.ROOT = a.root  # модель CAM++ — в <root>/models/frontend-onnx
    emb = cam.Embedder(cache=None)
    emo = common.load("tools/voices/emotions.py").Emotions(cache=None)
    kana = Kana() if any(VOICES[n].get("lang") == "ja" for n in names) else None
    for name in names:
        cfg = VOICES[name]
        m = build(name, cfg, a, cam, emb, emo, kana)
        if m is None:
            continue
        mpath = os.path.join(a.root, "voice-clips", "espeech", cfg["set"], "manifest.json")
        manifest = json.load(open(mpath, encoding="utf-8")) if os.path.exists(mpath) else {}
        manifest[name] = m
        for vn, vc in cfg.get("variants", {}).items():
            manifest[vn] = variant(vn, vc, cfg, name, a)
            manifest[vn]["base"] = name
        json.dump(manifest, open(mpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
