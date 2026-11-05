"""Главный модуль сборки Nuitka (build-exe.py): сам код — stream-tts/streamtts/desktop.py.

StreamTTS.exe --supertonic <аргументы> — supertonic-server (пакет supertonic/ проекта, скомпилирован в программу):
так его запускает stream-tts (engines/supertonic/launch.py, FLAG) — без отдельного Python, numpy и onnxruntime уже в
программе. Ключ разбирается до импорта desktop: у сервера нет ни настроек, ни блокировки одной копии, ни браузера."""
import sys

if sys.argv[1:2] == ["--supertonic"]:
    from supertonic.server import main as server

    server(sys.argv[2:])
else:
    from streamtts.desktop import main

    main()
