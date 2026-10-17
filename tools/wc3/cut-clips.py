"""Нарезает кандидатов в образцы голоса из звуков Warcraft III для отслушивания.

usage: .venv/Scripts/python.exe tools/wc3/cut-clips.py <batch> [voice_id ...] [--kinds SDUCA]
       .venv/Scripts/python.exe tools/wc3/cut-clips.py --pools [voice_id ...]
  <batch>     имя партии, например b03: результат в voice-clips/wc3/<batch>/
  voice_id    только эти голоса (по умолчанию все из HEROES и UNITS)
  --kinds     какие виды кандидатов резать (по умолчанию все), например CA — только варианты по подаче
  --pools     только обновить pools.json (реплики для sim в tools/voices/synth.py), без нарезки
Дальше: python tools/voices/pick.py --add-batch wc3 <batch>, процесс целиком — docs/voice-pipeline.md.

Виды кандидатов (обычные — по итогам A/B пилота b01):
  S00      текущий образец из voice-refs/wc3, для сравнения
  D01 D02  герои: реплики из диалогов кампаний, 10-15 с, из соседних строк одной миссии
           (одна сцена, одна подача), две разные миссии. Ответы героя как юнита звучат
           хуже диалогов, а клипы 20-30 с выигрыша не дают.
  U01      юниты: короткие ответы всех типов (нейтральные, боевые, шутки) подряд, 15-25 с.
           Герою без диалогов режется так же.
  C01      спокойная подача, A01 — подача с напором (ставится отдельным голосом <голос>-angry).
           По энергии реплик из tools/voices/emotions.py: у героя — окно диалога с ровной подачей,
           у юнита — самые спокойные или самые напористые ответы. Берутся, только если энергия
           отличается от D/U хотя бы на MOOD_DIFF, A — ещё и с энергией от MOOD_LOUD («напор»).

Текст реплики, по убыванию доверия (всё в voice-clips/wc3/):
  text-fixes.json        проверенные на слух правки {путь к исходнику: текст}
  subtitles.json         субтитры из карт кампаний (extract-subtitles.py), только диалоги
  text-fixes-draft.json  черновые правки ASR, не проверенные на слух
  asr.json               кеш Whisper
Whisper прогоняется и там, где есть субтитры: если субтитр не похож на звук, реплика
отбрасывается (в ruRU часть реплик осталась английской). Ответы юнитов сверяются с
wikiquote.json (ru.wikiquote.org/wiki/Warcraft_III:_Reign_of_Chaos). При расхождении клип
получает пометку «сверить текст» со вторым вариантом.

Метки фраз из review.py (phrase-tags.json): фразы с «не брать» в нарезку не попадают.

Нужен faster-whisper (GPU: ещё nvidia-cublas-cu12 и nvidia-cudnn-cu12) и ffmpeg в PATH, для C и A — torch и
transformers (venv проекта).
"""
import datetime, difflib, glob, importlib.util, json, os, random, re, shutil, site, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # tools/<группа>/ -> корень проекта
ORIGS = os.path.join(ROOT, "voice-origs", "Warcraft III (RU)")
REFS = os.path.join(ROOT, "voice-refs", "wc3")
CLIPS = os.path.join(ROOT, "voice-clips", "wc3")

DIALOGUE = (10.0, 15.0)  # длина клипа из диалогов, с
UNIT = (15.0, 25.0)      # длина клипа из ответов юнита, с
GAP = 0.25            # пауза между склеенными репликами, с
SCENE_GAP = 10        # реплики героя дальше этого по номеру строки считаются другой сценой
N_DLG = 2             # кандидатов из диалогов на героя
POOL = 60             # реплик на «голос в целом» (pools.json): центр уже устойчив, а считать быстрее
MOOD_DIFF = 0.08      # вариант по подаче (C, A) нужен, если энергия отличается от D/U хотя бы на столько
MOOD_SPREAD = 0.2     # окно диалога для C/A — с ровной подачей: разброс энергии реплик не больше
MOOD_LOUD = 0.78      # A ставится как <голос>-angry, поэтому нужен настоящий напор (порог LOUD в emotions.py)

# голос -> (имя в файлах диалогов, регулярка по пути диалога или None, папки ответов в units/, название)
# Папка ответов: "папка[:регулярка по имени файла[:метка для тега]]".
# Если в RoC и TFT персонажа озвучивали разные актёры (видно по эмбеддингам CAM++), это разные голоса:
# разделяются регуляркой по пути (кампании TFT лежат в *expcamp).
HEROES = {
    "wc3-arthas":      ("arthas", "humancampaign", ["human/arthas"], "Артас (паладин)"),
    "wc3-arthas-dk":   ("arthas", "undead", ["undead/evilarthas"], "Артас (рыцарь смерти)"),
    "wc3-jaina":       ("jaina", r"^(?!.*expcamp)", ["human/jaina"], "Джайна"),
    "wc3-jaina-tft":   ("jaina", "expcamp", [], "Джайна (TFT, другая актриса)"),
    "wc3-uther":       ("uther", None, ["human/uther"], "Утер"),
    "wc3-muradin":     ("muradin", None, ["human/muradin"], "Мурадин"),
    "wc3-antonidas":   ("antonidas", None, [], "Антонидас"),
    "wc3-medivh":      ("medivh", None, [], "Медив"),
    "wc3-thrall":      ("thrall", None, ["orc/thrall"], "Тралл"),
    "wc3-grom":        ("grom", None, ["orc/hellscream"], "Гром"),
    "wc3-cairne":      ("cairne", None, ["orc/cairne"], "Кэрн"),
    "wc3-rexxar":      ("rexxar", None, [], "Рексар"),
    "wc3-voljin":      ("voljin", None, [], "Вол'джин"),
    "wc3-grunt":       ("grunt", None, ["orc/grunt"], "Орк-пехотинец"),
    # Иллидан RoC (nightelf05-06 и ответы юнита) и TFT звучат по-разному (сходство центров 0,58).
    # В форме демона (illidanmorphed*) голос обработан эффектом и не узнаётся: не берём.
    "wc3-illidan":     ("illidan", "expcamp", [], "Иллидан (TFT)"),
    "wc3-illidan-roc": ("illidan", r"^(?!.*expcamp)", ["nightelf/illidan:^illidan(?!morphed)"], "Иллидан (RoC)"),
    "wc3-tyrande":     ("tyrande", None, ["nightelf/tyrande"], "Тиранда"),
    "wc3-furion":      ("furion", None, ["nightelf/furion"], "Фурион"),
    "wc3-maiev":       ("maiev", None, ["nightelf/maiev"], "Майев"),
    "wc3-cenarius":    ("cenarius", None, [], "Кенарий"),
    "wc3-kael":        ("kael", None, ["human/kael"], "Кель'тас"),
    "wc3-garithos":    ("garithos", None, ["creeps/lordgarithos"], "Гаритос"),
    "wc3-ladyvashj":   ("ladyvashj", None, ["naga/ladyvashj"], "Леди Вайш"),
    # Сильвана-банши в RoC (undead03-05) звучит иначе, чем Тёмная Охотница в TFT (0,66-0,71)
    "wc3-sylvanas":    ("sylvanas", "expcamp", ["undead/evilsylvanas"], "Сильвана (TFT)"),
    "wc3-sylvanas-roc": ("sylvanas", r"^(?!.*expcamp)", ["creeps/sylvanuswindrunner"], "Сильвана (RoC, банши)"),
    "wc3-kelthuzad":   ("kelthuzad", None, ["undead/kelthuzadlich"], "Кел'Тузад"),
    "wc3-anubarak":    ("anubarak", None, [], "Ануб'арак"),
    "wc3-varimathras": ("varimathras", None, ["undead/varimathras"], "Вариматрас"),
    "wc3-tichondrius": ("tichondrius", None, ["undead/tichondrius"], "Тихондрий"),
    "wc3-malganis":    ("malganis", None, [], "Мал'Ганис"),
    "wc3-archimonde":  ("archimonde", None, [], "Архимонд"),
    "wc3-mannoroth":   ("mannoroth", None, [], "Маннорот"),
    "wc3-narrator":    ("narrator", None, [], "Рассказчик"),
}
# голос -> (папка в units/, регулярка по имени файла, название)
UNITS = {
    "wc3-peasant":      ("human/peasant", r"", "Крестьянин"),
    "wc3-footman":      ("human/footman", r"", "Пехотинец"),
    "wc3-priest":       ("human/priest", r"", "Жрец"),
    "wc3-sorceress":    ("human/sorceress", r"", "Волшебница"),
    "wc3-rifleman":     ("human/rifleman", r"", "Стрелок"),
    "wc3-archmage":     ("human/heroarchmage", r"", "Архимаг"),
    "wc3-mountainking": ("human/heromountainking", r"", "Король гор"),
    "wc3-paladin":      ("human/heropaladin", r"", "Паладин"),
    "wc3-bloodmage":    ("human/herobloodelf", r"", "Кровавый маг"),
    "wc3-peon":         ("orc/peon", r"", "Батрак"),
    "wc3-headhunter":   ("orc/headhunter", r"", "Охотник за головами"),
    "wc3-witchdoctor":  ("orc/witchdoctor", r"", "Знахарь"),
    "wc3-acolyte":      ("undead/acolyte", r"", "Послушник"),
    "wc3-ghoul":        ("undead/ghoul", r"", "Вурдалак"),
    "wc3-abomination":  ("undead/abomination", r"", "Поганище"),
    "wc3-necromancer":  ("undead/necromancer", r"", "Некромант"),
    "wc3-deathknight":  ("undead/herodeathknight", r"", "Рыцарь смерти"),
    "wc3-lich":         ("undead/herolich", r"", "Лич"),
    "wc3-dreadlord":    ("undead/herodreadlord", r"", "Повелитель ужаса"),
    "wc3-archer":       ("nightelf/archer", r"", "Лучница"),
    "wc3-huntress":     ("nightelf/huntress", r"", "Охотница"),
    "wc3-dryad":        ("nightelf/dryad", r"", "Дриада"),
    "wc3-druidbear":    ("nightelf/druidoftheclaw", r"^druidoftheclaw(?!morph)", "Друид-медведь"),
    "wc3-goblinsapper": ("creeps/goblinsapper", r"", "Гоблин-сапёр"),
    "wc3-zeppelin":     ("creeps/goblinzeppelin", r"", "Гоблинский цеппелин"),
}
# Не речь: крики смерти, звуки атаки, эффекты, превращения.
SKIP = re.compile(r"death\d*$|alternatedeath|(?<!yes)attack\d|effort|cannibalize|blowsup|loop|morph\d|morphalternate")
# Тип ответа юнита -> группа кандидата
CATEGORY = [("yesattack", "combat"), ("warcry", "combat"), ("pissed", "pissed"),
            ("what", "neutral"), ("yes", "neutral"), ("ready", "neutral")]
GROUP_ORDER = ["neutral", "combat", "pissed"]
# Типичные галлюцинации Whisper на рычании и тишине (титры из обучающих субтитров).
HALLUCINATION = re.compile(r"субтитр|редактор|корректор|dimatorzok|продолжение следует|"
                           r"спасибо за просмотр|подпис\w+ на канал|музыка", re.I)
# Рычание и вой («Ммммм», «У-у-у»): текстом звук не описать, для образца такие реплики вредны.
VOCALIZATION = re.compile(r"([а-яё])(?:-?\1){2,}", re.I)
# Подсказка Whisper: имена в написании русской локализации 2002 года (по субтитрам кампаний),
# так их и произносят актёры: «Артес», «Тиренда», «Малганус», а не «Артас», «Тиранда», «Мал'Ганис».
ASR_PROMPT = ("Артес, Утер, Джайна Праудмур, Тралл, Гром Задира, Кэрн, Рексар, Волджин, Мурадин, "
              "Антонидас, Медив, Иллидан, Тиренда, Фарион, Мэв, Кенариус, Кель-Тас, Гаритос, леди Вайш, "
              "Сильвана, Кел-Тузед, Анубарак, Вариматрас, Тикондрус, Малганус, Архимонд, Маннорох, "
              "Магеридон, Нер-Зул, Гул-Дан. Лордерон, Даларан, Калимдор, Нортренд, Терамор, Даротар. "
              "Орда, Альянс, Плеть, Пылающий Легион, Ледяной Трон, Фростморн.")
# Субтитр берём вместо ASR, если тексты похожи хотя бы на SUB_MIN; ниже — это другая запись
# (в ruRU-локали часть реплик осталась с английской озвучкой). До SUB_CHECK — пометка «сверить».
SUB_MIN, SUB_CHECK = 0.5, 0.85
# Ответы юнитов сверяются с Викицитатником (страница шумная, поэтому только для пометки):
# от QUOTE_MIN это та же реплика, до QUOTE_SAME тексты расходятся.
QUOTE_MIN, QUOTE_SAME = 0.75, 0.97


def load_json(path, default):
    return json.load(open(path, encoding="utf-8")) if os.path.exists(path) else default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def duration(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True, check=True)
    return float(r.stdout)


class Pieces:
    """Исходные реплики: обрезка тишины, длительность, расшифровка (с кешем)."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.asr = load_json(os.path.join(CLIPS, "asr.json"), {})
        self.fixes = load_json(os.path.join(CLIPS, "text-fixes.json"), {})
        self.drafts = load_json(os.path.join(CLIPS, "text-fixes-draft.json"), {})
        self.subs = load_json(os.path.join(CLIPS, "subtitles.json"), {})
        self.quotes = load_json(os.path.join(CLIPS, "wikiquote.json"), [])
        self.tags = load_json(os.path.join(CLIPS, "phrase-tags.json"), {})  # метки из review.py
        self.model = None

    def wav(self, rel):
        dst = os.path.join(self.tmp, rel.replace("/", "_") + ".wav")
        if not os.path.exists(dst):
            ffmpeg("-i", os.path.join(ORIGS, rel), "-af",
                   "silenceremove=start_periods=1:start_threshold=-50dB,areverse,"
                   "silenceremove=start_periods=1:start_threshold=-50dB,areverse",
                   "-ac", "1", "-ar", "24000", dst)
        return dst

    def info(self, rel):
        if rel not in self.asr:
            wav = self.wav(rel)
            segs = list(self.whisper().transcribe(wav, language="ru", beam_size=5,
                                                  condition_on_previous_text=False,
                                                  initial_prompt=ASR_PROMPT)[0])
            self.asr[rel] = dict(
                dur=round(duration(wav), 3),
                text=re.sub(r"[«»\"]", "", " ".join(s.text.strip() for s in segs)).strip(),
                lp=round(sum(s.avg_logprob for s in segs) / len(segs), 3) if segs else -9,
                nsp=round(max((s.no_speech_prob for s in segs), default=1), 3),
                cr=round(max((s.compression_ratio for s in segs), default=0), 2))
        e = dict(self.asr[rel])
        good_asr = e["lp"] > -0.6 and e["nsp"] < 0.6 and e["cr"] < 2.4 and not HALLUCINATION.search(e["text"])
        e["alt"] = None  # второй вариант текста, если источники расходятся: «сверить текст»
        if rel in self.fixes:
            e["text"], e["src"], ok = self.fixes[rel], "fix", True
        elif rel in self.subs:
            sim = similarity(e["text"], self.subs[rel])
            asr_text = e["text"]
            e["text"], e["src"], e["sim"] = clean_sub(self.subs[rel]), "sub", round(sim, 2)
            ok = sim >= SUB_MIN
            if sim < SUB_CHECK:
                e["alt"] = "ASR: " + asr_text
        else:
            if rel in self.drafts:
                e["text"], e["src"], ok = self.drafts[rel], "draft", True
            else:
                e["src"], ok = "asr", good_asr
            quote, sim = self.quote(e["text"])
            if QUOTE_MIN <= sim < QUOTE_SAME:
                e["alt"] = "Викицитатник: " + quote
        e["check"] = e["alt"] is not None
        e["ok"] = (ok and len(re.findall(r"[а-яё]", e["text"].lower())) >= 3
                   and not VOCALIZATION.search(e["text"]) and not self.tags.get(rel, {}).get("bad"))
        return e

    def quote(self, text):
        """Ближайшая цитата из Викицитатника и её похожесть (по буквам, без пунктуации)."""
        a = letters(text)
        if len(a) < 3 or not self.quotes:
            return None, 0.0
        best = max(self.quotes, key=lambda q: difflib.SequenceMatcher(None, a, letters(q)).ratio())
        if letters(best) in self.known(a):
            return best, 1.0  # цитата — это другая, уже расшифрованная реплика, а не расхождение
        return best, difflib.SequenceMatcher(None, a, letters(best)).ratio()

    def known(self, own):
        if not hasattr(self, "_known"):
            texts = [e["text"] for e in self.asr.values()] + list(self.drafts.values())
            self._known = {letters(t) for t in texts}
        return self._known - {own}

    def whisper(self):
        if self.model is None:
            for d in glob.glob(os.path.join(site.getsitepackages()[-1], "nvidia", "*", "bin")):
                os.add_dll_directory(d)
                os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
            from faster_whisper import WhisperModel
            try:
                self.model = WhisperModel("large-v3", device="cuda", compute_type="float16")
            except Exception as e:
                print("CUDA недоступна, Whisper на CPU:", e, file=sys.stderr)
                self.model = WhisperModel("large-v3", device="cpu", compute_type="int8")
        return self.model

    def save(self):
        save_json(os.path.join(CLIPS, "asr.json"), self.asr)


def words(text):
    return re.sub(r"[^а-я ]", " ", text.lower().replace("ё", "е")).split()


def letters(text):
    return re.sub(r"[^а-я]", "", text.lower().replace("ё", "е"))


def similarity(a, b):
    return difflib.SequenceMatcher(None, words(a), words(b)).ratio()


def clean_sub(text):
    text = re.sub(r"\|c[0-9a-fA-F]{8}|\|r", "", text)
    text = re.sub(r"''|[«»\"]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def total(items):
    return sum(p["dur"] for p in items) + GAP * (len(items) - 1)


def dialogue_lines(name, folder):
    """Реплики героя в диалогах кампаний: {миссия: [(номер строки, rel), ...]}."""
    out = {}
    base = os.path.join(ORIGS, "sound", "dialogue")
    for path in glob.glob(os.path.join(base, "*", "*", "*.mp3")):
        rel = os.path.relpath(path, ORIGS).replace("\\", "/")
        if folder and not re.search(folder, rel):
            continue
        stem = os.path.splitext(os.path.basename(path))[0]
        # h01arthas08, a05illidan49, l01arthas22 (префикс a/b у некоторых миссий)
        m = re.match(r"^[a-z]\d{2}([a-z]+?)(\d+)[a-z]?$", stem)
        if m and m.group(1) in (name, "a" + name, "b" + name):
            out.setdefault(rel.split("/")[-2], []).append((int(m.group(2)), rel))
    return {k: sorted(v) for k, v in out.items()}


def dialogue_candidates(pieces, name, folder):
    """Окна из соседних реплик одной сцены (D), лучшие — из разных миссий."""
    windows = dialogue_windows(pieces, name, folder)
    chosen, used = [], set()
    ranked = sorted(windows, key=lambda w: -w["score"])
    for one_per_mission in (True, False):
        for w in ranked:
            rels = {p["rel"] for p in w["items"]}
            if len(chosen) >= N_DLG or rels & used or w in chosen:
                continue
            if one_per_mission and any(c["tag"] == w["tag"] for c in chosen):
                continue
            chosen.append(w)
            used |= rels
    return [dict(kind="D", **w) for w in chosen]


def dialogue_windows(pieces, name, folder):
    """Все окна DIALOGUE с из соседних годных реплик одной сцены, с оценкой разборчивости."""
    lo, hi = DIALOGUE
    windows = []
    for mission, lines in dialogue_lines(name, folder).items():
        items = [dict(rel=rel, num=num, **pieces.info(rel)) for num, rel in lines]
        for i in range(len(items)):
            run = []
            for j in range(i, len(items)):
                p = items[j]
                if not p["ok"] or (run and p["num"] - run[-1]["num"] > SCENE_GAP):
                    break
                run.append(p)
                if total(run) >= lo:
                    break
            if run and lo <= total(run) <= hi:
                score = (sum(p["lp"] for p in run) / len(run) - 0.05 * (len(run) - 1)
                         - 0.01 * abs(total(run) - (lo + hi) / 2))
                windows.append(dict(score=score, items=run, tag="dlg:" + mission))
    return windows


def category(stem):
    for key, group in CATEGORY:
        if key in stem:
            return group
    return None


def unit_files(folder, pattern=""):
    """Файлы ответов юнита (rel) без криков смерти и эффектов."""
    out = []
    for path in sorted(glob.glob(os.path.join(ORIGS, "units", folder, "*"))):
        stem = os.path.splitext(os.path.basename(path))[0]
        if SKIP.search(stem) or category(stem) is None or (pattern and not re.search(pattern, stem)):
            continue
        out.append(os.path.relpath(path, ORIGS).replace("\\", "/"))
    return out


def voice_pool(pieces, vid):
    """Все годные реплики голоса (rel): из них режутся кандидаты, из них же pools.json для sim."""
    if vid in HEROES:
        name, folder, unit_dirs, _ = HEROES[vid]
        rels = [rel for lines in dialogue_lines(name, folder).values() for _, rel in lines]
        for d in unit_dirs:
            rels += unit_files(*d.split(":", 2)[:2])
    else:
        rels = unit_files(*UNITS[vid][:2])
    return [rel for rel in rels if pieces.info(rel)["ok"]]


def unit_candidates(pieces, folder, pattern="", label=None):
    """Ответы юнита (U): короткие реплики всех типов в одном клипе. Типы по очереди, внутри
    типа — от самых разборчивых, затем клип упорядочен по типу и файлу."""
    groups = {g: [] for g in GROUP_ORDER}
    for rel in unit_files(folder, pattern):
        group = category(os.path.basename(rel))
        p = dict(rel=rel, **pieces.info(rel))
        # одна и та же реплика иногда лежит в нескольких файлах (pissed6 = pissed1)
        key = re.sub(r"[^а-яё]", "", p["text"].lower())
        if p["ok"] and not any(re.sub(r"[^а-яё]", "", q["text"].lower()) == key for q in groups[group]):
            groups[group].append(p)
    queues = [sorted(groups[g], key=lambda p: -p["lp"]) for g in GROUP_ORDER]
    run = []
    while any(queues) and total(run) < UNIT[0]:
        for q in queues:
            if q:
                p = q.pop(0)
                if total(run + [p]) <= UNIT[1]:
                    run.append(p)
    if total(run) < 4:
        return []
    run.sort(key=lambda p: (GROUP_ORDER.index(category(os.path.basename(p["rel"]))), p["rel"]))
    return [dict(kind="U", items=run, tag="unit:" + (label or folder.split("/")[-1]))]


def write_pools(pieces, voices):
    """pools.json: реплики «голоса в целом» для sim в tools/voices/synth.py, пути от корня проекта.
    Та же случайная выборка POOL реплик, что считал прежний synth-ab.py, поэтому оценки партий сравнимы."""
    path = os.path.join(CLIPS, "pools.json")
    pools = load_json(path, {})
    origs = os.path.relpath(ORIGS, ROOT).replace("\\", "/")
    for vid in voices:
        pool = voice_pool(pieces, vid)
        pools[vid] = [f"{origs}/{rel}" for rel in random.Random(vid).sample(pool, min(POOL, len(pool)))]
    save_json(path, pools)


def mood(emo, items):
    """Средняя энергия реплик (emotions.py) и её разброс."""
    a = [emo(os.path.join(ORIGS, p["rel"]))["a"] for p in items]
    return sum(a) / len(a), max(a) - min(a)


def mood_candidates(pieces, vid, regular, emo, kinds):
    """Варианты по подаче: C — самая спокойная, A — с самым сильным напором (ставится как <голос>-angry).
    Герой — окно диалога одной сцены с ровной подачей, юнит — его реплики по энергии. Вариант берётся, только
    если энергия отличается от обычных кандидатов (D, U) хотя бы на MOOD_DIFF: иначе он их повторяет;
    A — ещё и только с энергией от MOOD_LOUD."""
    base = [mood(emo, c["items"])[0] for c in regular]
    base = sum(base) / len(base) if base else None
    taken = [{p["rel"] for p in c["items"]} for c in regular]
    sign = {"C": -1, "A": 1}
    out = []
    windows = dialogue_windows(pieces, *HEROES[vid][:2]) if vid in HEROES else []
    if windows:
        for w in windows:
            w["a"], w["spread"] = mood(emo, w["items"])
        windows = [w for w in windows if {p["rel"] for p in w["items"]} not in taken]
        smooth = [w for w in windows if w["spread"] <= MOOD_SPREAD] or windows
        for kind in kinds:
            if smooth:
                w = max(smooth, key=lambda w: sign[kind] * w["a"])
                out.append(dict(kind=kind, items=w["items"], tag=w["tag"], a=w["a"]))
    else:
        dirs = [d.split(":", 2)[:2] for d in HEROES[vid][2]] if vid in HEROES else [UNITS[vid][:2]]
        lines, seen = [], set()
        for d in dirs:
            for rel in unit_files(*d):
                p = dict(rel=rel, **pieces.info(rel))
                key = re.sub(r"[^а-яё]", "", p["text"].lower())  # pissed6 = pissed1
                if p["ok"] and key not in seen:
                    seen.add(key)
                    p["a"] = emo(os.path.join(ORIGS, rel))["a"]
                    lines.append(p)
        for kind in kinds:
            run = []
            for p in sorted(lines, key=lambda p: sign[kind] * -p["a"]):
                if total(run + [p]) <= UNIT[1]:
                    run.append(p)
                if total(run) >= UNIT[0]:
                    break
            if total(run) >= 6:
                run.sort(key=lambda p: p["rel"])
                out.append(dict(kind=kind, items=run, tag="unit:" + dirs[0][0].split("/")[-1],
                                a=sum(p["a"] for p in run) / len(run)))
    return [c for c in out if (base is None or sign[c["kind"]] * (c["a"] - base) >= MOOD_DIFF)
            and (c["kind"] != "A" or c["a"] >= MOOD_LOUD)]


def sentence(text):
    text = text.strip()
    return text if re.search(r"[.!?…]$", text) else text + "."


def clip_text(parts):
    return " ".join(sentence(p["text"]) for p in parts)


def write_text(path, parts):
    with open(path, "w", encoding="utf-8") as f:
        f.write(clip_text(parts) + "\n")


def write_clip(pieces, items, dst):
    inputs, filt = [], []
    for i, p in enumerate(items):
        inputs += ["-i", pieces.wav(p["rel"])]
        pad = "anull" if i == len(items) - 1 else f"apad=pad_dur={GAP}"
        filt.append(f"[{i}:a]{pad}[a{i}]")
    filt.append("".join(f"[a{i}]" for i in range(len(items))) + f"concat=n={len(items)}:v=0:a=1[out]")
    ffmpeg(*inputs, "-filter_complex", ";".join(filt), "-map", "[out]",
           "-ac", "1", "-ar", "24000", dst + ".wav")
    parts = [dict(rel=p["rel"], text=p["text"], src=p["src"], alt=p["alt"]) for p in items]
    write_text(dst + ".txt", parts)
    return parts


def emotions():
    """Оценщик подачи из tools/voices/emotions.py (нужны torch и transformers)."""
    spec = importlib.util.spec_from_file_location("emotions", os.path.join(ROOT, "tools", "voices", "emotions.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Emotions()


def main():
    args = sys.argv[1:]
    kinds = list("SDUCA")
    if "--kinds" in args:
        i = args.index("--kinds")
        kinds = args[i + 1].replace(",", "").upper()
        del args[i:i + 2]
    pools_only = "--pools" in args
    if pools_only:
        args.remove("--pools")
    batch, only = (None, args) if pools_only else (args[0], args[1:])
    voices = [v for v in list(HEROES) + list(UNITS) if not only or v in only]
    unknown = set(only) - set(HEROES) - set(UNITS)
    if unknown:
        sys.exit(f"Неизвестные голоса: {' '.join(sorted(unknown))}")
    if pools_only:
        tmp = tempfile.mkdtemp()
        pieces = Pieces(tmp)
        try:
            write_pools(pieces, voices)
        finally:
            pieces.save()
            shutil.rmtree(tmp, ignore_errors=True)
        return print(f"pools.json: {len(voices)} голосов")
    mood_kinds = [k for k in "CA" if k in kinds]
    emo = emotions() if mood_kinds else None

    out = os.path.join(CLIPS, batch)
    os.makedirs(out, exist_ok=True)
    # Номера кандидатов зависят от параметров нарезки: голос с оценками не перенарезается,
    # новая версия нарезки — это новая партия.
    rated = {cid.rsplit(".", 1)[0] for cid, v in parse_ratings(out).items() if any(v)}
    for vid in [v for v in voices if v in rated]:
        print(f"!! {vid}: в {batch}/ratings.txt уже есть оценки, пропускаю (нарежьте новую партию)")
        voices.remove(vid)
    tmp = tempfile.mkdtemp()
    pieces = Pieces(tmp)
    rows = load_json(os.path.join(out, "clips.json"), {})
    try:
        for vid in voices:
            vdir = os.path.join(out, vid)
            shutil.rmtree(vdir, ignore_errors=True)
            os.makedirs(vdir)
            rows = {k: v for k, v in rows.items() if v["voice"] != vid}
            if vid in HEROES:
                name, folder, unit_dirs, title = HEROES[vid]
                found = dialogue_candidates(pieces, name, folder)
                if not found:
                    for d in unit_dirs:
                        found += unit_candidates(pieces, *d.split(":", 2))
            else:
                folder, pattern, title = UNITS[vid]
                found = unit_candidates(pieces, folder, pattern)
            if mood_kinds:
                found += mood_candidates(pieces, vid, found, emo, mood_kinds)
                emo.save()
            found = [c for c in found if c["kind"] in kinds]

            ref = os.path.join(REFS, vid)
            if "S" in kinds and os.path.exists(ref + ".wav"):
                cid = f"{vid}.S00"
                rel = f"voice-refs/wc3/{vid}.wav"
                text = pieces.fixes.get(rel) or open(ref + ".txt", encoding="utf-8").read().strip()
                parts = [dict(rel=rel, text=text, src="fix" if rel in pieces.fixes else "ref", alt=None)]
                shutil.copy(ref + ".wav", os.path.join(vdir, cid + ".wav"))
                write_text(os.path.join(vdir, cid + ".txt"), parts)
                rows[cid] = dict(voice=vid, title=title, kind="S", tag="current",
                                 dur=round(duration(ref + ".wav"), 2), lp=None, parts=parts)
            numbers = {}
            for c in found:
                numbers[c["kind"]] = numbers.get(c["kind"], 0) + 1
                cid = f"{vid}.{c['kind']}{numbers[c['kind']]:02d}"
                parts = write_clip(pieces, c["items"], os.path.join(vdir, cid))
                rows[cid] = dict(voice=vid, title=title, kind=c["kind"], tag=c["tag"],
                                 dur=round(total(c["items"]), 2),
                                 lp=round(sum(p["lp"] for p in c["items"]) / len(c["items"]), 2),
                                 parts=parts)
            n = sum(1 for r in rows.values() if r["voice"] == vid)
            print(f"== {vid}: {n} кандидатов" + "".join(f", {c['kind']} энергия {c['a']:.2f}"
                                                       for c in found if "a" in c), flush=True)
            pieces.save()
        write_pools(pieces, voices)
    finally:
        pieces.save()
        shutil.rmtree(tmp, ignore_errors=True)

    rows = dict(sorted(rows.items()))
    save_json(os.path.join(out, "clips.json"), rows)
    save_json(os.path.join(out, "batch.json"), dict(
        created=datetime.date.today().isoformat(), dialogue=DIALOGUE, unit=UNIT, gap=GAP,
        scene_gap=SCENE_GAP, dialogue_per_voice=N_DLG, kinds="".join(kinds),
        mood_diff=MOOD_DIFF, mood_spread=MOOD_SPREAD, mood_loud=MOOD_LOUD))
    write_ratings(out, rows)
    print(f"Готово: {len(rows)} кандидатов в {out}")


RATINGS_HEADER = """\
# Оценки кандидатов. Формат строки: id | оценка | эмоция | комментарий
# оценка: 1-5 (5 = беру как есть, 3 = годится, 1 = брак), пусто = не слушал
# эмоция: свободно (спокойно, приказ, гнев, усталость, шутка, крик...)
# комментарий: шум, эффекты, обрезанное слово и т.п. Ошибки в тексте правятся в review.py.
# Строки с # пересоздаются скриптами, свои пометки пишите в комментарий.
"""


def parse_ratings(out):
    """{id: (оценка, эмоция, комментарий)} из ratings.txt партии."""
    path = os.path.join(out, "ratings.txt")
    values = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            if line.startswith("#") or "|" not in line:
                continue
            cid, *rest = [p.strip() for p in line.rstrip("\n").split("|")]
            values[cid] = tuple((rest + ["", "", ""])[:3])
    return values


def write_ratings(out, rows, values=None):
    """Пересобирает ratings.txt по clips.json, сохраняя проставленные значения."""
    if values is None:
        values = parse_ratings(out)
    lines, voice = [RATINGS_HEADER], None
    for cid, r in rows.items():
        if r["voice"] != voice:
            voice = r["voice"]
            lines.append(f"\n# ===== {voice} — {r['title']} =====\n")
        alts = [p["alt"] for p in r["parts"] if p["alt"]]
        warn = " ⚠ сверить текст" if alts else ""
        lines.append(f"#   [{r['tag']}, {r['dur']:.1f} с{warn}] {clip_text(r['parts'])}\n")
        lines += [f"#     ⚠ {a}\n" for a in alts]
        score, emotion, comment = values.get(cid, ("", "", ""))
        lines.append(f"{cid} | {score} | {emotion} | {comment}".rstrip() + "\n")
    tmp = os.path.join(out, "ratings.txt.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp, os.path.join(out, "ratings.txt"))


if __name__ == "__main__":
    main()
