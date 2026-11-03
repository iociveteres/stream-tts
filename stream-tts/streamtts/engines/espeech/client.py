"""Клиент espeech-server (espeech/server.py): число шагов синтеза в каждом запросе, «как прочитает» (/accent),
словарь ударений пользователя (/accents, capabilities.user_accents), обучение LoRA (/train*, adapter в /tts,
capabilities.train — страница обучения, train.py).

Поток сервер отдаёт сам по кускам (chunked): фраза режется на куски по запасу воспроизведения (первый от 1,5 с звука,
следующие от 2,5 с). Короткая фраза приходит одним куском — первый звук тот же, что целиком, а громкость потока
выставляется заранее по примеру голоса и гуляет сильнее (замер на wc3-jaina: −12,9…−14,4 LUFS против −16), поэтому
она синтезируется целиком и выравнивается по LUFS."""
import urllib.parse

import aiohttp

from ... import audio
from ...config import CHARS_PER_SEC
from ..client import Client as Base

STREAM_MIN_SEC = 6.0   # расчётного звука (12 символов в секунду): короче — целиком


class Client(Base):
    ENGINE = "espeech"
    SETTINGS = {"nfe_steps": "espeech_steps"}
    # Как в PARAMS сервера: схема, пока он не ответил (строки переводятся при показе, static/en.json)
    DEFAULT_PARAMS = {"nfe_steps": {"type": "choice", "choices": [12, 16, 32], "default": 16, "title": "Качество синтеза",
                                    "help": "12 — быстрее, но заметно теряется качество; 16 — оптимально; "
                                            "32 — наибольшее качество, но вдвое медленнее"}}

    def params(self, stream):
        return dict(nfe_steps=int(self.s["espeech_steps"]))

    def streams(self, text):
        return len(text) / CHARS_PER_SEC > STREAM_MIN_SEC

    def bench_info(self):
        return dict(steps=int(self.s["espeech_steps"]))

    # --- словарь ударений пользователя: сервер размечает текст в потоке запроса, очередь к нему не нужна ---
    async def user_accents(self):
        """[{word, accented}]"""
        async with self.session.get(f"{self.url}/accents", timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200:
                await self._fail(r)
            return (await r.json())["words"]

    async def put_user_accent(self, accented):
        """Слово с ударением («з+амок»): добавить или заменить. ValueError — сервер не принял слово."""
        async with self.session.post(f"{self.url}/accents", json=dict(accented=accented),
                                     timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status == 400:
                raise ValueError(await r.text())
            if r.status != 200:
                await self._fail(r)
            return await r.json()

    async def delete_user_accent(self, word):
        async with self.session.delete(f"{self.url}/accents/{urllib.parse.quote(word)}",
                                       timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status not in (200, 404):
                await self._fail(r)
            return r.status == 200

    # --- обучение LoRA (capabilities.train, engines/espeech/train.py): сервер отвечает и во время шага ---
    async def _train(self, method, path, timeout=15, **kw):
        """→ JSON ответа; TrainError — отказ сервера ({error, message}), остальное — RuntimeError."""
        async with self.session.request(method, f"{self.url}{path}", timeout=aiohttp.ClientTimeout(total=timeout),
                                        **kw) as r:
            if r.status == 200:
                return await r.json()
            try:
                d = await r.json(content_type=None)
            except ValueError:
                d = None
            if isinstance(d, dict) and d.get("error"):
                raise TrainError(r.status, d["error"], str(d.get("message") or d["error"]))
            await self._fail(r)

    async def train_status(self, timeout=10):
        return await self._train("GET", "/train", timeout)

    async def train_start(self, form):
        """multipart (aiohttp.FormData): старт — подготовка мела и проверка на шаге 0 в рабочем потоке сервера."""
        return await self._train("POST", "/train", 600, data=form)

    async def train_stop(self):
        return await self._train("POST", "/train/stop", 60)

    async def train_save(self, snapshot):
        return await self._train("POST", "/train/save", 60, json=dict(snapshot=snapshot))

    async def train_discard(self):
        return await self._train("POST", "/train/discard", 60)

    async def synth_adapter(self, voice, text, adapter, seed=None):
        """Целиком со снимком обучения (adapter: last / best) или без адаптера голоса (none)."""
        body = dict(self._body(voice, text, False, seed), adapter=adapter)
        async with self.session.post(f"{self.url}/tts", json=body, timeout=aiohttp.ClientTimeout(total=300)) as r:
            if r.status == 409:   # снимка нет: {error: no_snapshot, message}
                raise TrainError(409, "no_snapshot", (await r.text())[:200])
            if r.status != 200:
                await self._fail(r)
            return audio.read_wav(await r.read())


class TrainError(Exception):
    """Отказ espeech-server в запросе обучения: HTTP-статус, код (busy, unsaved, oom…) и его текст (по-русски)."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code = status, code
