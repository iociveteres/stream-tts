"""Часть zip по HTTP: только нужные файлы архива, диапазонами (Range), без скачивания архива целиком.

Так программа берёт из колеса torch (1,9 ГБ) около 430 МБ, а из чекпойнта ESpeech (2,7 ГБ, torch.save — тоже zip)
только EMA-копию весов (engines/espeech/download.py). Описание архива закреплено в downloads.json: url, size (весь
архив), tail (начало хвоста: центральный каталог и его конец) и tail_sha256. Хвост закрепляет имена, смещения,
размеры и CRC32 всех файлов архива; выбранные файлы при распаковке сверяются по CRC32 (zipfile) и по общей
sha256 (digest: имя, \\0, содержимое — по порядку имён), так что подмену заметит и то, и другое.

Выбор файлов — members (список имён) или exclude (маски fnmatch без учёта регистра: всё, кроме них). Нужные
файлы идут диапазонами: по порядку в архиве, соседние склеиваются, если между ними меньше GAP ненужных байт (лишний
запрос дороже). Скачанное пишется подряд в один файл .part — хвост первым, потом диапазоны, — поэтому докачка
продолжает с его размера. Читает такой файл RangeFile: для zipfile это архив целиком, а в дырах — ошибка."""
import bisect, fnmatch, hashlib, io, os, zipfile

GAP = 256 << 10


class RangeFile(io.RawIOBase):
    """Архив размером size, от которого есть только куски segments [(начало в архиве, конец, начало в файле path)]."""

    def __init__(self, path, size, segments):
        self.f = open(path, "rb")
        self.size = size
        self.segs = sorted(segments)
        self.starts = [s[0] for s in self.segs]
        self.pos = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, offset, whence=0):
        self.pos = (offset if whence == 0 else self.pos + offset if whence == 1 else self.size + offset)
        return self.pos

    def tell(self):
        return self.pos

    def readinto(self, b):
        """Читает до конца b или архива, переходя через стыки кусков (zipfile ждёт заголовки целиком)."""
        want, got = min(len(b), self.size - self.pos), 0
        while got < want:
            i = bisect.bisect_right(self.starts, self.pos) - 1
            if i < 0 or self.pos >= self.segs[i][1]:
                raise OSError(f"нет байта {self.pos} архива: этот кусок не скачивался")
            start, end, at = self.segs[i]
            self.f.seek(at + self.pos - start)
            data = self.f.read(min(want - got, end - self.pos))
            if not data:
                raise OSError(f"файл {self.f.name} короче, чем нужно")
            b[got:got + len(data)] = data
            got += len(data)
            self.pos += len(data)
        return got

    def close(self):
        self.f.close()
        super().close()


def tail_len(spec):
    return spec["size"] - spec["tail"]


def check_tail(part, spec):
    """Хвост в начале part совпадает с закреплённым."""
    h, left = hashlib.sha256(), tail_len(spec)
    with open(part, "rb") as f:
        while left:
            b = f.read(min(left, 1 << 20))
            if not b:
                break
            h.update(b)
            left -= len(b)
    return not left and h.hexdigest() == spec["tail_sha256"]


def selected(spec, infos):
    """Выбранные файлы архива (ZipInfo) по members или exclude."""
    files = [i for i in infos if not i.is_dir()]
    if "members" in spec:
        want = set(spec["members"])
        out = [i for i in files if i.filename in want]
        missing = want - {i.filename for i in out}
        if missing:
            raise RuntimeError(f"в архиве нет {', '.join(sorted(missing)[:5])}")
        return out
    masks = [m.lower() for m in spec.get("exclude", ())]
    return [i for i in files if not any(fnmatch.fnmatch(i.filename.lower(), m) for m in masks)]


def plan(part, spec):
    """(выбранные файлы, диапазоны [(начало, конец)] в архиве) по хвосту, который уже лежит в начале part."""
    tail = (spec["tail"], spec["size"], 0)
    with RangeFile(part, spec["size"], [tail]) as rf, zipfile.ZipFile(rf) as z:
        infos = z.infolist()
    sel = selected(spec, infos)
    return sel, ranges_of(infos, sel, spec["tail"])


def ranges_of(infos, sel, tail):
    """Диапазоны [(начало, конец)] архива с файлами sel: конец файла — начало следующего (локальный заголовок,
    данные и дескриптор данных) или хвост."""
    order = sorted(i.header_offset for i in infos)
    ends = dict(zip(order, order[1:] + [tail]))
    ranges = []
    for start in sorted(i.header_offset for i in sel):
        if ranges and start - ranges[-1][1] < GAP:
            ranges[-1][1] = ends[start]
        else:
            ranges.append([start, ends[start]])
    return [tuple(r) for r in ranges]


def segments(spec, ranges):
    """Куски архива в part: хвост, потом диапазоны подряд."""
    out, at = [(spec["tail"], spec["size"], 0)], tail_len(spec)
    for start, end in ranges:
        out.append((start, end, at))
        at += end - start
    return out


def download_size(spec, ranges):
    return tail_len(spec) + sum(e - s for s, e in ranges)


def extract(part, spec, ranges, sink, progress=None):
    """Выбранные файлы по порядку имён: sink(ZipInfo, файл для чтения). Сверяет digest; ошибка — RuntimeError."""
    h = hashlib.sha256()
    with RangeFile(part, spec["size"], segments(spec, ranges)) as rf, zipfile.ZipFile(rf) as z:
        for info in sorted(selected(spec, z.infolist()), key=lambda i: i.filename):
            h.update(info.filename.encode() + b"\0")
            with z.open(info) as src:
                sink(info, _Hashing(src, h))
            if progress:
                progress(info)
    if h.hexdigest() != spec["digest"]:
        raise RuntimeError(f"{os.path.basename(spec['url'])}: файлы не совпали с закреплёнными")


class _Hashing(io.RawIOBase):
    """Чтение с подсчётом sha256: sink может читать файл как угодно, хэш считается по прочитанному."""

    def __init__(self, src, h):
        self.src, self.h = src, h

    def readable(self):
        return True

    def readinto(self, b):
        data = self.src.read(len(b))
        self.h.update(data)
        b[:len(data)] = data
        return len(data)


def digest_of(path, spec):
    """digest выбранных файлов полного архива (для закрепления: packaging/windows/pin-espeech.py)."""
    h = hashlib.sha256()
    with zipfile.ZipFile(path) as z:
        for info in sorted(selected(spec, z.infolist()), key=lambda i: i.filename):
            h.update(info.filename.encode() + b"\0")
            with z.open(info) as src:
                while b := src.read(1 << 20):
                    h.update(b)
    return h.hexdigest()
