"""Распознавание речи для уровня real (test_real.py): faster-whisper small, язык ru.

Запускается подпроцессом из корневого .venv (Python 3.11, faster-whisper; в stream-tts/.venv его нет). cuBLAS и
cuDNN — из пакетов nvidia-* или torch/lib (cu124), как в tools/voices/synth.py; без CUDA — на процессоре. Модель
small, а не large-v3: ей хватает ~0,5 ГБ видеопамяти рядом со вторым cosyvoice-server и Docker-версией, а отличить
речь от шума или мусора она может.

usage: .venv/Scripts/python.exe tests/e2e/asr.py <звук.wav>...
Выход (stdout): JSON {"device": "cuda"|"cpu", "texts": [текст каждого файла]}."""
import glob, json, os, site, sys


def cuda_dlls():
    sp = site.getsitepackages()[-1]
    for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")) + glob.glob(os.path.join(sp, "torch", "lib")):
        os.add_dll_directory(d)
        os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]


def main():
    files = sys.argv[1:]
    cuda_dlls()
    from faster_whisper import WhisperModel
    texts = None
    for device, compute in (("cuda", "float16"), ("cpu", "int8")):
        try:
            model = WhisperModel("small", device=device, compute_type=compute)
            # ошибка cuBLAS/cuDNN всплывает только на первом распознавании
            texts = [" ".join(s.text.strip() for s in model.transcribe(f, language="ru", beam_size=5)[0])
                     for f in files]
            break
        except Exception as e:  # noqa: BLE001
            print(f"faster-whisper на {device}: {e}", file=sys.stderr)
    if texts is None:
        sys.exit(1)
    print(json.dumps(dict(device=device, texts=texts), ensure_ascii=False))


if __name__ == "__main__":
    main()
