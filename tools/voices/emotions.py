"""Эмоции в речи: энергия (arousal), напор (dominance) и тон (valence) по записи.

usage: .venv/Scripts/python.exe tools/voices/emotions.py <файл|папка> ...       таблица по файлам
       .venv/Scripts/python.exe tools/voices/emotions.py --lines <папка> ...   поля arousal/valence в lines.json

Зачем. CosyVoice в zero-shot копирует не только тембр, но и подачу образца: из спокойного образца выходит
спокойный синтез, из крика — крик, а образец, где спокойная реплика соседствует с криком, даёт синтез, который
скачет или усредняет. Поэтому подачу нужно знать у каждой реплики и у каждого образца:
  - собирать однородные образцы и варианты по подаче (cut-clips.py: C — спокойный, A — с напором);
  - видеть на странице выбора, ровная ли подача внутри образца (разброс по окнам 3 с);
  - проверять, что синтез держит подачу образца.
Раньше эмоции размечались только вручную в review.py (phrase-tags.json), а реплик тысячи.

Модель: audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim (MSP-Podcast, три оси 0…1, лицензия
CC BY-NC-SA 4.0: только для некоммерческого использования, здесь она лишь размечает образцы). Выбрана по
50 фразам WC3 с ручной разметкой «спокойно» против «гнев/приказ/крик/угроза» (AUC): arousal 0,96,
dominance 0,98, Aniemore/wavlm-emotion-russian-resd 0,93, emotion2vec_plus_large 0,85. Категориальные
модели путают спокойную игровую речь с «отвращением» или «злостью», а ось энергии разделяет почти без ошибок.
Тон (valence) по голосу определяется хуже, его стоит считать подсказкой. Та разметка однобокая (одна игра,
только крайности энергии). Сверка на 156 фразах из пяти источников (tools/voices/emotion-eval.py) выбор
подтвердила: по энергии ρ 0,65 против 0,51 и 0,48 у категориальных моделей; тон не слышит ни одна модель.

Кеш: voice-clips/emotions.json {путь от корня: {stamp, a, d, v, win}}; win — энергия по окнам 3 с (шаг 1,5 с).
Модель (1,2 ГБ) скачивается с Hugging Face при первом запуске. Нужны torch, transformers, ffmpeg в PATH.
"""
import json, os, subprocess, sys, time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # tools/<группа>/ -> корень проекта
CACHE = os.path.join(ROOT, "voice-clips", "emotions.json")
MODEL = "audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim"
SR = 16000
WIN, HOP = 3.0, 1.5
# Пороги по ручной разметке WC3: спокойные реплики 0,35–0,76 (медиана 0,62), крик и приказ 0,69–0,99 (0,85).
CALM, LOUD = 0.6, 0.78
AUDIO = (".wav", ".mp3", ".ogg", ".flac", ".m4a")


def label(a, v=None):
    """Слово для подачи: спокойно, ровно, напор, злость (напор с плохим тоном)."""
    if a is None:
        return ""
    if a < CALM:
        return "спокойно"
    if a < LOUD:
        return "ровно"
    return "злость" if v is not None and v < 0.4 else "напор"


def spread(win):
    """Разброс энергии внутри записи: разница 90-го и 10-го процентиля по окнам."""
    return round(float(np.percentile(win, 90) - np.percentile(win, 10)), 3) if win and len(win) > 1 else 0.0


def pcm(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def save_cache(path, data):
    """Запись через временный файл. На Windows свежий файл иногда держит антивирус или индексатор
    (WinError 32 на os.replace), поэтому замена повторяется."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    for attempt in range(20):
        try:
            return os.replace(tmp, path)
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.25)


def stamp(path):
    st = os.stat(path)
    return f"{st.st_size}-{int(st.st_mtime)}"


def key_of(path):
    return os.path.relpath(os.path.abspath(path), ROOT).replace("\\", "/")


class Emotions:
    """emo(путь) -> {a, d, v, win}; модель грузится при первом промахе кеша."""

    def __init__(self, cache=CACHE):
        self.path = cache
        self.cache = json.load(open(cache, encoding="utf-8")) if cache and os.path.exists(cache) else {}
        self.dirty = False
        self.model = None

    def load(self):
        import torch, torch.nn as nn
        from transformers import Wav2Vec2FeatureExtractor
        from transformers.models.wav2vec2.modeling_wav2vec2 import Wav2Vec2Model, Wav2Vec2PreTrainedModel

        class Head(nn.Module):
            def __init__(self, config):
                super().__init__()
                self.dense = nn.Linear(config.hidden_size, config.hidden_size)
                self.dropout = nn.Dropout(config.final_dropout)
                self.out_proj = nn.Linear(config.hidden_size, config.num_labels)

            def forward(self, x):
                return self.out_proj(self.dropout(torch.tanh(self.dense(self.dropout(x)))))

        class Model(Wav2Vec2PreTrainedModel):  # как в карточке модели: среднее скрытых состояний -> 3 оси
            def __init__(self, config):
                super().__init__(config)
                self.wav2vec2 = Wav2Vec2Model(config)
                self.classifier = Head(config)
                self.post_init()

            def forward(self, x):
                return self.classifier(self.wav2vec2(x)[0].mean(dim=1))

        self.torch = torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.extract = Wav2Vec2FeatureExtractor.from_pretrained(MODEL)
        self.model = Model.from_pretrained(MODEL).to(self.device).eval()
        if self.device == "cuda":
            self.model.half()  # 0,6 ГБ видеопамяти вместо 1,2: рядом работает cosyvoice-server

    def run(self, x):
        """[arousal, dominance, valence] для куска pcm 16 кГц. Модель выдаёт порядок a, d, v."""
        if self.model is None:
            self.load()
        y = self.extract(x, sampling_rate=SR)["input_values"][0]
        t = self.torch.from_numpy(np.asarray(y)[None]).to(self.device)
        with self.torch.no_grad():
            out = self.model(t.half() if self.device == "cuda" else t)[0].float().cpu().numpy()
        return [round(float(z), 3) for z in out]

    def __call__(self, path):
        key, st = key_of(path), stamp(path)
        hit = self.cache.get(key)
        if not hit or hit["stamp"] != st:
            x = pcm(path)
            a, d, v = self.run(x[:30 * SR])  # длиннее 30 с образцов не бывает, а память не бесконечна
            win, n = [], int(WIN * SR)
            for start in range(0, max(len(x) - n, 0) + 1, int(HOP * SR)):
                win.append(self.run(x[start:start + n])[0])
            self.dirty = True
            hit = self.cache[key] = dict(stamp=st, a=a, d=d, v=v, win=win)
        return hit

    def save(self):
        if self.path and self.dirty:
            save_cache(self.path, self.cache)
            self.dirty = False


def files(args):
    for a in args:
        if os.path.isdir(a):
            for root, _, names in os.walk(a):
                yield from (os.path.join(root, n) for n in sorted(names) if n.lower().endswith(AUDIO))
        else:
            yield a


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    emo = Emotions()
    try:
        if args[0] == "--lines":  # как score-speaker.py: поля в lines.json, по ним build-ref.py фильтрует
            for d in args[1:]:
                path = os.path.join(d, "lines.json")
                lines = json.load(open(path, encoding="utf-8"))
                for ln in lines:
                    e = emo(os.path.join(d, ln["file"]))
                    ln.update(arousal=e["a"], valence=e["v"])
                    print(f"{ln['file']:12s} {e['a']:.2f} {e['v']:.2f} {label(e['a'], e['v']):9s} {ln['text'][:70]}")
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(lines, f, ensure_ascii=False, indent=1)
            return
        print(f"{'энерг':>5s} {'напор':>5s} {'тон':>5s} {'разбр':>5s}  подача     файл")
        for p in files(args):
            e = emo(p)
            print(f"{e['a']:5.2f} {e['d']:5.2f} {e['v']:5.2f} {spread(e['win']):5.2f}  {label(e['a'], e['v']):9s}  {key_of(p)}")
    finally:
        emo.save()


if __name__ == "__main__":
    main()
