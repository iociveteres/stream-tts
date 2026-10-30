"""espeech-server — синтез ESpeech-TTS-1_SFT-256K (F5-TTS, русский) для stream-tts: HTTP API как у cosyvoice-server.
Запуск: python -m espeech --models models/espeech --voices voices (server.py). Устройство — docs/espeech.md.

ESPEECH_DLL_DIRS — папки с DLL для torch через os.pathsep, их добавляют в поиск DLL до импорта torch: в программе
для Windows (engines/espeech/launch.py stream-tts) это заглушки вместо библиотек CUDA, которые torch_cuda.dll
импортирует, а синтез не вызывает, и cuBLAS из CUDA cosyvoice-server (или его заглушки, если CUDA нет)."""
import os as _os

_DLL_DIRS = [_os.add_dll_directory(_d) for _d in _os.environ.get("ESPEECH_DLL_DIRS", "").split(_os.pathsep)
             if _d and _os.path.isdir(_d) and hasattr(_os, "add_dll_directory")]   # ссылки держат папки в поиске
