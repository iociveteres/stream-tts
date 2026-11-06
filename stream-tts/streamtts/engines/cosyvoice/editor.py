"""Редактор голоса (editor.py) у CosyVoice: образец 2–30 с (советуем 10–20, docs/voice-pipeline.md), голос — .gguf.

Голос из образца сервер извлекает ONNX-фронтендом (POST /speaker?type=extract), а держит его выгруженным: фронтенд
грузится перед первым извлечением (пути — из флагов сервера, frontend_paths клиента) и выгружается через
FRONTEND_IDLE простоя."""
import asyncio, logging, os

from ..client import BACKGROUND, PREVIEW
from ...i18n import tr

log = logging.getLogger("editor")

# с: потом ONNX-фронтенд выгружается. Замер (2026-10-18, int8, CPU): ~300 МБ памяти сервера, загрузка ~3 с
FRONTEND_IDLE = 600


class Maker:
    ENGINE = "cosyvoice"
    EXT = ".gguf"
    EXTRACT = True                       # голос из записи делается (у Supertonic — нет)
    SAMPLE_MIN, SAMPLE_MAX = 2.0, 30.0   # с; длиннее синтез медленнее
    SAMPLE_OK = (10, 20)
    PACKAGES = ("frontend", "music")     # пакеты в редакторе: без фронтенда голос не создать

    def __init__(self, tts):
        self.tts = tts
        self._fe_timer = None

    SHARED = ()   # поля черновика, общие для всех вариантов (у ESpeech — темп)

    @staticmethod
    def fields(raw, dur):
        """Свои поля варианта (у CosyVoice нет)."""
        return {}

    @staticmethod
    def shared(raw, srcs):
        """Общие поля черновика из запроса админки (у CosyVoice нет)."""
        return {}

    @staticmethod
    def upgrade(d):
        """Черновик прежнего формата (у CosyVoice не менялся)."""

    def key_parts(self, v):
        return []   # ключ варианта — как до второй модели: синтез старых черновиков не пропадает

    def ref_name(self, voice):
        return "ref-" + voice

    def check(self, v):
        if not self.tts.frontend_ready:
            raise ValueError(tr("на сервере синтеза нет фронтенда для создания голосов: скачайте пакет"))

    def target(self, name, set_name, folder):
        """Куда сервер пишет голос: полный путь в folder (<голоса>/<набор>)."""
        return os.path.join(folder, name + self.EXT)

    async def register(self, name, v, text, wav, editor):
        """Под lane.hold: голос из образца; фронтенд грузится к первому и выгружается после простоя."""
        if await self.tts.load_frontend():
            log.info("ONNX-фронтенд загружен")
        try:
            await self.tts.extract(name, text, wav)
        finally:
            if self._fe_timer:
                self._fe_timer.cancel()
            self._fe_timer = asyncio.get_running_loop().call_later(
                FRONTEND_IDLE, lambda: asyncio.ensure_future(self.unload_frontend(BACKGROUND)))
        return text

    async def unload_frontend(self, priority=PREVIEW):
        """По таймеру простоя и кнопкой на вкладке «Статистика»."""
        if self._fe_timer:
            self._fe_timer.cancel()
            self._fe_timer = None
        try:
            async with self.tts.lane.hold(priority, tr("выгрузка фронтенда")):
                if self.tts.ok is not False and self.tts.frontend_loaded:
                    await self.tts.unload_frontend()
                    log.info("ONNX-фронтенд выгружен")
        except Exception as e:
            if priority != BACKGROUND:
                raise
            log.warning("ONNX-фронтенд не выгружен: %s", e)
