"""Текст для Supertonic: знаки ударения «+» убираются (Supertonic их не понимает, а разметку «з+амок» stream-tts
хранит для ESpeech), русский — нормализатором espeech-server (espeech/normalize.py: числа, латиница, ники, смайлы
и сленг чата словами кириллицей). Нормализатор — чистый Python (re, unicodedata; числительные свои), импорт
espeech/ не тянет torch и RUAccent: espeech/__init__.py лишь подключает папки ESPEECH_DLL_DIRS, если они заданы.
Другие языки — как есть: подготовку текста модели (helper.py) делает model.preprocess."""
from espeech.accent import unmark
from espeech.normalize import normalize


def prepare(text, lang):
    text = unmark(text)
    return normalize(text) if lang == "ru" else text.strip()
