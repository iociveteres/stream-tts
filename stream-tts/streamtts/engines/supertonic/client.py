"""Клиент supertonic-server (supertonic/server.py): число шагов синтеза и скорость речи в каждом запросе. Голоса из
записи сервер не делает (capabilities.extract = false): POST /speaker?type=extract и /speaker/save — 501.

Поток сервер отдаёт сам по кускам текста (chunked, первое предложение — отдельным куском). Короткая фраза — одно-два
предложения: первый кусок — почти вся фраза, а целиком она ещё и выравнивается по LUFS, поэтому, как у ESpeech
(engines/espeech/client.py), такая фраза синтезируется целиком. Порог выше, чем у ESpeech: Supertonic на процессоре в разы быстрее реального времени, и ожидание целой фразы
короче порога — доли секунды (оценка по заявленной скорости модели; замер на своём процессоре — «Скорость синтеза»
на вкладке «Сервер»)."""
from ...config import CHARS_PER_SEC
from ..client import Client as Base

STREAM_MIN_SEC = 10.0   # расчётного звука (12 символов в секунду): короче — целиком


class Client(Base):
    ENGINE = "supertonic"
    SETTINGS = {"total_step": "supertonic_steps", "speed": "supertonic_speed"}
    # Как в PARAMS сервера: схема, пока он не ответил (строки переводятся при показе, static/en.json)
    DEFAULT_PARAMS = {
        "total_step": {"type": "choice", "choices": [5, 8, 12, 16], "default": 8, "title": "Качество синтеза",
                       "help": "Шагов модели на фразу: 5 — быстрее всего; 8 — оптимально; 12 и 16 — чуть чище звук, "
                               "но синтез в полтора-два раза дольше"},
        "speed": {"type": "choice", "choices": [0.9, 1.0, 1.05, 1.15, 1.3], "default": 1.05, "title": "Скорость речи",
                  "help": "Темп речи: 1 — обычный, меньше — медленнее, больше — быстрее; 1,05 — как у авторов "
                          "модели"},
    }

    def params(self, stream):
        return dict(total_step=int(self.s["supertonic_steps"]), speed=float(self.s["supertonic_speed"]))

    def streams(self, text):
        return len(text) / CHARS_PER_SEC > STREAM_MIN_SEC

    def bench_info(self):
        """Скорость синтеза зависит от числа шагов; скорость речи меняет длину звука, а не RTF."""
        return dict(steps=int(self.s["supertonic_steps"]))
