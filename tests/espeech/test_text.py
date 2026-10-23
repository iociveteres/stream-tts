"""espeech-server без модели: нарезка фразы на куски, длительность, нормализатор, словарь ударений пользователя.
Запуск: espeech/.venv/Scripts/python -m pytest tests/espeech/test_text.py (секунды)."""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from espeech import model as M, normalize, pipeline as P  # noqa: E402
from espeech.accent import UserAccents  # noqa: E402

BPS = 24.0   # байт на секунду звука — темп медленного голоса (Артес ~22–24)


def test_chains_joined_with_commas():
    assert P.join_chains(["Пять!", "Четыре!", "Три!", "Пуск!"]) == ["Пять, четыре, три, пуск!"]
    assert P.join_chains(["Эй!", "Ты!", "Иди сюда, быстро."]) == ["Эй, ты!", "Иди сюда, быстро."]
    # одиночное короткое предложение — как есть
    assert P.join_chains(["Ого.", "Вот это поворот событий."]) == ["Ого.", "Вот это поворот событий."]


def test_plan_first_chunk_short_next_grow():
    text = ("Спасибо за подписку, друг! Сегодня мы пройдём кампанию нежити до конца. Если повезёт, то и бонусные "
            "миссии успеем. А потом ещё немного поиграем в доту, если чат не будет против такого плана.")
    chunks = P.chunks(text, BPS)
    assert len(chunks) >= 2
    assert " ".join(chunks) == text   # ничего не теряется и не переставляется
    first = len(chunks[0].encode()) / BPS
    assert first < P.FIRST_MAX_SEC
    assert all(len(c.encode()) / BPS <= P.MAX_SEC for c in chunks)


def test_long_first_sentence_split_by_commas():
    text = ("Слушайте все, кто сегодня пришёл на стрим, у меня для вас важное объявление, касающееся розыгрыша, "
            "который пройдёт в субботу вечером.")
    chunks = P.chunks(text, BPS)
    assert len(chunks[0].encode()) / BPS <= P.FIRST_MAX_SEC
    assert " ".join(chunks) == text


def test_frames_short_minimum_and_punct():
    rate = M.FPS / BPS
    assert P.frames("Д+а.", rate) == int(1.4 * M.FPS)
    # короткий со знаком внутри — +0,5 с за знак
    assert P.frames("Отв+ет: д+а.", rate) == int((1.4 + 0.5) * M.FPS)
    long = "Сег+одня м+ы пройдём камп+анию н+ежити до конц+а, ес+ли повезёт. И б+онусные м+иссии усп+еем."
    base = rate * len(long.encode())
    assert P.frames(long, rate) == int(base + 0.15 * M.FPS + 0.3 * M.FPS)


def test_trim_cuts_silence():
    import numpy as np
    y = np.concatenate([np.zeros(M.SR // 2), 0.3 * np.sin(np.arange(M.SR) / 10), np.zeros(M.SR // 2)])
    t = P.trim(y.astype(np.float32))
    assert M.SR <= len(t) < M.SR * 1.1


def test_normalize_output_alphabet():
    out = normalize.normalize("Спасибо за 500р, xXx_Killer_2007!!! :) Стрим в 18:30")
    assert "пятьсот" in out and "киллер" in out.lower() and "восемнадцать" in out
    assert not set(out) - set("абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ .,!?:;—-")


def test_user_accents(tmp_path):
    path = str(tmp_path / "accents.json")
    u = UserAccents(path)
    assert u.put("З+АМОК") == ("замок", "з+амок")
    u.put("+ежик")
    u.put("вс+ё")
    # поверх готовой разметки: регистр и «ё» — из текста, «ё» в тексте находит слово с «е»
    assert u.apply("Зам+ок и ЗАМ+ОК, зам+ок-то.") == "З+амок и З+АМОК, зам+ок-то."
    assert u.apply("Ёжик ушёл.") == "+Ёжик ушёл."
    assert u.apply("Вс+е пришли.") == "Вс+е пришли."   # «всё» из словаря — только где RUAccent прочитал «всё»
    assert u.apply("Это вс+ё.") == "Это вс+ё."
    for bad in ("замок", "zam+ok", "два сл+ова", "з+", "з++амок", "+"):
        try:
            u.parse(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    assert UserAccents(path).items() == u.items()   # файл переживает перезапуск
    assert u.delete("ЗАМОК") and not u.delete("замок")
    assert "замок" not in dict(UserAccents(path).items())
    assert UserAccents().apply("Зам+ок.") == "Зам+ок."
