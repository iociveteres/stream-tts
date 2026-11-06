"""Синтез Supertonic 3 на onnxruntime (CPU): перенос helper.py официального репозитория supertone-inc/supertonic
(py/helper.py, коммит 1e9799e, MIT — THIRD-PARTY.txt) — подготовка текста и индексы символов (UnicodeProcessor),
нарезка на куски (chunk_text), duration_predictor → text_encoder → vector_estimator (total_step шагов) → vocoder.

Отличия от helper.py:
- шум латента — из своего генератора np.random (seed воспроизводит фразу целиком), а не из глобального;
- синтез — генератор по кускам текста: поток отдаёт кусок, как только он готов; stop (threading.Event) проверяется
  перед каждым шагом vector_estimator и куском — остановка за один шаг (десятки мс);
- символ, которого нет в unicode_indexer.json (индекс -1), выбрасывается: helper.py передал бы -1 в модель;
- звук обрезан по длительности из duration_predictor (как в example_onnx.py) и пересчитан из 44,1 в 24 кГц.

Папка модели — раскладка репозитория HF Supertone/supertonic-3: onnx/{duration_predictor,text_encoder,
vector_estimator,vocoder}.onnx, onnx/tts.json, onnx/unicode_indexer.json (voice_styles/ — пресеты голосов)."""
import json, math, os, re, threading, unicodedata

import numpy as np

SR = 24000   # stream-tts и протокол cosyvoice-server — 24 кГц
ONNX = ("duration_predictor", "text_encoder", "vector_estimator", "vocoder")
FILES = tuple(f"onnx/{n}.onnx" for n in ONNX) + ("onnx/tts.json", "onnx/unicode_indexer.json")
LANGS = ("en", "ko", "ja", "ar", "bg", "cs", "da", "de", "el", "es", "et", "fi", "fr", "hi", "hr", "hu", "id", "it",
         "lt", "lv", "nl", "pl", "pt", "ro", "ru", "sk", "sl", "sv", "tr", "uk", "vi", "na")
GAP_SEC = 0.3   # пауза между кусками — как silence_duration в helper.py


class Stopped(Exception):
    """Синтез остановлен (POST /tts/stop или клиент отключился)."""


def missing(models):
    """Каких файлов модели нет в папке (пустой список — всё на месте)."""
    return [f for f in FILES if not os.path.isfile(os.path.join(models, f))]


# --------------------------------------------------------------------------------------- текст (helper.py)
_EMOJI = re.compile("[\U0001f600-\U0001f64f\U0001f300-\U0001f5ff\U0001f680-\U0001f6ff\U0001f700-\U0001f77f"
                    "\U0001f780-\U0001f7ff\U0001f800-\U0001f8ff\U0001f900-\U0001f9ff\U0001fa00-\U0001fa6f"
                    "\U0001fa70-\U0001faff☀-⛿✀-➿\U0001f1e6-\U0001f1ff]+")
_REPL = {"–": "-", "‑": "-", "—": "-", "_": " ", "“": '"', "”": '"', "‘": "'", "’": "'",
         "´": "'", "`": "'", "[": " ", "]": " ", "|": " ", "/": " ", "#": " ", "→": " ", "←": " "}
_EXPR = {"@": " at ", "e.g.,": "for example, ", "i.e.,": "that is, "}
_END = re.compile(r"[.!?;:,'\"')\]}…。」』】〉》›»]$")
# границы предложений chunk_text: после .!? и пробела, кроме английских сокращений
_SENT = re.compile(r"(?<!Mr\.)(?<!Mrs\.)(?<!Ms\.)(?<!Dr\.)(?<!Prof\.)(?<!Sr\.)(?<!Jr\.)(?<!Ph\.D\.)(?<!etc\.)"
                   r"(?<!e\.g\.)(?<!i\.e\.)(?<!vs\.)(?<!Inc\.)(?<!Ltd\.)(?<!Co\.)(?<!Corp\.)(?<!St\.)(?<!Ave\.)"
                   r"(?<!Blvd\.)(?<!\b[A-Z]\.)(?<=[.!?])\s+")


def preprocess(text, lang):
    """UnicodeProcessor._preprocess_text: NFKD (й, ё — буква и надстрочный знак, их модель знает), знаки,
    пробелы у пунктуации, точка в конце, теги языка."""
    text = _EMOJI.sub("", unicodedata.normalize("NFKD", text))
    for k, v in _REPL.items():
        text = text.replace(k, v)
    text = re.sub(r"[♥☆♡©\\]", "", text)
    for k, v in _EXPR.items():
        text = text.replace(k, v)
    text = re.sub(r" ([,.!?;:'])", r"\1", text)
    text = re.sub(r'"{2,}', '"', re.sub(r"'{2,}", "'", text))
    text = re.sub(r"\s+", " ", text).strip()
    if not _END.search(text):
        text += "."
    return f"<{lang}>{text}</{lang}>"


def chunk_text(text, max_len=300):
    """Куски до max_len символов: по абзацам, внутри — по предложениям (длинное предложение — одним куском)."""
    chunks = []
    for par in (p.strip() for p in re.split(r"\n\s*\n+", text.strip())):
        if not par:
            continue
        cur = ""
        for sent in _SENT.split(par):
            if len(cur) + len(sent) + 1 <= max_len:
                cur += (" " if cur else "") + sent
            else:
                if cur:
                    chunks.append(cur.strip())
                cur = sent
        if cur:
            chunks.append(cur.strip())
    return chunks


# --------------------------------------------------------------------------------------- звук
def resample(x, sr_from, sr_to):
    """Пересчёт частоты через БПФ: спектр обрезается на новой частоте Найквиста со спадом косинусом в верхних 5 %
    (без него — звон Гиббса). Линейная интерполяция (resample в stream-tts/streamtts/audio.py) при 44,1 → 24 кГц
    заворачивает 12–22 кГц обратно в слышимую полосу. Длина БПФ кратна sr_from/НОД·64 — новая длина целая, размер
    раскладывается на малые множители (быстро); запас нулей в конце — чтобы конец куска не заворачивался в начало."""
    n = len(x)
    if sr_from == sr_to or not n:
        return np.asarray(x, np.float32)
    g = math.gcd(sr_from, sr_to)
    a, b = sr_from // g, sr_to // g
    size = -(-(n + 1024) // (a * 64)) * a * 64
    out = size // a * b
    spec = np.fft.rfft(x, size)
    keep = min(len(spec), out // 2 + 1)
    spec = spec[:keep].copy()
    taper = max(1, keep // 20)
    spec[-taper:] *= 0.5 + 0.5 * np.cos(np.linspace(0, np.pi, taper))
    y = np.fft.irfft(spec, out) * (out / size)
    return y[:round(n * b / a)].astype(np.float32)


# --------------------------------------------------------------------------------------- модель
class Style:
    """Голос — стиль из voice_styles/*.json: style_ttl [1, 50, 256] и style_dp [1, 8, 16]."""

    def __init__(self, d):
        def arr(key):
            v = d[key]
            a = np.asarray(v["data"], np.float32).reshape(v["dims"])
            if a.ndim != 3 or a.shape[0] != 1:
                raise ValueError(f"{key}: форма {list(a.shape)}, нужен один стиль [1, …]")
            return a
        self.ttl, self.dp = arr("style_ttl"), arr("style_dp")


class Model:
    def __init__(self, models, threads=0):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        # без арены: после фразы в 300 символов 470 МБ рабочего набора и 850 выделено вместо 600 и 1100 (арена держит
        # пик самой длинной фразы), синтез медленнее на ~6 %
        opts.enable_cpu_mem_arena = False
        # между запусками сессий (шаги, куски) потоки не крутятся вхолостую: синтез идёт рядом с OBS и игрой
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        d = os.path.join(models, "onnx")
        self.dp, self.enc, self.est, self.voc = (
            ort.InferenceSession(os.path.join(d, n + ".onnx"), sess_options=opts, providers=["CPUExecutionProvider"])
            for n in ONNX)
        with open(os.path.join(d, "tts.json"), encoding="utf-8") as f:
            cfg = json.load(f)
        with open(os.path.join(d, "unicode_indexer.json"), encoding="utf-8") as f:
            self.indexer = np.asarray(json.load(f), np.int64)
        self.sr = cfg["ae"]["sample_rate"]
        self.chunk = cfg["ae"]["base_chunk_size"] * cfg["ttl"]["chunk_compress_factor"]   # сэмплов на кадр латента
        self.ldim = cfg["ttl"]["latent_dim"] * cfg["ttl"]["chunk_compress_factor"]
        self.version = cfg.get("tts_version", "")

    def ids(self, text):
        codes = np.frombuffer(text.encode("utf-16-le"), np.uint16)   # helper.py: ord() в uint16
        idx = self.indexer[codes]
        idx = idx[idx >= 0]
        return idx[None, :], np.ones((1, 1, len(idx)), np.float32)

    def infer(self, text, lang, style, steps, speed, rng, stop):
        """Один кусок → float32 с частотой модели (44,1 кГц)."""
        ids, mask = self.ids(preprocess(text, lang))
        dur = self.dp.run(None, {"text_ids": ids, "style_dp": style.dp, "text_mask": mask})[0] / speed
        emb = self.enc.run(None, {"text_ids": ids, "style_ttl": style.ttl, "text_mask": mask})[0]
        n_wav = int(dur[0] * self.sr)
        n_lat = max(1, -(-n_wav // self.chunk))
        xt = rng.standard_normal((1, self.ldim, n_lat), dtype=np.float32)
        lat_mask = np.ones((1, 1, n_lat), np.float32)
        total = np.array([steps], np.float32)
        for step in range(steps):
            if stop.is_set():
                raise Stopped
            xt = self.est.run(None, {"noisy_latent": xt, "text_emb": emb, "style_ttl": style.ttl,
                                     "text_mask": mask, "latent_mask": lat_mask,
                                     "current_step": np.array([step], np.float32), "total_step": total})[0]
        if stop.is_set():
            raise Stopped
        wav = self.voc.run(None, {"latent": xt})[0]
        return wav[0, :n_wav]

    def synth(self, text, lang, style, steps=8, speed=1.05, seed=None, stop=None, first_sentence=False):
        """Генератор кусков float32 24 кГц; перед каждым куском, кроме первого, — пауза GAP_SEC. Stopped — стоп.
        first_sentence — первое предложение отдельным куском (поток): первый звук фразы в 85 символов из двух
        предложений — через ~0,5 с вместо ~1,6 (8 шагов), а пока оно играет, остальное успевает с запасом."""
        stop = stop or threading.Event()
        rng = np.random.default_rng(seed)
        max_len = 120 if lang in ("ko", "ja") else 300
        chunks = chunk_text(text, max_len)
        if first_sentence and chunks:
            head, *rest = _SENT.split(chunks[0], maxsplit=1)
            if rest:
                chunks = [head] + chunk_text(" ".join([rest[0]] + chunks[1:]), max_len)
        for i, chunk in enumerate(chunks):
            wav = resample(self.infer(chunk, lang, style, steps, speed, rng, stop), self.sr, SR)
            if i:
                wav = np.concatenate([np.zeros(int(GAP_SEC * SR), np.float32), wav])
            yield wav
