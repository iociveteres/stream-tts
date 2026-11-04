"""Файл по HTTP для загрузок из админки (install.py, engines/espeech/download.py): пишется в <имя>.part с докачкой
(Range) и повторами, сверяется с sha256 (у LFS-файлов Hugging Face — из x-linked-etag) и только потом
переименовывается. Прогресс — в progress.dl (Installer.dl), progress._push() будит админку."""
import asyncio, hashlib, logging, os, re, time

import aiohttp

from .i18n import tr

log = logging.getLogger("install")
CHUNK = 1 << 20
RETRIES = 5


async def head(session, url, rel):
    """Размер и sha256 файла: у LFS-файлов Hugging Face отдаёт их в x-linked-size и x-linked-etag до редиректа."""
    async with session.head(url, allow_redirects=False) as r:
        if r.status >= 400:
            raise RuntimeError(f"{rel}: HTTP {r.status}")
        size = int(r.headers.get("x-linked-size") or r.headers.get("content-length") or 0)
        sha = r.headers.get("x-linked-etag", "").strip('"')
        return size, sha if re.fullmatch(r"[0-9a-f]{64}", sha) else ""


async def fetch(progress, session, url, rel, dst, size, sha):
    """url → dst; rel — имя в прогрессе, size и sha — для проверки (0 и "" — не проверять)."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    part = dst + ".part"
    progress.dl["file"] = rel
    for attempt in range(1, RETRIES + 1):
        have = part_size(dst)
        if size and have > size:
            os.remove(part)
            progress.dl["done"] -= have
            have = 0
        if size and have == size:
            break
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            async with session.get(url, headers=headers) as r:
                if r.status == 200 and have:  # сервер не умеет Range: заново
                    progress.dl["done"] -= have
                    have = 0
                elif r.status not in (200, 206):
                    raise RuntimeError(f"HTTP {r.status}")
                t0, got0 = time.monotonic(), progress.dl["done"]
                with open(part, "ab" if have else "wb") as f:
                    async for chunk in r.content.iter_chunked(CHUNK):
                        f.write(chunk)
                        progress.dl["done"] += len(chunk)
                        dt = time.monotonic() - t0
                        if dt > 1:
                            progress.dl["speed"] = (progress.dl["done"] - got0) / dt
                        progress._push()
            if not size or part_size(dst) == size:
                break
        except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
            if attempt == RETRIES:
                raise RuntimeError(f"{rel}: {e or type(e).__name__}") from e
            log.warning("%s: %s, повтор %d через 3 с", rel, e or type(e).__name__, attempt)
            await asyncio.sleep(3)
    if sha:
        progress.dl.update(file=tr("{file}: проверка", file=rel), speed=0.0)
        progress._push(force=True)
        got = await asyncio.to_thread(sha256, part)
        if got != sha:
            os.remove(part)
            raise RuntimeError(tr("{file}: контрольная сумма не совпала, файл удалён — скачайте ещё раз", file=rel))
    os.replace(part, dst)


def part_size(dst):
    try:
        return os.path.getsize(dst + ".part")
    except OSError:
        return 0


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(CHUNK):
            h.update(b)
    return h.hexdigest()
