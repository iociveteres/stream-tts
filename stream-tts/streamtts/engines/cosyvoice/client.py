"""Клиент cosyvoice-server: настройки модели в каждом запросе и ONNX-фронтенд для голосов из записи."""
import logging

import aiohttp

from ...config import CHARS_PER_SEC
from ...i18n import tr
from ..client import Client as Base

log = logging.getLogger("cosy")

TOKENS_PER_SEC = 25.0     # речевых токенов CosyVoice3 на секунду звука
# Фраза короче полутора чанков стриминга синтезируется целиком: первый звук всё равно приходит почти
# одновременно с последним, а целиком синтез быстрее и громкость выравнивается по LUFS
STREAM_MIN_CHUNKS = 1.5


class Client(Base):
    ENGINE = "cosyvoice"

    def __init__(self, url, settings, lane=None):
        super().__init__(url, settings, lane)
        self.frontend_paths = None   # пути ONNX-фронтенда с сервера (GET /frontend/model); {} — не настроен

    async def _checked(self):
        if self.frontend_paths is None:
            try:
                self.frontend_paths = await self._frontend_paths()
            except Exception as e:   # синтезу это не мешает: повтор при следующей проверке
                log.warning("пути ONNX-фронтенда: %s", e)

    def _lost(self):
        self.frontend_paths = None

    # --- синтез ---
    def params(self, stream):
        """max_token_text_ratio и chunk_tokens. chunk_tokens у потока передаётся всегда: сервер запоминает его из
        прошлого запроса и не сбрасывает."""
        body = dict(mode="zero-shot", response_format="wav")
        if self.s["token_ratio"]:
            body["max_token_text_ratio"] = self.s["token_ratio"]
        if stream:
            body["chunk_tokens"] = int(self.s["chunk_tokens"])
        return body

    def bench_info(self):
        return dict(chunk=int(self.s["chunk_tokens"]))

    def streams(self, text):
        """Фраза короче полутора чанков стриминга — целиком."""
        return len(text) / CHARS_PER_SEC * TOKENS_PER_SEC > STREAM_MIN_CHUNKS * int(self.s["chunk_tokens"])

    # --- ONNX-фронтенд: нужен только для голосов из записи (editor.py), грузится по требованию ---
    async def _frontend_paths(self):
        """Пути из флагов --speech-tokenizer/--campplus: сервер отдаёт их и до загрузки фронтенда."""
        async with self.session.get(f"{self.url}/frontend/model", timeout=aiohttp.ClientTimeout(total=5)) as r:
            if r.status == 404:   # сборка cosyvoice.cpp без этого API
                return {}
            r.raise_for_status()
            d = await r.json()
        return d if d.get("speech_tokenizer") and d.get("campplus") else {}

    @property
    def frontend_loaded(self):
        return bool(self.info.get("frontend_available"))

    @property
    def frontend_ready(self):
        """Фронтенд загружен или его можно загрузить."""
        return self.frontend_loaded or bool(self.frontend_paths)

    async def load_frontend(self):
        """Вызывать под lane.hold: загрузка занимает сервер ~3 с."""
        if self.frontend_loaded:
            return False
        if not self.frontend_paths:
            raise RuntimeError(tr("на сервере синтеза нет фронтенда для создания голосов: скачайте пакет"))
        async with self.session.post(f"{self.url}/frontend/model/load", json=self.frontend_paths,
                                     timeout=aiohttp.ClientTimeout(total=120)) as r:
            if r.status != 200:
                await self._fail(r)
        self.info["frontend_available"] = True
        return True

    async def unload_frontend(self):
        async with self.session.post(f"{self.url}/frontend/model/unload", timeout=aiohttp.ClientTimeout(total=30)) as r:
            if r.status != 200:
                await self._fail(r)
        self.info["frontend_available"] = False
