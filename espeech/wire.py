"""Формат обмена espeech-server: ошибка HTTP со статусом, разбор multipart/form-data, звук PCM16 и заголовок WAV
(в том числе с неизвестной длиной — для потока, как у cosyvoice-server)."""
import re, struct, urllib.parse

import numpy as np


class HTTPError(Exception):
    def __init__(self, status, text):
        super().__init__(text)
        self.status = status


def multipart(ctype, body):
    """multipart/form-data → [(имя, имя файла или None, bytes)] по порядку (поля могут повторяться)."""
    m = re.search(r'boundary="?([^";]+)"?', ctype)
    if not ctype.startswith("multipart/form-data") or not m:
        raise HTTPError(400, "multipart/form-data expected")
    out = []
    for part in (b"\r\n" + body).split(b"\r\n--" + m.group(1).encode())[1:]:
        if part.startswith(b"--"):
            break
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        head = head.decode("utf-8", "replace")
        name = re.search(r'(?i)content-disposition:[^\r\n]*?\bname="([^"]*)"', head)
        fn = re.search(r'(?i)\bfilename="([^"]*)"', head)
        fn2 = re.search(r"(?i)\bfilename\*=utf-8''([^;\r\n]+)", head)
        if name:
            out.append((name.group(1), urllib.parse.unquote(fn2.group(1)) if fn2 else fn and fn.group(1), data))
    return out


def pcm16(x):
    return (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()


def wav_header(n_bytes=None, sr=24000):
    """WAV PCM16 моно; n_bytes=None — длина неизвестна (поток, как у cosyvoice-server)."""
    data = 0xFFFFFFFF if n_bytes is None else n_bytes
    riff = 0xFFFFFFFF if n_bytes is None else 36 + n_bytes
    return (b"RIFF" + struct.pack("<I", riff) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, sr, sr * 2, 2, 16) +
            b"data" + struct.pack("<I", data))
