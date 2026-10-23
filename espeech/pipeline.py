"""Синтез фразы по кускам: нормализатор → предложения → нарезка «по бюджету» → RUAccent на кусок → ESpeech.

ESpeech не умеет стриминг: фраза синтезируется целиком, а каждый кусок стоит ~0,8 с + 0,13 × секунд звука (образец
6–7 с проходит через DiT в каждом куске). Поэтому фраза режется так, чтобы первый кусок был коротким (первый звук
~1 с), а каждый следующий успевал синтезироваться, пока звучат готовые (samples/espeech-sentences):
- первый кусок — от 1,5 с звука (длинное первое предложение режется по , ; : — до 4 с), следующие — от 2,5 с и
  растут, пока их синтез укладывается в запас доигрывающего звука; не длиннее 12 с звука (дальше модель теряет конец);
- секунды звука оцениваются по байтам UTF-8 и темпу голоса;
- длительность куска: как у f5-tts (кадров на байт, короче 10 байт — медленнее в 3,3 раза) + 0,3 с за каждый конец
  предложения внутри куска; короткий кусок (< 2 с) — не меньше 1,4 с + 0,5 с за каждый знак , : ; — (иначе модель
  обрывает хвост после знака), длинный — не меньше 1 с + 0,15 с за знак (паузы на знаках);
- подряд идущие предложения из 1–2 слов склеиваются через запятую («Пять! Четыре! Пуск!» → «Пять, четыре, пуск!»):
  цепочки коротких предложений модель глотает;
- тишина по краям каждого куска обрезается (модель ставит 0,3–0,5 с в начало), между кусками — 0,4 с;
- один seed на все куски фразы."""
import re

import numpy as np

from . import model as M

PUNCT_IN = re.compile(r"[,:;—]")
SENT_END = re.compile(r"[.!?]+(?=\s)")   # конец предложения внутри куска (за ним ещё текст)
PAUSE = 0.4                               # тишина между кусками, с (на слух 0,3–0,45)
MAX_SEC = 12.0                            # предел куска, с звука
FIRST_SEC, NEXT_SEC, FIRST_MAX_SEC = 1.5, 2.5, 4.0
COST_C, COST_K = 1.0, 0.15                # цена куска c + k × звук, с — с запасом (замер: 0,63–0,8 + 0,10–0,13)


# ------------------------------------------------------------------------------------------- текст
def split_sentences(text):
    """По концу предложения (. ! ? и их сочетания, многоточие); знак остаётся у предложения."""
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def join_chains(sents):
    """Подряд идущие предложения из 1–2 слов — одно предложение через запятую, знак — у последнего."""
    out, run = [], []

    def flush():
        if len(run) >= 2:
            words = [re.sub(r"[.!?…]+$", "", s) for s in run[:-1]] + [run[-1]]
            out.append(", ".join([words[0]] + [w[:1].lower() + w[1:] for w in words[1:]]))
        else:
            out.extend(run)
        run.clear()

    for s in sents:
        if len(s.split()) <= 2:
            run.append(s)
        else:
            flush()
            out.append(s)
    flush()
    return out


def split_long(sent, max_bytes):
    """Предложение длиннее max_bytes — по , ; : — жадно до max_bytes (как chunk_text f5-tts)."""
    if len(sent.encode()) <= max_bytes:
        return [sent]
    parts = [p for p in re.split(r"(?<=[,;:—])\s+", sent) if p]
    out, cur = [], ""
    for p in parts:
        if cur and len((cur + " " + p).encode()) > max_bytes:
            out.append(cur)
            cur = p
        else:
            cur = (cur + " " + p).strip()
    if cur:
        out.append(cur)
    return out


def plan(sents, bps, first_sec=FIRST_SEC, min_sec=NEXT_SEC, max_bytes=10 ** 9, c=COST_C, k=COST_K, pause=PAUSE,
         first_max_sec=FIRST_MAX_SEC):
    """Нарезка по запасу воспроизведения. bps — байт текста на секунду звука. Первый кусок — от first_sec; дальше
    запас B — сколько готового звука ещё не доиграло; следующий кусок — от min_sec и растёт, пока его синтез
    c + k × L укладывается в B. → куски (строки)."""
    est = lambda s: max(1.0, len(s.encode()) / bps)
    if sents and first_max_sec and est(sents[0]) > first_max_sec:
        sents = split_long(sents[0], int(first_max_sec * bps)) + list(sents[1:])
    out, cur, B = [], "", None
    for s in sents:
        if not cur:
            cur = s
            continue
        nxt = cur + " " + s
        if len(nxt.encode()) > max_bytes:
            fits = False
        elif B is None:
            fits = est(cur) < first_sec
        else:
            fits = est(cur) < min_sec or c + k * est(nxt) <= B
        if fits:
            cur = nxt
        else:
            L = est(cur)
            B = L + pause if B is None else max(0.0, B - (c + k * L)) + L + pause
            out.append(cur)
            cur = s
    if cur:
        out.append(cur)
    return out


def chunks(text, bps):
    """Нормализованный текст → куски для синтеза (без ударений)."""
    max_bytes = int(MAX_SEC * bps)
    sents = [p for s in join_chains(split_sentences(text)) for p in split_long(s, max_bytes)]
    return plan(sents, bps, max_bytes=max_bytes)


def frames(text, rate):
    """Кадров мела на кусок text (с ударениями) при темпе rate (кадров на байт)."""
    nb = len(text.encode())
    base = rate * nb / (0.3 if nb < 10 else 1.0)
    n_punct = len(PUNCT_IN.findall(text.rstrip(",:;— ")))
    if base < 2.0 * M.FPS:
        f = max(base, (1.4 + 0.5 * n_punct) * M.FPS)
    else:
        f = max(base, 1.0 * M.FPS) + 0.15 * n_punct * M.FPS
    return int(f + 0.3 * M.FPS * len(SENT_END.findall(text.strip())))


# ------------------------------------------------------------------------------------------- звук
def edges(y, thr_db=-40.0, win=0.02):
    """(начало, конец) звука в отсчётах: окна по 20 мс громче максимума − 40 дБ."""
    h = int(M.SR * win)
    n = len(y) // h
    if n == 0:
        return 0, len(y)
    db = 20 * np.log10(np.sqrt(np.mean(y[:n * h].reshape(n, h) ** 2, 1)) + 1e-9)
    loud = np.nonzero(db > db.max() + thr_db)[0]
    if not len(loud):
        return 0, len(y)
    return loud[0] * h, min(len(y), (loud[-1] + 1) * h)


def trim(y, keep_head=0.02, keep_tail=0.05, fade=0.01):
    a, b = edges(y)
    y = y[max(0, a - int(keep_head * M.SR)):min(len(y), b + int(keep_tail * M.SR))].copy()
    n = min(len(y) // 2, int(fade * M.SR))
    if n:
        r = np.linspace(0, 1, n, dtype=np.float32)
        y[:n] *= r
        y[-n:] *= r[::-1]
    return y
