"""Редактор голоса (editor.py) у ESpeech (docs/espeech.md, «Образец голоса»): голос — .espeech.

- Образец 4–12 с, советуем 6–7 с спокойной речи одной-двумя целыми фразами: F5 копирует манеру образца, поэтому
  кусок выбирается вручную, а длина прямо определяет скорость синтеза (длиннее 12 с сервер не берёт).
- Текст — ровно то, что звучит, с ударениями «+» перед гласной. Без «+» его размечает POST /accent сервера
  (нормализация и RUAccent, как на синтезе), кнопка «Ударения» в админке показывает разметку для правки.
- Темп (кадров мела на байт текста) — по необязательному отрезку для темпа (поле tempo варианта: start, end, text —
  обычно длиннее образца, до TEMPO_MAX): короткий кусок бывает быстрее или медленнее средней речи голоса. Без него —
  по самому образцу (так считает сервер). Уходит в multipart поле tempo при регистрации.
- ONNX-фронтенда нет. Сохранение — POST /speaker/save с путём от папки голосов сервера (--voices):
  «<набор>/<имя>.espeech»."""
import math

from ...i18n import tr

HOP = 256          # отсчётов на кадр мела ESpeech (espeech/model.py)
TEMPO_MAX = 60.0   # с: отрезок для темпа — только счёт кадров, сервер его не получает


def ref_text(text):
    """Текст образца как у f5-tts preprocess_ref_audio_text (espeech/voices.py): в конце «. »."""
    text = text.strip()
    return text + " " if text.endswith(".") else text + ". "


def tempo_of(samples, accented):
    """Кадров на байт UTF-8 (с «+» и «. » в конце), как voices.tempo_of сервера."""
    return (samples // HOP) / len(ref_text(accented).encode())


class Maker:
    ENGINE = "espeech"
    EXT = ".espeech"
    SAMPLE_MIN, SAMPLE_MAX = 4.0, 12.0
    SAMPLE_OK = (6, 7)
    PACKAGES = ("music",)

    def __init__(self, tts):
        self.tts = tts

    @staticmethod
    def fields(raw, dur):
        """tempo варианта: {start, end, text} или нет поля."""
        t = raw.get("tempo")
        if not isinstance(t, dict):
            return {}
        start, end = max(0.0, float(t.get("start", 0))), min(float(dur), float(t.get("end", 0)))
        if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
            raise ValueError(tr("неверный отрезок для темпа"))
        return dict(tempo=dict(start=round(start, 2), end=round(end, 2), text=str(t.get("text", ""))[:3000]))

    def key_parts(self, v):
        t = v.get("tempo")
        return [self.ENGINE, [t["start"], t["end"], " ".join(t["text"].split())] if t else None]

    def ref_name(self, voice):
        return f"ref-{voice}~{self.ENGINE}"   # «~» в имени голоса не бывает (editor.NAME)

    def check(self, v):
        t = v.get("tempo")
        if t:
            if t["end"] - t["start"] > TEMPO_MAX:
                raise ValueError(tr("отрезок для темпа длиннее {n} с", n=int(TEMPO_MAX)))
            if not t["text"].strip():
                raise ValueError(tr("нет расшифровки отрезка для темпа"))

    def target(self, name, set_name, folder):
        return f"{set_name}/{name}{self.EXT}"

    async def accented(self, text):
        """Текст как его прочитает модель: с «+» — как есть, без — нормализация и ударения сервера."""
        text = " ".join(text.split())
        return text if "+" in text else (await self.tts.accent(text))["accented"]

    async def tempo(self, v, editor):
        t = v.get("tempo")
        if not t:
            return None
        n = len(editor._cut(v["src"], t["start"], t["end"]))
        return tempo_of(n, await self.accented(t["text"]))

    async def register(self, name, v, text, wav, editor):
        """Под lane.hold: голос из образца в памяти сервера. → текст образца с ударениями."""
        text = await self.accented(text)
        tempo = await self.tempo(v, editor)
        await self.tts.extract(name, text, wav, tempo=f"{tempo:.6f}" if tempo else None)
        return text
