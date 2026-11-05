"""Редактор голоса (editor.py) у Supertonic: голоса из записи нет — у модели только готовые голоса, своего
клонирования нет (capabilities.extract = false). Черновик общий для всех моделей и остаётся, но синтез и сохранение
вариантов отказывают с понятной причиной, а админка вместо редактора показывает подсказку (editor.js)."""
from ...i18n import tr


class Maker:
    ENGINE = "supertonic"
    EXT = ".supertonic"
    EXTRACT = False                      # голос из записи не делается (editor.js показывает отказ)
    SAMPLE_MIN, SAMPLE_MAX = 2.0, 30.0   # с: пределы не применяются, но черновик показывает записи
    SAMPLE_OK = (10, 20)
    PACKAGES = ()
    SHARED = ()

    def __init__(self, tts):
        self.tts = tts

    @staticmethod
    def fields(raw, dur):
        return {}

    @staticmethod
    def shared(raw, srcs):
        return {}

    @staticmethod
    def upgrade(d):
        pass

    def key_parts(self, v):
        return [self.ENGINE]

    def ref_name(self, voice):
        return f"ref-{voice}~{self.ENGINE}"   # «~» в имени голоса не бывает (voices.NAME)

    def check(self, v):
        raise ValueError(refusal(self.tts.title))

    def target(self, name, set_name, folder):
        return f"{set_name}/{name}{self.EXT}"

    async def register(self, name, v, text, wav, editor):
        raise ValueError(refusal(self.tts.title))


def refusal(title):
    return tr("модель {engine} не делает голос из записи: у неё только готовые голоса. Чтобы сделать голос, "
              "выберите другую модель на вкладке «Сервер»", engine=title)
