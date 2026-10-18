"""Статистика: история активаций из SQLite и загрузка машины: процессор и память (Windows API или /proc),
видеокарта (nvidia-smi)."""
import asyncio, collections, os, shutil, time

HISTORY = 300   # точек: 10 минут при опросе раз в 2 с (__main__.load_monitor), только пока открыта админка


class _CPU:
    """Загрузка процессора между двумя вызовами и память, без psutil. В контейнере Docker — вся виртуальная машина
    WSL2, а не только контейнер."""

    def __init__(self):
        self.prev = None

    def _times(self):
        """(простой, всего) в любых единицах."""
        if os.name == "nt":
            import ctypes
            idle, kernel, user = (ctypes.c_ulonglong() for _ in range(3))   # FILETIME: два DWORD подряд
            ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
            return idle.value, kernel.value + user.value   # время ядра включает простой
        with open("/proc/stat") as f:
            v = [int(x) for x in f.readline().split()[1:]]
        return v[3] + v[4], sum(v[:8])                     # idle + iowait; guest уже входит в user

    def _memory(self):
        """(занято, всего) в МБ."""
        if os.name == "nt":
            import ctypes

            class Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_uint32), ("dwMemoryLoad", ctypes.c_uint32)] + \
                    [(n, ctypes.c_uint64) for n in ("total", "avail", "pf_total", "pf_avail", "virt_total",
                                                    "virt_avail", "ext_avail")]
            st = Status()
            st.dwLength = ctypes.sizeof(st)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return (st.total - st.avail) >> 20, st.total >> 20
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
        return (info["MemTotal"] - info["MemAvailable"]) >> 10, info["MemTotal"] >> 10

    def poll(self):
        idle, total = self._times()
        util = None
        if self.prev and total > self.prev[1]:
            util = round(100 * (1 - (idle - self.prev[0]) / (total - self.prev[1])))
        self.prev = idle, total
        used, mem_total = self._memory()
        return dict(util=util, cores=os.cpu_count(), mem_used=used, mem_total=mem_total)


class Load:
    """Текущая загрузка и история за последние минуты (график на вкладке «Статистика»).

    nvidia-smi в Docker есть только при пробросе видеокарты в контейнер (docker-compose.gpu.yml)."""

    def __init__(self):
        self.cpu_probe = _CPU()
        self.cpu = None
        self.gpu = None
        self.history = collections.deque(maxlen=HISTORY)   # [время, ЦП %, ГП %, видеопамять %]

    async def poll(self):
        try:
            self.cpu = self.cpu_probe.poll()
        except Exception:
            self.cpu = None
        self.gpu = await self._gpu()
        c, g = self.cpu or {}, self.gpu or {}
        if c.get("util") is not None or g:
            self.history.append([round(time.time(), 1), c.get("util"), g.get("util"),
                                 round(100 * g["mem_used"] / g["mem_total"]) if g.get("mem_total") else None])

    async def _gpu(self):
        exe = shutil.which("nvidia-smi")
        if not exe:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                exe, "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                "--format=csv,noheader,nounits", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), 5)
            name, util, used, total, temp = [x.strip() for x in out.decode().splitlines()[0].split(",")]
            return dict(name=name, util=int(util), mem_used=int(used), mem_total=int(total), temp=int(temp))
        except Exception:
            return None

    def public(self, history=False):
        out = dict(cpu=self.cpu, gpu=self.gpu, sample=self.history[-1] if self.history else None)
        if history:
            out["history"] = list(self.history)
        return out


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
