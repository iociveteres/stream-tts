"""Достаёт файлы из архивов S.T.A.L.K.E.R. (Clear Sky / Call of Pripyat: resources.db*, *.xdb*).

Использование:
    python tools/meme/extract-stalker.py <папка resources> <регэксп пути> <куда> [--list]

Пример (фразы бандитов):
    python tools/meme/extract-stalker.py "E:/Games/steamapps/common/Stalker Call of Pripyat/resources" \
        "characters_voice/human_0./bandit/" voice-origs/stalker-cop

Формат — по исходникам OpenXRay (xrCore/LocatorAPI.cpp, LzHuf.cpp): архив — цепочка чанков
<u32 id><u32 size>; таблица файлов лежит в чанке 1, при флаге 0x80000000 сжата LZHUF.
Запись таблицы: u16 размер, u32 size_real, u32 size_compr, u32 crc, имя, u32 смещение.
Шифрование архивов ShoC (без ini-заголовка) не поддержано. Файлы, сжатые LZO, пропускаются.
"""
import argparse
import glob
import os
import re
import struct
import sys

COMPRESSED = 0x80000000

# --- LZHUF (порт Decode из OpenXRay xrCore/LzHuf.cpp) ---
N, F, THRESHOLD = 4096, 60, 2
N_CHAR = 256 - THRESHOLD + F
T = N_CHAR * 2 - 1
R = T - 1
MAX_FREQ = 0x4000

D_CODE = bytes([0] * 32 + [1] * 16 + [2] * 16 + [3] * 16 + [i for i in range(4, 12) for _ in range(8)]
               + [i for i in range(12, 24) for _ in range(4)] + [i for i in range(24, 48) for _ in range(2)]
               + list(range(48, 64)))
D_LEN = bytes([3] * 32 + [4] * 48 + [5] * 64 + [6] * 48 + [7] * 48 + [8] * 16)


def lzhuf_decode(src):
    size = struct.unpack_from("<I", src)[0]
    pos = 4
    getbuf = getlen = 0
    freq = [0] * (T + 1)
    prnt = [0] * (T + N_CHAR + 1)
    son = [0] * T
    for i in range(N_CHAR):
        freq[i] = 1
        son[i] = i + T
        prnt[i + T] = i
    i, j = 0, N_CHAR
    while j <= R:
        freq[j] = freq[i] + freq[i + 1]
        son[j] = i
        prnt[i] = prnt[i + 1] = j
        i += 2
        j += 1
    freq[T] = 0xFFFF
    prnt[R] = 0

    def fill():
        nonlocal getbuf, getlen, pos
        while getlen <= 8:
            b = src[pos] if pos < len(src) else 0
            pos += 1
            getbuf |= b << (8 - getlen)
            getlen += 8

    def get_bit():
        nonlocal getbuf, getlen
        fill()
        bit = (getbuf >> 15) & 1
        getbuf = (getbuf << 1) & 0xFFFF
        getlen -= 1
        return bit

    def get_byte():
        nonlocal getbuf, getlen
        fill()
        b = (getbuf >> 8) & 0xFF
        getbuf = (getbuf << 8) & 0xFFFF
        getlen -= 8
        return b

    def reconst():
        j = 0
        for i in range(T):
            if son[i] >= T:
                freq[j] = (freq[i] + 1) // 2
                son[j] = son[i]
                j += 1
        i, j = 0, N_CHAR
        while j < T:
            f = freq[j] = freq[i] + freq[i + 1]
            k = j - 1
            while f < freq[k]:
                k -= 1
            k += 1
            freq[k + 1:j + 1] = freq[k:j]
            freq[k] = f
            son[k + 1:j + 1] = son[k:j]
            son[k] = i
            i += 2
            j += 1
        for i in range(T):
            k = son[i]
            if k >= T:
                prnt[k] = i
            else:
                prnt[k] = prnt[k + 1] = i

    def update(c):
        if freq[R] == MAX_FREQ:
            reconst()
        c = prnt[c + T]
        while True:
            k = freq[c] + 1
            freq[c] = k
            l = c + 1
            if k > freq[l]:
                l += 1
                while k > freq[l]:
                    l += 1
                l -= 1
                freq[c] = freq[l]
                freq[l] = k
                i = son[c]
                prnt[i] = l
                if i < T:
                    prnt[i + 1] = l
                j = son[l]
                son[l] = i
                prnt[j] = c
                if j < T:
                    prnt[j + 1] = c
                son[c] = j
                c = l
            c = prnt[c]
            if c == 0:
                break

    text = bytearray(b" " * (N + F))
    out = bytearray()
    r = N - F
    while len(out) < size:
        c = son[R]
        while c < T:
            c = son[c + get_bit()]
        c -= T
        update(c)
        if c < 256:
            out.append(c)
            text[r] = c
            r = (r + 1) & (N - 1)
        else:
            i = get_byte()
            p = D_CODE[i] << 6
            for _ in range(D_LEN[i] - 2):
                i = (i << 1) + get_bit()
            p |= i & 0x3F
            start = (r - p - 1) & (N - 1)
            for k in range(c - 255 + THRESHOLD):
                b = text[(start + k) & (N - 1)]
                out.append(b)
                text[r] = b
                r = (r + 1) & (N - 1)
    return bytes(out[:size])


# --- архив ---
def chunk(f, want):
    f.seek(0)
    while True:
        head = f.read(8)
        if len(head) < 8:
            return None
        cid, size = struct.unpack("<II", head)
        if cid & ~COMPRESSED == want:
            data = f.read(size)
            return lzhuf_decode(data) if cid & COMPRESSED else data
        f.seek(size, 1)


def file_table(path):
    with open(path, "rb") as f:
        data = chunk(f, 1)
    if data is None:
        sys.exit(f"{path}: нет таблицы файлов (чанк 1)")
    pos = 0
    while pos < len(data):
        size, real, compr, crc = struct.unpack_from("<HIII", data, pos)
        name_len = size - 16
        name = data[pos + 14:pos + 14 + name_len].decode("cp1251")
        ptr = struct.unpack_from("<I", data, pos + 14 + name_len)[0]
        pos += 14 + name_len + 4
        yield name.replace("\\", "/"), ptr, real, compr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("resources")
    ap.add_argument("pattern")
    ap.add_argument("out")
    ap.add_argument("--list", action="store_true", help="только показать подходящие файлы")
    a = ap.parse_args()
    rx = re.compile(a.pattern, re.I)

    archives = sorted(glob.glob(os.path.join(a.resources, "*.db*")) + glob.glob(os.path.join(a.resources, "*.xdb*")))
    found = skipped = 0
    for arch in archives:
        hits = [e for e in file_table(arch) if rx.search(e[0]) and e[2]]
        if not hits:
            continue
        print(f"{os.path.basename(arch)}: {len(hits)}")
        with open(arch, "rb") as f:
            for name, ptr, real, compr in hits:
                found += 1
                if a.list:
                    print("  ", name, real)
                    continue
                if real != compr:
                    skipped += 1  # сжато LZO; для звуков ogg не встречается
                    continue
                dst = os.path.join(a.out, *name.split("/"))
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                f.seek(ptr)
                with open(dst, "wb") as o:
                    o.write(f.read(real))
    print(f"Найдено {found}, пропущено сжатых {skipped} -> {a.out}")


if __name__ == "__main__":
    main()
