"""espeech-server — синтез ESpeech-TTS-1_SFT-256K (F5-TTS, русский) для stream-tts: HTTP API как у cosyvoice-server.
Запуск: python -m espeech --models models/espeech --voices voices (server.py). Устройство — docs/espeech.md.

ESPEECH_DLL_DIRS — библиотеки CUDA для облегчённого torch через os.pathsep, их подключают до импорта torch
(engines/espeech/launch.py stream-tts, dll_dirs()): заглушки библиотек, которые torch загружает, а синтез не вызывает,
и cuBLAS из CUDA cosyvoice-server (или его заглушки, если CUDA нет). Windows: папки — в поиск DLL. Linux: файлы и все
lib*.so* из папок загружаются с RTLD_GLOBAL — потом ld.so находит зависимости torch по SONAME среди загруженных."""
import os as _os

_ENTRIES = [_d for _d in _os.environ.get("ESPEECH_DLL_DIRS", "").split(_os.pathsep) if _d]
if hasattr(_os, "add_dll_directory"):
    _DLL_DIRS = [_os.add_dll_directory(_d) for _d in _ENTRIES if _os.path.isdir(_d)]   # ссылки держат папки в поиске
else:
    import ctypes as _ctypes

    def _libs(p):
        if _os.path.isfile(p):
            return [p]
        return sorted(_os.path.join(p, f) for f in (_os.listdir(p) if _os.path.isdir(p) else ())
                      if f.startswith("lib") and ".so" in f and _os.path.isfile(_os.path.join(p, f)))

    _DLL_DIRS = [_ctypes.CDLL(_f, mode=_ctypes.RTLD_GLOBAL) for _e in _ENTRIES for _f in _libs(_e)]
