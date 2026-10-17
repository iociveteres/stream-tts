"""Точка входа собранной программы (Nuitka, packaging/windows/StreamTTS.py); работает и из исходников.

usage: StreamTTS.exe [--no-browser]
       python -m streamtts.desktop [--no-browser]   (из папки stream-tts)

Поверх __main__.main(): настройки из <данные>/stream-tts.env (KEY=VALUE, окружение важнее; при первом запуске
кладётся пример с комментариями), одна копия на папку данных (вторая открывает админку уже запущенной и выходит),
лог в консоль и в <данные>/logs/stream-tts.log, админка открывается в браузере, когда сервер ответил.
Это консольное приложение: закрыть окно — выключить stream-tts, встроенный cosyvoice-server завершается вместе с
ним (Job Object в runtime.py). Ошибка старта (порт занят и т. п.) не закрывает окно, пока не нажат Enter.
"""
import argparse, hashlib, importlib, logging, logging.handlers, os, sys, threading, time, urllib.request, webbrowser

from . import paths

ENV_FILE = os.path.join(paths.DATA, "stream-tts.env")
ENV_EXAMPLE = """\
# Настройки stream-tts: KEY=VALUE, строки с # пропускаются. Действуют после перезапуска программы.
# Полный список — docs/stream-tts.md, «Развёртывание». Переменные окружения Windows важнее этого файла.

# Порт админки и оверлея (http://127.0.0.1:8090/)
#STREAM_TTS_PORT=8090
# 0.0.0.0 — открыть оверлей для OBS на другом ПК; тогда обязателен пароль админки
#STREAM_TTS_BIND=127.0.0.1
#STREAM_TTS_PASSWORD=
# Внешний cosyvoice-server вместо встроенного (Docker, другая машина)
#COSYVOICE_URL=http://192.168.1.10:8080
# Устройство синтеза: cuda, vulkan или cpu (иначе — выбор во вкладке «Сервер»)
#COSYVOICE_BACKEND=
"""
log = logging.getLogger("stream-tts")


def load_env():
    """<DATA>/stream-tts.env в os.environ, не перетирая заданное. До импорта config: он читает окружение сразу."""
    os.makedirs(paths.DATA, exist_ok=True)
    if not os.path.exists(ENV_FILE):
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write(ENV_EXAMPLE)
        return
    with open(ENV_FILE, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and not os.environ.get(k, "").strip():
                os.environ[k] = v


def single_instance():
    """True, если других копий с той же папкой данных нет. Блокировка живёт, пока жив процесс."""
    tag = hashlib.sha1(os.path.normcase(os.path.abspath(paths.DATA)).encode()).hexdigest()[:8]
    if os.name == "nt":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        # общий для всех копий: по нему установщик (AppMutex в StreamTTS.iss) видит запущенную программу
        single_instance.app = k32.CreateMutexW(None, False, "StreamTTS")
        single_instance.handle = k32.CreateMutexW(None, False, f"Local\\StreamTTS-{tag}")
        return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS
    import fcntl
    single_instance.handle = open(os.path.join(paths.DATA, f".lock-{tag}"), "w")
    try:
        fcntl.flock(single_instance.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def setup_logging():
    """Обработчики root до __main__.main(): там basicConfig, который при готовых обработчиках ничего не делает."""
    fmt = logging.Formatter("%(asctime)s %(name)s: %(message)s", "%H:%M:%S")
    os.makedirs(os.path.join(paths.DATA, "logs"), exist_ok=True)
    file = logging.handlers.RotatingFileHandler(os.path.join(paths.DATA, "logs", "stream-tts.log"),
                                                maxBytes=2 << 20, backupCount=3, encoding="utf-8")
    file.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file)
    if sys.stderr:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)


def admin_url():
    from . import config
    host = config.LISTEN_HOST
    if host in ("0.0.0.0", "::", "") or host in config.LOCAL_BINDS:
        host = "127.0.0.1"
    return f"http://{host}:{config.LISTEN_PORT}/"


def open_when_ready(url, timeout=120):
    """Ждёт ответа /login (он открыт без пароля) и открывает админку. Поток-демон: выход его не ждёт."""
    def run():
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                with urllib.request.urlopen(url + "login", timeout=2):
                    webbrowser.open(url)
                    return
            except OSError:
                time.sleep(0.5)
    threading.Thread(target=run, daemon=True, name="open-browser").start()


def set_title(text):
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetConsoleTitleW(text)


def pause():
    """Чтобы окно консоли с ошибкой не закрылось сразу."""
    if sys.stdin and sys.stdin.isatty():
        try:
            input("\nНажмите Enter, чтобы закрыть окно…")
        except (EOFError, KeyboardInterrupt):
            pass


def main():
    ap = argparse.ArgumentParser(prog="StreamTTS", description="Озвучка наград Twitch голосами cosyvoice")
    ap.add_argument("--no-browser", action="store_true", help="не открывать админку в браузере")
    args = ap.parse_args()

    load_env()
    importlib.reload(paths)  # STREAM_TTS_MODELS и др. могли прийти из stream-tts.env
    url = admin_url()
    if not single_instance():
        print(f"StreamTTS уже запущен: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    setup_logging()
    set_title(f"StreamTTS — {url}")
    print(f"StreamTTS: админка {url}, оверлей для OBS {url}overlay\n"
          f"Данные: {paths.DATA}\nЗакройте это окно, чтобы выключить озвучку.\n", flush=True)
    if not args.no_browser:
        open_when_ready(url)

    from . import __main__ as app
    try:
        app.main()
    except KeyboardInterrupt:
        pass
    except SystemExit as e:
        if e.code not in (0, None):
            pause()
        raise
    except OSError as e:
        # порт занят: run_app падает при bind
        log.error("не удалось запустить: %s", e)
        log.error("порт %s занят другой программой? Его можно сменить в %s (STREAM_TTS_PORT)",
                  url.rsplit(":", 1)[-1].rstrip("/"), ENV_FILE)
        pause()
        sys.exit(1)
    except Exception:
        log.exception("stream-tts упал")
        pause()
        sys.exit(1)


if __name__ == "__main__":
    main()
