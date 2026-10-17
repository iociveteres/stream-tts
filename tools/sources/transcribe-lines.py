"""Режет длинную запись (YouTube, летсплей, стрим) на фразы с расшифровкой — для build-ref.py.

Использование (из venv проекта, нужен faster-whisper; для --demucs ещё demucs):
    .venv/Scripts/python.exe tools/sources/transcribe-lines.py <аудио...> --out <папка> [--demucs] [--lang ru]
        [--start 1:23] [--end 4:56]

На выходе <папка>/NNN.wav (24 кГц моно) и lines.json: {file, text, src, start, end, lp}.
--demucs сначала отделяет голос от музыки (htdemucs, вокальная дорожка).
"""
import argparse
import glob
import json
import os
import site
import subprocess
import sys
import tempfile


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)


def seconds(t):
    if t is None:
        return None
    parts = [float(p) for p in str(t).split(":")]
    return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))


def cuda_dlls():
    """cuBLAS/cuDNN для faster-whisper: из пакетов nvidia-* или из torch (cu124 везёт их в torch/lib)."""
    sp = site.getsitepackages()[-1]
    for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")) + glob.glob(os.path.join(sp, "torch", "lib")):
        os.add_dll_directory(d)
        os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]


def vocals(src, tmp):
    """Вокальная дорожка htdemucs: <tmp>/htdemucs/<имя>/vocals.wav."""
    subprocess.run([sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs",
                    "-o", tmp, src], check=True)
    name = os.path.splitext(os.path.basename(src))[0]
    return os.path.join(tmp, "htdemucs", name, "vocals.wav")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio", nargs="+")
    ap.add_argument("--out", required=True)
    ap.add_argument("--demucs", action="store_true")
    ap.add_argument("--lang", default="ru")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--min", type=float, default=2.0, help="фразы короче, с, не сохранять")
    ap.add_argument("--max", type=float, default=12.0, help="фразы длиннее, с, не сохранять")
    a = ap.parse_args()

    cuda_dlls()
    from faster_whisper import WhisperModel
    try:
        model = WhisperModel("large-v3", device="cuda", compute_type="float16")
    except Exception as e:  # noqa: BLE001
        print("CUDA недоступна, Whisper на CPU:", e, file=sys.stderr)
        model = WhisperModel("large-v3", device="cpu", compute_type="int8")

    os.makedirs(a.out, exist_ok=True)
    lines_path = os.path.join(a.out, "lines.json")
    lines = json.load(open(lines_path, encoding="utf-8")) if os.path.exists(lines_path) else []
    tmp = tempfile.mkdtemp(prefix="tr-")
    for src in a.audio:
        cut = os.path.join(tmp, "cut.wav")
        rng = (["-ss", str(seconds(a.start))] if a.start else []) + \
              (["-to", str(seconds(a.end))] if a.end else [])
        ffmpeg(*rng, "-i", src, "-ac", "1", "-ar", "44100", cut)
        work = vocals(cut, tmp) if a.demucs else cut
        segs, _ = model.transcribe(work, language=a.lang, beam_size=5, vad_filter=True,
                                   condition_on_previous_text=False)
        offset = seconds(a.start) or 0.0
        for s in segs:
            dur = s.end - s.start
            if not (a.min <= dur <= a.max) or s.no_speech_prob > 0.5:
                continue
            fn = f"{len(lines):03d}.wav"
            ffmpeg("-ss", f"{max(0, s.start - 0.05):.2f}", "-to", f"{s.end + 0.1:.2f}", "-i", work,
                   "-ac", "1", "-ar", "24000", os.path.join(a.out, fn))
            lines.append(dict(file=fn, text=s.text.strip(), src=os.path.basename(src),
                              start=round(offset + s.start, 2), end=round(offset + s.end, 2),
                              lp=round(s.avg_logprob, 3)))
            print(f"{len(lines) - 1:3d} {dur:5.1f}s lp={s.avg_logprob:5.2f} {s.text.strip()}")
    with open(lines_path, "w", encoding="utf-8") as f:
        json.dump(lines, f, ensure_ascii=False, indent=1)
    print(f"{len(lines)} фраз -> {a.out}")


if __name__ == "__main__":
    main()
