"""Клиент espeech-server (espeech/server.py): число шагов синтеза в каждом запросе, «как прочитает» (/accent).

Поток сервер отдаёт сам по кускам (chunked): фраза режется на куски по запасу воспроизведения (первый от 1,5 с звука,
следующие от 2,5 с). Короткая фраза приходит одним куском — первый звук тот же, что целиком, а громкость потока
выставляется заранее по примеру голоса и гуляет сильнее (замер на wc3-jaina: −12,9…−14,4 LUFS против −16), поэтому
она синтезируется целиком и выравнивается по LUFS."""
from ...config import CHARS_PER_SEC
from ..client import Client as Base

STREAM_MIN_SEC = 6.0   # расчётного звука (12 символов в секунду): короче — целиком


class Client(Base):
    ENGINE = "espeech"
    SETTINGS = {"nfe_steps": "espeech_steps"}
    # Как в PARAMS сервера: схема, пока он не ответил (строки переводятся при показе, static/en.json)
    DEFAULT_PARAMS = {"nfe_steps": {"type": "choice", "choices": [16, 32, 12], "default": 16, "title": "Шагов синтеза",
                                    "help": "16 — быстро и почти как 32; 32 — максимальное качество, вдвое медленнее; "
                                            "12 — быстрее, звучание заметно другое"}}

    def params(self, stream):
        return dict(nfe_steps=int(self.s["espeech_steps"]))

    def streams(self, text):
        return len(text) / CHARS_PER_SEC > STREAM_MIN_SEC

    def bench_info(self):
        return dict(steps=int(self.s["espeech_steps"]))
