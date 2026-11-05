"""Голоса supertonic-server: файлы <имя>.supertonic — JSON стиля из voice_styles/*.json модели как есть (style_ttl,
style_dp; у каждого dims и data). Имя голоса — имя файла; лежат в <папка>/<имя>.supertonic или
<папка>/<набор>/<имя>.supertonic (один уровень подпапок, как у cosyvoice-server и espeech-server). Папка
перечитывается при каждом запросе: stream-tts кладёт и убирает голоса без перезапуска сервера."""
import glob, json, os, threading

from .model import Style

EXT = ".supertonic"


class Voices:
    def __init__(self, folder):
        self.dir = folder
        self.files = {}     # имя → путь
        self.cache = {}     # имя → (mtime, Style)
        self.hidden = {}    # имя → mtime файла: забыт DELETE /speaker, пока файл не изменится
        self.lock = threading.Lock()

    def scan(self):
        found = {}
        if self.dir and os.path.isdir(self.dir):
            files = sorted(glob.glob(os.path.join(self.dir, "*" + EXT))) + \
                sorted(glob.glob(os.path.join(self.dir, "*", "*" + EXT)))
            for f in files:
                name = os.path.splitext(os.path.basename(f))[0]
                if name in found:   # одно имя в двух наборах — первый (корень, затем наборы по алфавиту)
                    continue
                try:
                    mt = os.path.getmtime(f)
                except OSError:
                    continue
                with self.lock:
                    if self.hidden.get(name) == mt:
                        continue
                    self.hidden.pop(name, None)
                found[name] = f
        with self.lock:
            self.files = found
            for name in [n for n in self.cache if n not in found]:
                del self.cache[name]
        return found

    def names(self):
        return sorted(self.scan())

    def get(self, name):
        """→ Style; KeyError — нет голоса, ValueError — файл не читается как стиль."""
        path = self.scan()[name]
        mt = os.path.getmtime(path)
        with self.lock:
            hit = self.cache.get(name)
        if hit and hit[0] == mt:
            return hit[1]
        try:
            with open(path, encoding="utf-8") as f:
                style = Style(json.load(f))
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise ValueError(f"голос {name} ({os.path.basename(path)}) не читается: {e}") from None
        with self.lock:
            self.cache[name] = (mt, style)
        return style

    def delete(self, name):
        """Забыть голос: файл остаётся, голос скрыт, пока файл не изменится (как DELETE у espeech-server и
        cosyvoice-server: после перезапуска вернётся). → был ли такой голос."""
        path = self.scan().get(name)
        if path is None:
            return False
        with self.lock:
            self.hidden[name] = os.path.getmtime(path)
            self.files.pop(name, None)
            self.cache.pop(name, None)
        return True
