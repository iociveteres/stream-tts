"""Клиент espeech-server (espeech/server.py): число шагов синтеза в каждом запросе, «как прочитает» (/accent),
словарь ударений пользователя (/accents, capabilities.user_accents).

Поток сервер отдаёт сам по кускам (chunked): фраза режется на куски по запасу воспроизведения (первый от 1,5 с звука,
следующие от 2,5 с). Короткая фраза приходит одним куском — первый звук тот же, что целиком, а громкость потока
выставляется заранее по примеру голоса и гуляет сильнее (замер на wc3-jaina: −12,9…−14,4 LUFS против −16), поэтому
она синтезируется целиком и выравнивается по LUFS."""
import urllib.parse

import aiohttp

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
