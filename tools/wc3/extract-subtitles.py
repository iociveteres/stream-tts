"""Достаёт русские субтитры диалогов из карт кампаний WC3 1.31: {путь к звуку: текст}.

usage: python tools/wc3/extract-subtitles.py <папка с картами> [out.json]
  Карты (*.w3m, *.w3x) извлекаются из CASC так же, как звуки: маска war3.w3mod:campaign*.
  По умолчанию результат пишется в voice-clips/cosyvoice/wc3/subtitles.json, его читает cut-clips.py.

Строки лежат в самой карте: war3map.wts (английский, с комментарием-путём к звуку у каждой
реплики) и _Locales\\ruRU.w3mod\\war3map.wts (те же номера STRING, русский текст).
Файлы в картах зашифрованы, поэтому поверх mpyq добавлена расшифровка. Нужен pip install mpyq.
"""
import bz2, glob, json, os, re, struct, sys, zlib

import mpyq
from mpyq import (MPQ_FILE_COMPRESS, MPQ_FILE_ENCRYPTED, MPQ_FILE_EXISTS, MPQ_FILE_FIX_KEY,
                  MPQ_FILE_SECTOR_CRC, MPQ_FILE_SINGLE_UNIT)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))  # tools/<группа>/ -> корень проекта


def decompress(data):
    kind = data[0]
    if kind == 2:
        return zlib.decompress(data[1:])
    if kind == 16:
        return bz2.decompress(data[1:])
    raise RuntimeError(f"неподдерживаемое сжатие {kind:#x}")


class MPQ(mpyq.MPQArchive):
    def __init__(self, path):
        super().__init__(path, listfile=False)

    def _decrypt_tail(self, data, key):
        n = len(data) // 4 * 4
        return self._decrypt(data[:n], key) + data[n:]

    def read(self, name):
        a, b = self._hash(name, "HASH_A"), self._hash(name, "HASH_B")
        entry = next((e for e in self.hash_table if e.hash_a == a and e.hash_b == b), None)
        if entry is None:
            return None
        blk = self.block_table[entry.block_table_index]
        if not blk.flags & MPQ_FILE_EXISTS or blk.archived_size == 0:
            return None
        self.file.seek(blk.offset + self.header["offset"])
        raw = self.file.read(blk.archived_size)
        key = None
        if blk.flags & MPQ_FILE_ENCRYPTED:
            key = self._hash(name.split("\\")[-1], "TABLE")
            if blk.flags & MPQ_FILE_FIX_KEY:
                key = ((key + blk.offset) ^ blk.size) & 0xFFFFFFFF
        if blk.flags & MPQ_FILE_SINGLE_UNIT:
            if key is not None:
                raw = self._decrypt_tail(raw, key)
            compressed = blk.flags & MPQ_FILE_COMPRESS and blk.size > blk.archived_size
            return decompress(raw) if compressed else raw
        sector = 512 << self.header["sector_size_shift"]
        nsec = (blk.size + sector - 1) // sector
        count = nsec + 1 + (1 if blk.flags & MPQ_FILE_SECTOR_CRC else 0)
        table = raw[:4 * count]
        if key is not None:
            table = self._decrypt_tail(table, (key - 1) & 0xFFFFFFFF)
        pos = struct.unpack(f"<{count}I", table)
        out, left = [], blk.size
        for i in range(nsec):
            s = raw[pos[i]:pos[i + 1]]
            if key is not None:
                s = self._decrypt_tail(s, (key + i) & 0xFFFFFFFF)
            if blk.flags & MPQ_FILE_COMPRESS and len(s) < min(sector, left):
                s = decompress(s)
            out.append(s)
            left -= len(s)
        return b"".join(out)


def parse_wts(data):
    text = data.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    return {int(m.group(1)): (m.group(2), m.group(3).strip()) for m in
            re.finditer(r"STRING (\d+)\n((?://[^\n]*\n)*)\{\n(.*?)\n\}", text, re.S)}


def main():
    maps_dir = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "voice-clips", "cosyvoice", "wc3", "subtitles.json")
    subs = {}
    for path in sorted(glob.glob(os.path.join(maps_dir, "**", "*.w3[mx]"), recursive=True)):
        mpq = MPQ(path)
        base, ru = mpq.read("war3map.wts"), mpq.read("_Locales\\ruRU.w3mod\\war3map.wts")
        if not base or not ru:
            continue
        base, ru = parse_wts(base), parse_wts(ru)
        for sid, (comment, _) in base.items():
            m = re.search(r"//\s*(Sound\\Dialogue\\\S+\.(?:mp3|wav))", comment, re.I)
            if m and sid in ru:
                subs[m.group(1).replace("\\", "/").lower()] = ru[sid][1]
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"{len(subs)} реплик -> {dst}")


if __name__ == "__main__":
    main()
