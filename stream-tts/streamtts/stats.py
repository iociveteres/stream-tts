"""Статистика: история активаций из SQLite и загрузка GPU (nvidia-smi)."""
import asyncio, shutil, time


class GPU:
    """nvidia-smi есть только при пробросе видеокарты в контейнер (docker-compose.gpu.yml)."""

    def __init__(self):
        self.data = None

    async def poll(self):
        exe = shutil.which("nvidia-smi")
        if not exe:
            self.data = None
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 5)
            name, util, used, total, temp = [x.strip() for x in out.decode().splitlines()[0].split(",")]
            self.data = dict(name=name, util=int(util), mem_used=int(used), mem_total=int(total), temp=int(temp))
        except Exception:
            self.data = None


def period(db, hours, include_test=False):
    since = time.time() - hours * 3600
    where = "ts >= ?" + ("" if include_test else " AND test = 0")
    q = lambda sql, *a: [dict(r) for r in db.q(sql, since, *a)]
    total = q(f"SELECT COUNT(*) n, SUM(outcome='played') played, SUM(outcome!='played') refunded FROM events WHERE {where}")[0]
    by_hour = q(f"SELECT CAST(ts/3600 AS INTEGER)*3600 hour, SUM(outcome='played') played, "
                f"SUM(outcome!='played') refunded FROM events WHERE {where} GROUP BY hour ORDER BY hour")
    voices = q(f"SELECT voice, COUNT(*) n FROM events WHERE {where} AND outcome='played' "
               f"GROUP BY voice ORDER BY n DESC LIMIT 10")
    viewers = q(f"SELECT login, COUNT(*) n FROM events WHERE {where} GROUP BY login ORDER BY n DESC LIMIT 10")
    # source появился позже chat и test: у старых записей способ восстанавливается по ним
    sources = q(f"SELECT COALESCE(source, CASE WHEN test=1 THEN 'test' WHEN chat=1 THEN 'chat' ELSE 'reward' END) "
                f"source, COUNT(*) n FROM events WHERE {where} AND outcome='played' GROUP BY 1 ORDER BY n DESC")
    reasons = q(f"SELECT reason, COUNT(*) n FROM events WHERE {where} AND outcome!='played' "
                f"GROUP BY reason ORDER BY n DESC")
    timing = q(f"SELECT AVG(wait_ms) wait_ms, SUM(synth_ms) synth_ms, SUM(audio_ms) audio_ms, AVG(audio_ms) avg_audio "
               f"FROM events WHERE {where} AND outcome='played'")[0]
    ratio = (timing["synth_ms"] / timing["audio_ms"]) if timing["audio_ms"] else None
    return dict(hours=hours, since=since, total=total, by_hour=by_hour, voices=voices, viewers=viewers,
                sources=sources, reasons=reasons, wait_ms=timing["wait_ms"], avg_audio_ms=timing["avg_audio"], synth_ratio=ratio)
