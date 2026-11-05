"""Редактор голоса (editor.py) у ESpeech (docs/espeech.md, «Образец голоса»): голос — .espeech.

- Образец 4–12 с, советуем 6–7 с спокойной речи одной-двумя целыми фразами: F5 копирует манеру образца, поэтому
  кусок выбирается вручную, а длина прямо определяет скорость синтеза (длиннее 12 с сервер не берёт).
- Текст — ровно то, что звучит, с ударениями «+» перед гласной. Без «+» его размечает POST /accent сервера
  (нормализация и RUAccent, как на синтезе), кнопка «Ударения» в админке показывает разметку для правки.
- Темп (кадров мела на байт текста) — общий для всех вариантов голоса (поля черновика, SHARED): необязательный
  отрезок для темпа tempo {src, start, end, text} — обычно длиннее образца, до TEMPO_MAX, с любой записи: короткий
  кусок бывает быстрее или медленнее средней речи голоса; без него — по самому образцу. speed — поправка в процентах
  (±SPEED_MAX, «+» — быстрее): темп делится на 1 + speed/100. Уходит в multipart поле tempo при регистрации; без
  отрезка и поправки поля нет, темп считает сервер. Черновики до общего темпа держали tempo у варианта (upgrade).
- ONNX-фронтенда нет. Сохранение — POST /speaker/save с путём от папки голосов сервера (--voices):
  «<набор>/<имя>.espeech»."""
import io, math, wave

from ...i18n import tr

HOP = 256          # отсчётов на кадр мела ESpeech (espeech/model.py)
TEMPO_MAX = 60.0   # с: отрезок для темпа — только счёт кадров, сервер его не получает
SPEED_MAX = 30     # %: поправка темпа ползунком в админке


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
    EXTRACT = True
    SAMPLE_MIN, SAMPLE_MAX = 4.0, 12.0
    SAMPLE_OK = (6, 7)
    PACKAGES = ("music",)

    def __init__(self, tts):
        self.tts = tts

    SHARED = ("tempo", "speed")   # поля черновика, общие для всех вариантов

    @staticmethod
    def fields(raw, dur):
        """Своих полей варианта нет: темп — общий (shared)."""
        return {}

    @staticmethod
    def shared(raw, srcs):
        """Общие поля из запроса админки: tempo — {src, start, end, text} или None (по образцу), speed — целое."""
        out = {}
        if "tempo" in raw:
            t = raw["tempo"]
            if isinstance(t, dict):
                src = srcs.get(str(t.get("src")))
                if not src:
                    raise ValueError(tr("неверный отрезок для темпа"))
                start, end = max(0.0, float(t.get("start", 0))), min(float(src["dur"]), float(t.get("end", 0)))
                if not (math.isfinite(start) and math.isfinite(end)) or end <= start:
                    raise ValueError(tr("неверный отрезок для темпа"))
                out["tempo"] = dict(src=src["id"], start=round(start, 2), end=round(end, 2),
                                    text=str(t.get("text", ""))[:3000])
            else:
                out["tempo"] = None
        if "speed" in raw:
            s = raw["speed"] or 0
            if not isinstance(s, (int, float)) or not abs(s) <= SPEED_MAX:
                raise ValueError(tr("неверная поправка темпа"))
            out["speed"] = round(s) or None
        return out

    @staticmethod
    def upgrade(d):
        """Черновик до общего темпа: отрезок первого варианта, у которого он был, — общий."""
        for v in d["variants"]:
            t = v.pop("tempo", None)
            if isinstance(t, dict) and not d.get("tempo"):
                d["tempo"] = dict(t, src=v["src"])

    def key_parts(self, v):
        t, speed = v.get("tempo"), v.get("speed")
        parts = [self.ENGINE, [t["src"], t["start"], t["end"], " ".join(t["text"].split())] if t else None]
        return parts + [speed] if speed else parts   # без поправки ключ прежний

    def ref_name(self, voice):
        return f"ref-{voice}~{self.ENGINE}"   # «~» в имени голоса не бывает (voices.NAME)

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

    async def tempo(self, v, text, wav, editor):
        """Кадров на байт с поправкой speed или None: по самому образцу (считает сервер)."""
        t, speed = v.get("tempo"), v.get("speed") or 0
        if t:
            rate = tempo_of(len(editor._cut(t["src"], t["start"], t["end"])), await self.accented(t["text"]))
        elif speed:
            with wave.open(io.BytesIO(wav)) as w:
                rate = tempo_of(w.getnframes(), text)
        else:
            return None
        return rate / (1 + speed / 100)

    async def register(self, name, v, text, wav, editor):
        """Под lane.hold: голос из образца в памяти сервера. → текст образца с ударениями."""
        text = await self.accented(text)
        tempo = await self.tempo(v, text, wav, editor)
        await self.tts.extract(name, text, wav, tempo=f"{tempo:.6f}" if tempo else None)
        return text
