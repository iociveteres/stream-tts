"""Реплики голосов Warcraft III для LoRA ESpeech и образец 6–7 с: вход tools/espeech/train.py.

usage: .venv/Scripts/python.exe tools/wc3/lora-clips.py [голос …] [--out voice-clips/wc3-lora] [--root <корень>]
                                                         [--ref <голос>=<rel>[,<rel>…]]
  голос   wc3-* из HEROES и UNITS cut-clips.py или вариант из voice-refs/wc3/manifest.json (wc3-thrall-angry);
          вариант собирается вместе со своим персонажем. По умолчанию — все голоса манифеста.
  --root  корень проекта с voice-origs/, voice-clips/, voice-refs/ (по умолчанию этот; из worktree — основная копия).
  --ref   образец вручную: реплики (пути от «voice-origs/Warcraft III (RU)/») подряд с паузой 0,25 с.

Реплики — те же, что режет tools/wc3/cut-clips.py (voice_pool: диалоги кампаний и ответы юнита, без «не брать» из
review.py, без рычания и галлюцинаций Whisper), текст по убыванию доверия: text-fixes.json → субтитры кампаний →
text-fixes-draft.json → ASR (кеш asr.json; нет в кеше — распознаётся faster-whisper и дописывается). Не берутся
реплики с пометкой «сверить текст», длиннее 30 с (предел train.py) и повторы одного текста (pissed6 = pissed1).
Тишина по краям обрезана (−50 дБ, как в cut-clips), 24 кГц моно PCM16; текст ровно как звучит, без «+».

Варианты одного персонажа (<голос>-angry и т. п. в voice-refs/wc3/manifest.json) делят один адаптер, у них свой
только образец. Образец — 5,5–7,5 с (если таких нет — 4,5–9 с) одной репликой или 2–4 соседними (одна сцена диалога
или один тип ответа юнита), темп 10–15 русских букв в секунду (медленный образец даёт лишние слова в конце,
docs/espeech.md), текст сверен с ASR (разница букв ≤ 10 %). Основному голосу — спокойный (энергия
tools/voices/emotions.py < 0,6, без «!», если такие есть), варианту -angry — с напором (от 0,78), сначала из реплик его образца
CosyVoice; у героя сначала диалоги, у медленных голосов — самый быстрый из спокойных. Реплики образцов всех
вариантов персонажа в обучение не входят, у вариантов они разные.

Выход <out>/<голос>/: lora/<имя>.wav + .txt, ref.wav + ref.txt; у варианта — только ref.wav + ref.txt.
<out>/manifest.json — {голос: {title, clips, minutes, steps, src, skipped, ref, variants}}, у варианта
{title, base, ref}; steps — шаги LoRA по правилу пользователя (от 4 мин — 3000, ~2 мин — 2000, ~1 мин — 1000,
меньше 1 мин — 500).
Нужны faster-whisper (если реплики нет в asr.json), torch и transformers (энергия; кеш voice-clips/emotions.json),
ffmpeg в PATH.
"""
import argparse, difflib, importlib.util, json, os, re, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # tools/<группа>/ -> корень проекта

MAX_DUR = 30.0          # train.py пропускает реплики длиннее
REF_DUR = (5.5, 7.5)    # образец ESpeech: 6–7 с (docs/espeech.md, «Образец голоса»)
REF_RATE = (10.0, 15.0)  # букв в секунду
REF_MAX_PARTS = 4
REF_CER = 0.10          # текст образца против ASR
CALM, LOUD = 0.6, 0.78  # пороги энергии emotions.py
GAP = 0.25              # пауза между репликами склеенного образца, как в cut-clips


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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
    a = ap.parse_args()
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

    def item(rel, num=None):
        e = pieces.info(rel)
        return dict(rel=rel, num=num, text=e["text"], dur=e["dur"], src=e["src"], check=e["check"], alt=e["alt"],
                    asr=pieces.asr[rel]["text"])

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

    def pick_ref(seqs, calm, prefer=(), used=(), dur=REF_DUR, max_cer=REF_CER, excl=False):
        """Лучший образец из окон: темп в REF_RATE, подача по calm (иначе напор), у героя — диалог, затем ближе
        к темпу; prefer — rel, которые лучше (образец CosyVoice варианта), used — уже занятые другим образцом."""
        cands = []
        for w in runs(seqs, dur):
            text = cc.clip_text(w)
            if any(p["rel"] in used or p["check"] or cer(p["text"], p["asr"]) > max_cer for p in w):
                continue
            if calm and not excl and "!" in text:
                continue
            d = cc.total(w)
            rate = len(letters(text)) / d
            off = max(0.0, REF_RATE[0] - rate, rate - REF_RATE[1])
            cands.append(dict(w=w, text=text, dur=d, rate=rate, off=off, unit=w[0]["num"] is None,
                              pref=sum(p["rel"] in prefer for p in w) / len(w)))
        # энергия — дорого (модель): только для лучших по темпу и длине
        cands.sort(key=lambda c: (c["off"] > 0, -c["pref"], c["off"], len(c["w"]), abs(c["dur"] - 6.5)))
        best = None
        for c in cands[:120]:
            c["a"] = sum(arousal(p) for p in c["w"]) / len(c["w"])
            good = c["a"] < CALM if calm else c["a"] >= LOUD
            key = (c["off"] > 0, not good, -c["pref"], c["unit"], round(c["off"]), len(c["w"]),
                   (c["a"] if calm else -c["a"]) + abs(c["dur"] - 6.5) * 0.1 + c["off"] * 0.1)
            if best is None or key < best[0]:
                best = (key, c)
        emo.save()
        return best[1] if best else None

    def write_ref(vdir, c):
        os.makedirs(vdir, exist_ok=True)
        dst = os.path.join(vdir, "ref")
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
            dlg = [[p for p in seq if p["rel"] in pool] for seq in dlg]
            uns = [[p for p in seq if p["rel"] in pool] for seq in groups.values()]
            print(f"== {vid} ({title}): {len(pool)} годных реплик", flush=True)

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
                seqs = [q for q in dlg + uns if q]
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
                     skipped=skipped, ref=None, variants=variants.get(vid, []))
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
            print(f"   {len(chosen)} реплик, {m['minutes']} мин → {m['steps']} шагов; пропущено {skipped}", flush=True)
            pieces.save()
            json.dump(dict(sorted(manifest.items())), open(mpath, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    finally:
        pieces.save()
        emo.save()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
