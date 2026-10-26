"""Реплики голосов Warcraft III для LoRA ESpeech и образец 6–7 с: вход tools/espeech/train.py.

usage: .venv/Scripts/python.exe tools/wc3/lora-clips.py [голос …] [--out voice-clips/wc3-lora] [--root <корень>]
                                                         [--ref <голос>=<rel>[,<rel>…]] [--rate <от>,<до>]
                                                         [--candidates N]
  голос   wc3-* из HEROES и UNITS cut-clips.py или вариант из voice-refs/wc3/manifest.json (wc3-thrall-angry);
          вариант собирается вместе со своим персонажем. По умолчанию — все голоса манифеста.
  --root  корень проекта с voice-origs/, voice-clips/, voice-refs/ (по умолчанию этот; из worktree — основная копия).
  --ref   образец вручную: реплики (пути от «voice-origs/Warcraft III (RU)/») подряд с паузой 0,25 с.
  --rate  темп образца, букв/с (по умолчанию 10,15): «9,10.5» — подобрать образец медленнее.
  --candidates N  только подобрать образец: N лучших без общих реплик в <out>/<голос>/ref-cand/NN.wav + .txt,
          реплики, образец и manifest.json не трогаются. Выбранный кусок — в --ref или в голос (адаптер не меняется).

Реплики — те же, что режет tools/wc3/cut-clips.py (voice_pool: диалоги кампаний и ответы юнита, без «не брать» из
review.py, без рычания и галлюцинаций Whisper), текст по убыванию доверия: text-fixes.json → субтитры кампаний →
text-fixes-draft.json → ASR (кеш asr.json; нет в кеше — распознаётся faster-whisper и дописывается). Не берутся
реплики с пометкой «сверить текст», длиннее 30 с (предел train.py) и повторы одного текста (pissed6 = pissed1).
Тишина по краям обрезана (−50 дБ, как в cut-clips), 24 кГц моно PCM16; текст ровно как звучит, без «+».
У реплик отрезается хвост после речи — рычание зверя, эхо (Охотница: пантера после половины ответов): громкий
звук позже конца последнего слова по faster-whisper (word_timestamps, кеш <out>/words.json); и для обучения, и для
образца.

Варианты одного персонажа (<голос>-angry и т. п. в voice-refs/wc3/manifest.json) делят один адаптер, у них свой
только образец. Образец — 5,5–7,5 с (если таких нет — 4,5–9 с) одной репликой или 2–4 соседними (одна сцена диалога
или один тип ответа юнита), темп 10–15 русских букв в секунду (медленный образец даёт лишние слова в конце,
docs/espeech.md), текст сверен с ASR (разница букв ≤ 10 %). Основному голосу — спокойный (энергия
tools/voices/emotions.py < 0,6, без «!», если такие есть), варианту -angry — с напором (от 0,78), сначала из реплик его образца
CosyVoice; у героя сначала диалоги, у медленных голосов — самый быстрый из спокойных. Реплики образцов всех
вариантов персонажа в обучение не входят, у вариантов они разные.

Выход <out>/<голос>/: lora/<имя>.wav + .txt, ref.wav + ref.txt; у варианта — только ref.wav + ref.txt.
<out>/manifest.json — {голос: {title, clips, minutes, steps, src, skipped, tails, ref, variants}} (tails — {реплика:
секунд отрезано}), у варианта
{title, base, ref}; steps — шаги LoRA по правилу пользователя (от 4 мин — 3000, ~2 мин — 2000, ~1 мин — 1000,
меньше 1 мин — 500).
Нужны faster-whisper (реплики нет в asr.json или words.json), torch и transformers (энергия; кеш voice-clips/emotions.json),
ffmpeg в PATH.
"""
import argparse, difflib, importlib.util, json, os, re, shutil, sys, tempfile, wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # tools/<группа>/ -> корень проекта

MAX_DUR = 30.0          # train.py пропускает реплики длиннее
REF_DUR = (5.5, 7.5)    # образец ESpeech: 6–7 с (docs/espeech.md, «Образец голоса»)
REF_RATE = (10.0, 15.0)  # букв в секунду
REF_MAX_PARTS = 4
REF_CER = 0.10          # текст образца против ASR
CALM, LOUD = 0.6, 0.78  # пороги энергии emotions.py
GAP = 0.25              # пауза между репликами склеенного образца, как в cut-clips
# Хвост после речи: у Охотницы после половины ответов рычит пантера (0,3–0,9 с), у рыцаря смерти и Иллидана (RoC)
# тянется эхо — LoRA выучивает его как часть голоса и дописывает после фраз (Иллидан ~1,2 с, без адаптера 0,2). Громкий (окна 20 мс не тише пика на TAIL_DB) звук позже конца последнего слова
# (faster-whisper, word_timestamps) + TAIL_AFTER — не речь; от TAIL_MIN с реплика режется в провале громкости
# до 0,4 с после слова (не раньше 0,08 с), а без провала — через TAIL_KEEP с, с затуханием TAIL_FADE.
TAIL_DB, TAIL_AFTER, TAIL_MIN, TAIL_KEEP, TAIL_FADE = 35, 0.2, 0.25, 0.15, 0.03


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cc_read(path):
    with wave.open(path) as w:
        return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float32) / 32768


def letters(text):
    return re.sub(r"[^а-яё]", "", text.lower())


def cer(a, b):
    a, b = letters(a).replace("ё", "е"), letters(b).replace("ё", "е")
    return 1 - difflib.SequenceMatcher(None, a, b).ratio() if a else 1.0


def steps_for(minutes):
    """Правило пользователя: меньше 1 мин — 500, ~1 мин — 1000, ~2 мин — 2000, от 4 мин — 3000; между — линейно,
    шаг 500."""
    if minutes < 1:
        return 500
    s = 1000 * minutes if minutes <= 2 else 2000 + 500 * (minutes - 2)
    return int(min(3000, max(500, round(s / 500) * 500)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("voices", nargs="*")
    ap.add_argument("--out")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--ref", action="append", default=[], help="<голос>=<rel>[,<rel>…]")
    ap.add_argument("--rate", help="темп образца, букв/с: <от>,<до> (по умолчанию 10,15)")
    ap.add_argument("--candidates", type=int, help="N лучших образцов в <голос>/ref-cand/, без обучающих реплик")
    a = ap.parse_args()
    rate_lo, rate_hi = map(float, a.rate.split(",")) if a.rate else REF_RATE
    root = os.path.abspath(a.root)
    out = os.path.abspath(a.out or os.path.join(root, "voice-clips", "wc3-lora"))

    cc = load("cut_clips", os.path.join(HERE, "cut-clips.py"))
    cc.ROOT = root
    cc.ORIGS = os.path.join(root, "voice-origs", "Warcraft III (RU)")
    cc.REFS = os.path.join(root, "voice-refs", "wc3")
    cc.CLIPS = os.path.join(root, "voice-clips", "wc3")
    em = load("emotions", os.path.join(ROOT, "tools", "voices", "emotions.py"))
    em.ROOT = root  # ключи кеша — пути от корня
    emo = em.Emotions(cache=os.path.join(root, "voice-clips", "emotions.json"))
    refs = json.load(open(os.path.join(cc.REFS, "manifest.json"), encoding="utf-8"))
    manual = {}
    for r in a.ref:
        v, rels = r.split("=", 1)
        manual[v] = rels.split(",")

    bases = list(cc.HEROES) + list(cc.UNITS)

    def base_of(v):
        if v in bases:
            return v
        b = max((b for b in bases if v.startswith(b + "-")), key=len, default=None)
        if b is None:
            sys.exit(f"Неизвестный голос: {v}")
        return b

    variants = {}
    for v in refs:
        if v not in bases:
            variants.setdefault(base_of(v), []).append(v)
    wanted = a.voices or [v for v in refs if v in bases]
    todo = list(dict.fromkeys(base_of(v) for v in wanted))

    os.makedirs(out, exist_ok=True)
    mpath = os.path.join(out, "manifest.json")
    manifest = json.load(open(mpath, encoding="utf-8")) if os.path.exists(mpath) else {}
    tmp = tempfile.mkdtemp()
    pieces = cc.Pieces(tmp)
    wpath = os.path.join(out, "words.json")
    words = json.load(open(wpath, encoding="utf-8")) if os.path.exists(wpath) else {}
    clean = {}   # rel → wav без хвоста
    cut_wav = pieces.wav
    pieces.wav = lambda rel: clean.get(rel) or cut_wav(rel)

    def item(rel, num=None):
        e = pieces.info(rel)
        return dict(rel=rel, num=num, text=e["text"], dur=e["dur"], src=e["src"], check=e["check"], alt=e["alt"],
                    asr=pieces.asr[rel]["text"])

    def cut_tail(it):
        """Хвост после речи (рычание зверя, эхо) — отрезать; → секунд отрезано или 0."""
        rel = it["rel"]
        if rel not in words:
            if pieces.model is None:
                # cuDNN torch — раньше Whisper: иначе грузится cuDNN из nvidia/*/bin, и модель эмоций потом падает
                # («Could not load symbol cudnnGetLibConfig»); cuBLAS для faster-whisper — тоже из torch/lib
                import torch
                if torch.cuda.is_available():
                    torch.nn.functional.conv1d(torch.zeros(1, 1, 8, device="cuda"), torch.zeros(1, 1, 3, device="cuda"))
                torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
                if os.path.isdir(torch_lib) and torch_lib not in os.environ["PATH"]:
                    os.add_dll_directory(torch_lib)
                    os.environ["PATH"] = torch_lib + os.pathsep + os.environ["PATH"]
            segs = pieces.whisper().transcribe(cut_wav(rel), language="ru", beam_size=5, word_timestamps=True,
                                               condition_on_previous_text=False, initial_prompt=it["text"])[0]
            ws = [w for s in segs for w in s.words]
            words[rel] = dict(end=round(ws[-1].end, 2) if ws else None)
        end = words[rel]["end"]
        if end is None:
            return 0.0
        x = cc_read(cut_wav(rel))
        h = 480   # 20 мс
        n = len(x) // h
        if n == 0:
            return 0.0
        db = 20 * np.log10(np.sqrt((x[:n * h].reshape(n, h) ** 2).mean(1)) + 1e-9)
        tail = (db[int((end + TAIL_AFTER) * 50):] > db.max() - TAIL_DB).sum() / 50
        # резать в провале между словом и хвостом, если он есть в 0,4 с после слова, иначе — TAIL_KEEP после
        k0, k1 = int(end * 50), min(n, int((end + 0.4) * 50))
        j = k0 + int(np.argmin(db[k0:k1])) if k1 > k0 else k0
        keep = max(end + 0.08, (j + 0.5) / 50) if k1 > k0 and db[j] < db.max() - 30 else end + TAIL_KEEP
        if tail < TAIL_MIN or keep >= len(x) / 24000 - TAIL_FADE:
            return 0.0
        dst = os.path.join(tmp, "clean_" + rel.replace("/", "_") + ".wav")
        cc.ffmpeg("-i", cut_wav(rel), "-af", f"atrim=end={keep:.3f},afade=t=out:st={keep - TAIL_FADE:.3f}:d={TAIL_FADE}",
                  dst)
        clean[rel] = dst
        cut = it["dur"] - keep
        it["dur"] = round(keep, 3)
        return cut

    def arousal(it):
        if "a" not in it:
            it["a"] = emo(os.path.join(cc.ORIGS, it["rel"]))["a"]
        return it["a"]

    def runs(seqs, dur):
        """Окна 1..REF_MAX_PARTS соседних реплик длиной dur."""
        for seq in seqs:
            for i in range(len(seq)):
                for j in range(i + 1, min(i + REF_MAX_PARTS, len(seq)) + 1):
                    w = seq[i:j]
                    if len(w) > 1 and w[-1]["num"] is not None and w[-1]["num"] - w[-2]["num"] > 3:
                        break   # между репликами больше двух чужих строк — другой кусок сцены
                    d = cc.total(w)
                    if d > dur[1]:
                        break
                    if d >= dur[0]:
                        yield w

    def pick_ref(seqs, calm, prefer=(), used=(), dur=REF_DUR, max_cer=REF_CER, excl=False, n=1):
        """Лучший образец из окон: темп в rate, подача по calm (иначе напор), у героя — диалог, затем ближе
        к темпу; prefer — rel, которые лучше (образец CosyVoice варианта), used — уже занятые другим образцом.
        n > 1 — список n лучших без общих реплик (--candidates)."""
        cands = []
        for w in runs(seqs, dur):
            text = cc.clip_text(w)
            if any(p["rel"] in used or p["check"] or cer(p["text"], p["asr"]) > max_cer for p in w):
                continue
            if calm and not excl and "!" in text:
                continue
            d = cc.total(w)
            rate = len(letters(text)) / d
            off = max(0.0, rate_lo - rate, rate - rate_hi)
            cands.append(dict(w=w, text=text, dur=d, rate=rate, off=off, unit=w[0]["num"] is None,
                              pref=sum(p["rel"] in prefer for p in w) / len(w)))
        # энергия — дорого (модель): только для лучших по темпу и длине
        cands.sort(key=lambda c: (c["off"] > 0, -c["pref"], c["off"], len(c["w"]), abs(c["dur"] - 6.5)))
        ranked = []
        for c in cands[:120]:
            c["a"] = sum(arousal(p) for p in c["w"]) / len(c["w"])
            good = c["a"] < CALM if calm else c["a"] >= LOUD
            key = (c["off"] > 0, not good, -c["pref"], c["unit"], round(c["off"]), len(c["w"]),
                   (c["a"] if calm else -c["a"]) + abs(c["dur"] - 6.5) * 0.1 + c["off"] * 0.1)
            ranked.append((key, c))
        emo.save()
        ranked.sort(key=lambda kc: kc[0])
        if n == 1:
            return ranked[0][1] if ranked else None
        picked, taken = [], set()
        for _, c in ranked:
            rels = {p["rel"] for p in c["w"]}
            if not rels & taken:
                picked.append(c)
                taken |= rels
            if len(picked) == n:
                break
        return picked

    def write_ref(vdir, c, name="ref"):
        os.makedirs(vdir, exist_ok=True)
        dst = os.path.join(vdir, name)
        cc.write_clip(pieces, c["w"], dst)
        return dict(parts=[p["rel"] for p in c["w"]], sec=round(c["dur"], 2), text=c["text"],
                    rate=round(c["rate"], 1), arousal=round(c["a"], 3))

    def manual_ref(v, rels):
        w = [item(r) for r in rels]
        text = cc.clip_text(w)
        return dict(w=w, text=text, dur=cc.total(w), rate=len(letters(text)) / cc.total(w),
                    a=sum(arousal(p) for p in w) / len(w))

    try:
        for vid in todo:
            # реплики голоса, разбитые на последовательности соседних (для образца)
            if vid in cc.HEROES:
                name, folder, unit_dirs, title = cc.HEROES[vid]
                dlg = [[item(rel, num) for num, rel in lines] for lines in cc.dialogue_lines(name, folder).values()]
                units = [rel for d in unit_dirs for rel in cc.unit_files(*d.split(":", 2)[:2])]
            else:
                folder, pattern, title = cc.UNITS[vid]
                dlg, units = [], cc.unit_files(folder, pattern)
            groups = {}
            for rel in units:
                groups.setdefault(cc.category(os.path.basename(rel)), []).append(item(rel))
            pool = set(cc.voice_pool(pieces, vid))
            tails = {}
            for p in (p for g in list(groups.values()) + dlg for p in g if p["rel"] in pool):
                c = cut_tail(p)
                if c:
                    tails[os.path.splitext(os.path.basename(p["rel"]))[0]] = round(c, 2)
            json.dump(words, open(wpath, "w", encoding="utf-8"), ensure_ascii=False, indent=0)
            dlg = [[p for p in seq if p["rel"] in pool] for seq in dlg]
            uns = [[p for p in seq if p["rel"] in pool] for seq in groups.values()]
            print(f"== {vid} ({title}): {len(pool)} годных реплик", flush=True)

            seqs = [q for q in dlg + uns if q]
            if a.candidates:   # только кандидаты образца: реплики и manifest.json не трогаются
                for v in [vid] + variants.get(vid, []):
                    if a.voices and v not in a.voices:
                        continue
                    calm = v == vid or not v.endswith("-angry")
                    cdir = os.path.join(out, v, "ref-cand")
                    shutil.rmtree(cdir, ignore_errors=True)
                    for i, c in enumerate(pick_ref(seqs, calm, n=a.candidates), 1):
                        r = write_ref(cdir, c, f"{i:02d}")
                        print(f"   {v} {i:02d}: {r['sec']} с, {r['rate']} букв/с, энергия {r['arousal']} — {r['text']}"
                              f"  [{', '.join(r['parts'])}]")
                continue

            # образцы: голос и его варианты
            refc = {}
            for v in [vid] + variants.get(vid, []):
                if v in manual:
                    refc[v] = manual_ref(v, manual[v])
                    continue
                calm = v == vid or not v.endswith("-angry")
                src = refs.get(v, {}).get("source", [])
                prefer = {s.replace("voice-origs/Warcraft III (RU)/", "") for s in
                          (src if isinstance(src, list) else [src])}
                used = {p["rel"] for c in refc.values() if c for p in c["w"]}
                c = pick_ref(seqs, calm, () if calm else prefer, used)
                if c is None:  # у коротких голосов окон 5,5–7,5 с со сверенным текстом нет
                    c = pick_ref(seqs, calm, () if calm else prefer, used, (4.5, 9.0), 0.15)
                if c is None:  # Маннорот, цеппелин: спокойных реплик без «!» нет вовсе
                    c = pick_ref(seqs, calm, () if calm else prefer, used, (4.5, 9.0), 0.15, excl=True)
                refc[v] = c
            ref_rels = {p["rel"] for c in refc.values() if c for p in c["w"]}

            # обучающие реплики
            chosen, seen, skipped = [], set(), dict(check=0, long=0, dup=0, ref=0)
            for p in [p for seq in dlg for p in seq] + [p for seq in uns for p in seq]:
                key = (letters(p["text"]), round(p["dur"], 1))
                if p["rel"] in ref_rels:
                    skipped["ref"] += 1
                elif p["check"]:
                    skipped["check"] += 1
                elif p["dur"] > MAX_DUR:
                    skipped["long"] += 1
                elif key in seen:
                    skipped["dup"] += 1
                else:
                    seen.add(key)
                    chosen.append(p)
            vdir = os.path.join(out, vid)
            ldir = os.path.join(vdir, "lora")
            shutil.rmtree(ldir, ignore_errors=True)
            os.makedirs(ldir)
            stems = set()
            for p in chosen:
                stem = os.path.splitext(os.path.basename(p["rel"]))[0]
                if stem in stems:
                    stem = p["rel"].split("/")[-2] + "_" + stem
                stems.add(stem)
                shutil.copy(pieces.wav(p["rel"]), os.path.join(ldir, stem + ".wav"))
                with open(os.path.join(ldir, stem + ".txt"), "w", encoding="utf-8") as f:
                    f.write(p["text"].strip() + "\n")
            minutes = sum(p["dur"] for p in chosen) / 60
            m = dict(title=title, clips=len(chosen), minutes=round(minutes, 2), steps=steps_for(minutes),
                     src={s: sum(p["src"] == s for p in chosen) for s in ("fix", "sub", "draft", "asr")},
                     skipped=skipped, tails=tails, ref=None, variants=variants.get(vid, []))
            for v, c in refc.items():
                if c is None:
                    print(f"   {v}: образец не найден")
                    continue
                r = write_ref(os.path.join(out, v), c)
                if v == vid:
                    m["ref"] = r
                else:
                    manifest[v] = dict(title=refs[v].get("title", v), base=vid, ref=r)
                print(f"   образец {v}: {r['sec']} с, {r['rate']} букв/с, энергия {r['arousal']} — {r['text']}")
            manifest[vid] = m
            print(f"   {len(chosen)} реплик, {m['minutes']} мин → {m['steps']} шагов; пропущено {skipped}"
                  f"{f'; хвост отрезан у {len(tails)}' if tails else ''}", flush=True)
            pieces.save()
            json.dump(dict(sorted(manifest.items())), open(mpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    finally:
        pieces.save()
        emo.save()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
