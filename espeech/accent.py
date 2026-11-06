"""Ударения RUAccent 1.5.8.3 в полном режиме (омографы turbo3.1, словарь словоформ) с той же разметкой, что у исходного
пакета, но ~0,3 ГБ памяти вместо 2,4 (samples/ruaccent-memory). Код разметки — классы пакета ruaccent как есть
(process_all и модели); своё — только загрузка:
- RuleEngine (лемматизатор и POS-теггер koziev, ~1 ГБ) не грузится: process_all его не вызывает;
- словарь словоформ (3,2 млн форм, 762 МБ в dict) — SQLite на диске (accents.sqlite, tools/espeech/prepare.py);
- свои токенизаторы вместо transformers (бит в бит): символьный для nn_accent, остальные — библиотека tokenizers;
  transformers и huggingface_hub на время импорта ruaccent подменяются пустыми модулями (берутся только имена);
- ONNX — веса во внешнем файле с границы 64 КБ: onnxruntime отображает его в память (файловые страницы в uss не
  входят); allow_spinning=0 — потоки onnxruntime не крутятся вхолостую (разметка в 2,8 раза быстрее рядом с синтезом).
RUAccent.load не вызывать: он лезет в сеть (HfFileSystem) и грузит RuleEngine.

Папка (tools/espeech/prepare.py): dictionary/{omographs,yo_words,yo_homographs}.json.gz, nn/<модель>/ (токенизаторы,
config.json), onnx/<модель>.onnx + .onnx.data, accents.sqlite.

Поверх разметки — правило «вс+ём»: «Внимание всем!» RUAccent размечает «вс+ём», а «всём» (предложный падеж «всё»)
бывает только после предлога. Последним — словарь ударений пользователя (UserAccents, файл --user-accents сервера):
его слова перекрывают и словарь, и омографы, и это правило."""
import gzip, json, os, re, sqlite3, sys, types

import numpy as np


# ------------------------------------------------------------------------------------------- токенизаторы
class CharTok:
    """CharTokenizer nn_accent: символы → id по vocab.txt, [bos] … [eos], do_lower_case."""

    def __init__(self, path):
        cfg = json.load(open(os.path.join(path, "tokenizer_config.json"), encoding="utf-8"))
        with open(os.path.join(path, "vocab.txt"), encoding="utf-8") as f:
            self.vocab = {t.rstrip("\n"): i for i, t in enumerate(f.readlines())}
        self.lower = cfg.get("do_lower_case", False)
        self.unk = self.vocab[cfg["unk_token"]]
        self.bos, self.eos = self.vocab[cfg["bos_token"]], self.vocab[cfg["eos_token"]]
        # как PreTrainedTokenizer: спецтокен в самом тексте («[unk]») — один id, а не символы
        special = [cfg[k] for k in ("pad_token", "unk_token", "bos_token", "eos_token")]
        self.split = re.compile("(" + "|".join(map(re.escape, special)) + ")")

    def __call__(self, text, return_tensors="np"):
        if self.lower:
            text = text.lower()
        ids = [self.bos]
        for i, part in enumerate(self.split.split(text)):
            ids += [self.vocab[part]] if i % 2 else [self.vocab.get(c, self.unk) for c in part]
        ids.append(self.eos)
        return {"input_ids": np.array([ids], dtype=np.int64), "attention_mask": np.ones((1, len(ids)), dtype=np.int64)}


class FastTok:
    """Остальные токенизаторы (tokenizer.json) в объёме, который вызывает ruaccent: строка с offsets и
    special_tokens_mask, пара строк и пакет пар с усечением и добивкой."""
    # какие входы отдаёт transformers 5 (model_input_names класса из tokenizer_config.json)
    INPUTS = {"BertTokenizer": ["input_ids", "token_type_ids", "attention_mask"],
              "DistilBertTokenizer": ["input_ids", "attention_mask"],
              "RobertaTokenizer": ["input_ids", "attention_mask"]}

    def __init__(self, path):
        from tokenizers import Tokenizer
        cfg = json.load(open(os.path.join(path, "tokenizer_config.json"), encoding="utf-8"))
        self.names = self.INPUTS[cfg["tokenizer_class"]]
        self._tokenizer = Tokenizer.from_file(os.path.join(path, "tokenizer.json"))
        self._tokenizer.no_truncation()
        self._tokenizer.no_padding()
        self.unk_token_id = self._tokenizer.token_to_id(cfg["unk_token"])
        self.pad_token, self.pad_id = cfg["pad_token"], self._tokenizer.token_to_id(cfg["pad_token"])

    def _arrays(self, encs, offsets=False, special=False):
        out = {"input_ids": np.array([e.ids for e in encs], dtype=np.int64)}
        if "token_type_ids" in self.names:
            out["token_type_ids"] = np.array([e.type_ids for e in encs], dtype=np.int64)
        out["attention_mask"] = np.array([e.attention_mask for e in encs], dtype=np.int64)
        if special:
            out["special_tokens_mask"] = np.array([e.special_tokens_mask for e in encs], dtype=np.int64)
        if offsets:
            out["offset_mapping"] = np.array([e.offsets for e in encs], dtype=np.int64)
        return out

    def __call__(self, text, pair=None, return_offsets_mapping=False, return_special_tokens_mask=False,
                 return_tensors="np", padding=False, truncation=False, max_length=None):
        t = self._tokenizer
        if truncation:
            t.enable_truncation(max_length, strategy="longest_first")
        if padding:
            t.enable_padding(pad_id=self.pad_id, pad_token=self.pad_token)
        try:
            if isinstance(text, list):
                encs = t.encode_batch(list(zip(text, pair)) if pair is not None else text)
            else:
                encs = [t.encode(text, pair) if pair is not None else t.encode(text)]
        finally:
            t.no_truncation()
            t.no_padding()
        return self._arrays(encs, return_offsets_mapping, return_special_tokens_mask)

    def convert_ids_to_tokens(self, i):
        return self._tokenizer.id_to_token(i)

    def convert_tokens_to_string(self, tokens):
        return self._tokenizer.decoder.decode(tokens)


# ------------------------------------------------------------------------------------------- словарь
class SqliteAccents:
    """dict.get поверх таблицы a(w PRIMARY KEY, s) и небольшого словаря поверх (буквы, custom_dict), а над ним —
    словарь пользователя (UserAccents)."""

    def __init__(self, path, overlay=(), user=None):
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        self.overlay = dict(overlay)
        self.user = user.words if user is not None else {}

    def get(self, key, default=None):
        if key in self.user:
            return self.user[key]
        if key in self.overlay:
            return self.overlay[key]
        row = self.db.execute("SELECT s FROM a WHERE w=?", (key,)).fetchone()
        return row[0] if row else default

    def update(self, d):
        self.overlay.update(d)

    def __contains__(self, word):
        """Есть ли форма в словаре — для транслита латиницы в normalize."""
        return self.get(word.lower()) is not None


# ------------------------------------------------------------------------------------------- словарь пользователя
VOWELS = "аеёиоуыэюя"
ENTRY = re.compile(r"[а-яё+]+(?:-[а-яё+]+)*")
TOKEN = re.compile(r"(?<![\w+])[А-Яа-яЁё+]+(?:-[А-Яа-яЁё+]+)*")
MAX_WORD = 64


def _mark(src, val):
    """Ударения из val на буквы слова src: регистр и «ё» — как в тексте («ЗАМ+ОК», «з+амок» → «З+АМОК»)."""
    letters, out, i = src.replace("+", ""), [], 0
    for c in val:
        out.append("+" if c == "+" else letters[i])
        i += c != "+"
    return "".join(out)


# Ударение, которое уже стоит в тексте: «+» перед гласной внутри слова или в его начале («з+амок», «+оба»).
# «+25», «18+» и «C++» — не ударения.
STRESS = re.compile(r"(?<![\dA-Za-z_])\+(?=[аеёиоуыэюяАЕЁИОУЫЭЮЯ])")


def unmark(text):
    """Текст без ударений: перед normalize, иначе «з+амок» станет «з плюс амок»."""
    return STRESS.sub("", text)


def keep_marked(src, accented):
    """Слова, где в src ударение уже стояло, — с ним, а не с разметкой модели (accented): повторная разметка не
    трогает поправленное вручную. Остальные слова — как в accented. Регистр — из accented (normalize его меняет)."""
    marked = {}
    for m in TOKEN.finditer(src):
        w = m.group(0)
        if STRESS.search(w):
            marked[w.replace("+", "").lower()] = w.lower()

    def sub(m):
        w = m.group(0)
        v = marked.get(w.replace("+", "").lower())
        return _mark(w, v) if v else w
    return TOKEN.sub(sub, accented) if marked else accented


class UserAccents:
    """Свои ударения пользователя поверх RUAccent: слово (строчными, без «+») → его разметка («з+амок»).

    Применяются к готовой разметке и перекрывают всё — словарь, омографы (turbo3.1 их решает до словаря, поэтому
    одной подмены в SqliteAccents мало) и правило «вс+ём». Слово ищется как есть, потом с «ё» → «е»: «ежик» из
    словаря подходит и к «ёжик», который поставил RUAccent; «вс+ё» — только там, где RUAccent сам прочитал «всё».
    Хранится в JSON-файле path (None — только в памяти); вызывать под одной блокировкой с разметкой."""

    def __init__(self, path=None):
        self.path = path
        self.words = {}
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            for v in (d.get("words") or {}).values():
                try:
                    k, v = self.parse(v)
                except ValueError:
                    continue
                self.words[k] = v

    @staticmethod
    def parse(accented):
        """«З+амок» → («замок», «з+амок»); ValueError — не одно русское слово с ударением перед гласной."""
        v = str(accented).strip().lower()
        if not v or len(v) > MAX_WORD or not ENTRY.fullmatch(v):
            raise ValueError("одно слово русскими буквами, ударение — «+» перед гласной")
        if "+" not in v or "++" in v or any(v[i + 1:i + 2] not in tuple(VOWELS) for i, c in enumerate(v) if c == "+"):
            raise ValueError("ударение — «+» перед гласной")
        return v.replace("+", ""), v

    def items(self):
        return sorted(self.words.items())

    def put(self, accented):
        k, v = self.parse(accented)
        self.words[k] = v
        self._save()
        return k, v

    def delete(self, word):
        if self.words.pop(str(word).strip().lower().replace("+", ""), None) is None:
            return False
        self._save()
        return True

    def _save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(version=1, words=dict(self.items())), f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def apply(self, text):
        if not self.words:
            return text

        def sub(m):
            w = m.group(0)
            plain = w.replace("+", "").lower()
            v = self.words.get(plain) or self.words.get(plain.replace("ё", "е"))
            return _mark(w, v) if v else w
        return TOKEN.sub(sub, text)


# ------------------------------------------------------------------------------------------- «вс+ём»
VSEM = re.compile(r"(?:(\S+)(\s+))?(?<![\w+])([вВ][сС]\+?)([ёЁ])([мМ])\b")
PREP = {"о", "об", "обо", "при", "в", "во", "на"}


def _vsem(m):
    if m.group(1) and m.group(1).replace("+", "").lower() in PREP:
        return m.group(0)
    return (m.group(1) or "") + (m.group(2) or "") + m.group(3) + ("е" if m.group(4) == "ё" else "Е") + m.group(5)


# ------------------------------------------------------------------------------------------- загрузка
def _import_ruaccent():
    """ruaccent берёт из transformers и huggingface_hub только имена на верхнем уровне (AutoTokenizer, база
    CharTokenizer, HfFileSystem, hf_hub_download), а нужны они лишь в RUAccent.load."""
    saved = {k: sys.modules.get(k) for k in ("transformers", "huggingface_hub")}
    tr = types.ModuleType("transformers")
    tr.AutoTokenizer = None
    tr.PreTrainedTokenizer = object
    hub = types.ModuleType("huggingface_hub")
    hub.HfFileSystem = lambda: None
    hub.hf_hub_download = None
    sys.modules.update(transformers=tr, huggingface_hub=hub)
    try:
        import ruaccent.ruaccent as rm
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return rm


class Accentor:
    def __init__(self, workdir, threads=None, user=None):
        import onnxruntime as ort
        rm = _import_ruaccent()

        # transformers 5 не отдаёт token_type_ids из CharTokenizer, а ONNX-модели nn_accent их требуют — нули
        class Session(ort.InferenceSession):
            def run(self, names, feed, *a, **kw):
                if "token_type_ids" not in feed and "input_ids" in feed and "token_type_ids" in self.input_names:
                    feed = dict(feed, token_type_ids=np.zeros_like(feed["input_ids"]))
                return super().run(names, feed, *a, **kw)

        def session(name):
            so = ort.SessionOptions()
            if threads:
                so.intra_op_num_threads = threads
            so.add_session_config_entry("session.intra_op.allow_spinning", "0")
            s = Session(os.path.join(workdir, "onnx", name + ".onnx"), so, providers=["CPUExecutionProvider"])
            s.input_names = {i.name for i in s.get_inputs()}
            return s

        nn = lambda sub: os.path.join(workdir, "nn", sub)
        jload = lambda name: json.load(gzip.open(os.path.join(workdir, "dictionary", name)))
        labels = lambda sub: json.load(open(os.path.join(nn(sub), "config.json"), encoding="utf-8"))["id2label"]

        acc = rm.RUAccent()   # пустые обёртки моделей; дальше — то же, что RUAccent.load, без сети и RuleEngine
        acc.workdir, acc.tiny_mode, acc.custom_dict = workdir, False, {}
        acc.omographs = jload("omographs.json.gz")
        acc.omographs.update({"коса": ["к+оса", "кос+а"]})
        om = acc.omograph_model
        om.session = session("turbo3.1")
        om.tokenizer = FastTok(nn("nn_omograph/turbo3.1"))
        acc.yo_words = jload("yo_words.json.gz")
        am = acc.accent_model
        am.session = session("nn_accent")
        am.id2label = labels("nn_accent")
        am.tokenizer = CharTok(nn("nn_accent"))
        acc.yo_homographs = jload("yo_homographs.json.gz")
        ym = acc.yo_homograph_model
        ym.session = session("nn_yo_homograph_resolver")
        ym.id2label = labels("nn_yo_homograph_resolver")
        ym.tokenizer = FastTok(nn("nn_yo_homograph_resolver"))
        self.user = user if user is not None else UserAccents()
        acc.accents = SqliteAccents(os.path.join(workdir, "accents.sqlite"), user=self.user)
        acc.accents.update(acc.custom_dict)
        acc.accents.update(acc.letters_accent)
        sp = acc.stress_usage_predictor
        sp.session = session("nn_stress_usage_predictor")
        sp.id2label = labels("nn_stress_usage_predictor")
        sp.tokenizer = FastTok(nn("nn_stress_usage_predictor"))
        self.acc = acc
        self.lexicon = acc.accents

    def __call__(self, text):
        """Текст (после normalize) → с ударениями «+» перед гласной; слова пользователя — последними."""
        return self.user.apply(VSEM.sub(_vsem, self.acc.process_all(text))) if text.strip() else ""
