"""espeech-server без модели: нарезка фразы на куски, длительность, нормализатор, словарь ударений пользователя,
ударения, уже стоящие в тексте,
подсказка в логе о причине недоступной видеокарты. Запуск: espeech/.venv/Scripts/python -m pytest
tests/espeech/test_text.py (секунды)."""
import os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from espeech import model as M, normalize, pipeline as P  # noqa: E402
from espeech.accent import UserAccents, keep_marked, unmark  # noqa: E402
from espeech.engine import cuda_note  # noqa: E402

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


def test_gen_text_space_after_final_sign():
    # как chunk_text f5-tts: после однобайтного знака — пробел (иначе модель глотает конечную согласную)
    assert P.gen_text("Спас+ибо за подп+иску, др+уг!") == "Спас+ибо за подп+иску, др+уг! "
    assert P.gen_text("Пр+ивет") == "Пр+ивет"
    assert P.gen_text("") == ""


def test_voice_ref_padded_with_silence():
    """Образец, оборванный на звуке, давал щелчок в начале каждого куска: мел образца получает 50 мс тишины
    (как в f5-tts), а темп и длина образца считаются без неё."""
    import numpy as np
    from espeech import voices as V

    class FakeTTS:
        def cond(self, wav):
            self.wav = wav
            return None, 0.1

    tts, ref = FakeTTS(), np.full(M.SR * 6, 0.5, np.float32)
    v = V.Voice("x", tts, ref, dict(text="Д+а."), path="x.espeech")
    pad = int(V.REF_PAD * M.SR)
    assert len(tts.wav) == len(ref) + pad and not tts.wav[-pad:].any() and tts.wav[0] == 0.5
    assert v.ref_len == (len(ref) + pad) // M.HOP
    assert v.ref_sec == 6.0 and v.rate == V.tempo_of(len(ref), "Д+а.")


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


def test_marks_in_text_kept():
    """Ударения в тексте: до normalize снимаются («з+амок» не становится «з плюс амок»), после разметки модели
    возвращаются в свои слова; «+25», «18+», «C++» — не ударения."""
    t = "Привет, з+амок на ГОР+Е! +25 градусов, 18+, C++ и +оба"
    assert unmark(t) == "Привет, замок на ГОРЕ! +25 градусов, 18+, C++ и оба"
    assert "плюс амок" not in normalize.normalize(unmark(t))
    model = "прив+ет, зам+ок на гор+е! плюс двадцать пять гр+адусов, восемнадцать плюс, си и об+а"
    assert keep_marked(t, model) == ("прив+ет, з+амок на гор+е! плюс двадцать пять гр+адусов, восемнадцать плюс, "
                                     "си и +оба")
    assert keep_marked("Зам+ок", "З+амок") == "Зам+ок" and keep_marked("замок", "з+амок") == "з+амок"
