"""Загрузка Supertonic 3 по кнопке на вкладке «Загрузки» (программа для Windows, Docker и исходники — одним кодом) и
готовые голоса модели.

Что и откуда — downloads.json, раздел supertonic: файлы репозитория Hugging Face Supertone/supertonic-3 закреплённой
ревизии с размером и sha256 (таблица в docs/supertonic.md), адрес — <url>/<путь>. Кладутся в paths.MODELS/supertonic
в раскладке репозитория (её и ждёт supertonic-server); каждый файл — с докачкой и проверкой (fetch.py), уже скачанный
(тот же размер) не качается. LICENSE серверу не нужен, но лежит рядом с весами: OpenRAIL-M требует передавать условия
вместе с моделью, а админка показывает их до загрузки. В установщик и образ веса не кладутся (docs/supertonic.md,
«Лицензии»).

Готовые голоса: после загрузки 10 стилей voice_styles/*.json ставятся набором supertonic —
paths.VOICES/supertonic/<имя>.supertonic (файл — JSON стиля как есть) и manifest.json набора (группа на странице
голосов). Удаление модели убирает и эти голоса; голоса пользователя в той же папке остаются.

STREAM_TTS_TEST_SUPERTONIC — только тестам (tests/e2e/test_supertonic_download.py): JSON вместо раздела
downloads.json, файлы с локального HTTP-сервера."""
import contextlib, functools, json, os, shutil

from ... import fetch, paths, store
from ...i18n import tr

SET = "supertonic"   # набор готовых голосов: paths.VOICES/supertonic, группа «Supertonic 3» на странице голосов
EXT = ".supertonic"
# Готовые голоса: стиль модели → имя голоса. Зритель выбирает голос командой «!ttssf <имя>» в чате, а «m1» и «f3»
# ему ничего не говорят и легко путаются. Поэтому — короткие русские имена латиницей (voices.NAME: только ASCII):
# пол слышен по имени, и их легко набрать с телефона. Внутри пола — по алфавиту в порядке номеров стиля (M1 — anton,
# F1 — anna), так что к стилю модели имя сводится без таблицы. Буквы без двойного прочтения латиницей (ж, ш, й, х,
# ц): зритель напишет имя так же, как видит его на странице голосов. Имена не пересекаются с голосами проекта.
VOICES = {"M1": "anton", "M2": "boris", "M3": "gleb", "M4": "oleg", "M5": "pavel",
          "F1": "anna", "F2": "vera", "F3": "nina", "F4": "olga", "F5": "rita"}
DISK_EXTRA = 4 << 20   # голоса (~3 МБ) и кластеры диска сверх файлов модели


@functools.cache
def spec():
    path = paths.env("STREAM_TTS_TEST_SUPERTONIC")
    if path:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with open(os.path.join(paths.HERE, "downloads.json"), encoding="utf-8") as f:
        return json.load(f)["supertonic"]


def model_dir():
    return os.path.abspath(os.path.join(paths.MODELS, "supertonic"))


def files():
    """[(url, путь в репозитории, файл на диске, размер, sha256)]."""
    s = spec()
    return [(f"{s['url'].rstrip('/')}/{f['path']}", f["path"], os.path.join(model_dir(), *f["path"].split("/")),
             f["size"], f["sha256"]) for f in s["files"]]


def _have(dst, size):
    return os.path.isfile(dst) and os.path.getsize(dst) == size


def model_ready():
    """Все файлы закреплённой ревизии на месте (по размеру; sha256 сверяется при загрузке)."""
    return all(_have(dst, size) for _, _, dst, size, _ in files())


def size():
    return sum(f["size"] for f in spec()["files"])


def voices_dir():
    return os.path.join(paths.VOICES, SET)


def voice_path(name):
    return os.path.join(voices_dir(), name + EXT)


def voices_missing():
    """Готовые голоса, которых нет (удалили набор или модель положили вручную)."""
    return [v for v in VOICES.values() if not os.path.isfile(voice_path(v))]


def state(offered=True):
    """Для вкладки «Загрузки»: offer — загрузка есть (supertonic-server найден); model — файлы модели на месте, voices —
    голосов из 10 на месте; installed — и то и другое."""
    if not offered:
        return dict(offer=False)
    model, missing = model_ready(), voices_missing()
    left = sum(size for _, _, dst, size, _ in files() if not _have(dst, size))
    return dict(offer=True, model=model, voices=len(VOICES) - len(missing), voices_total=len(VOICES),
                installed=model and not missing, size=left, disk=size() + DISK_EXTRA, model_dir=model_dir(),
                license=spec().get("license", ""), names=sorted(VOICES.values()))


def bytes_present():
    """Скачано: файлы целиком и .part (прогресс после обрыва)."""
    return sum(size if _have(dst, size) else fetch.part_size(dst) for _, _, dst, size, _ in files())


async def download(inst, session):
    """Недостающие файлы модели (inst — Installer: прогресс dl и _push), потом готовые голоса."""
    todo = [f for f in files() if not _have(f[2], f[3])]
    inst.dl.update(total=sum(f[3] for f in files()), done=bytes_present())
    for url, rel, dst, size, sha in todo:
        await fetch.fetch(inst, session, url, rel, dst, size, sha)
    install_voices()


def install_voices():
    """Стили voice_styles/<стиль>.json модели → paths.VOICES/supertonic/<имя>.supertonic и записи в manifest.json
    набора. Файл пишется рядом и переименовывается: сервер перечитывает папку голосов при каждом запросе."""
    os.makedirs(voices_dir(), exist_ok=True)
    for style, name in VOICES.items():
        src = os.path.join(model_dir(), "voice_styles", style + ".json")
        dst = voice_path(name)
        shutil.copyfile(src, dst + ".tmp")
        os.replace(dst + ".tmp", dst)
    path = os.path.join(voices_dir(), "manifest.json")
    meta = store.read_json(path, {})
    meta = meta if isinstance(meta, dict) else {}
    for style, name in VOICES.items():
        entry = meta.get(name) if isinstance(meta.get(name), dict) else {}
        meta[name] = {**entry, "title": f"Supertonic 3 {style}"}
    store.write_json(path, meta)


def remove():
    """Удалить модель и готовые голоса (сервер Supertonic остановлен). Голоса пользователя в папке набора остаются."""
    shutil.rmtree(model_dir(), ignore_errors=True)
    for name in VOICES.values():
        with contextlib.suppress(FileNotFoundError):
            os.remove(voice_path(name))
    path = os.path.join(voices_dir(), "manifest.json")
    meta = store.read_json(path, {})
    if isinstance(meta, dict):
        meta = {k: v for k, v in meta.items() if k not in VOICES.values()}
        if meta:
            store.write_json(path, meta)
        else:
            with contextlib.suppress(FileNotFoundError):
                os.remove(path)
    with contextlib.suppress(OSError):
        os.rmdir(voices_dir())   # только пустую


def check_space():
    """Места хватит на модель. Иначе ValueError."""
    need = sum(size for _, _, dst, size, _ in files() if not _have(dst, size)) + DISK_EXTRA
    try:
        os.makedirs(paths.MODELS, exist_ok=True)
        free = shutil.disk_usage(paths.MODELS).free
    except OSError:
        return
    if free < need:
        raise ValueError(tr("для Supertonic 3 нужно {need} МБ свободного места, а есть {free} МБ",
                            need=need >> 20, free=free >> 20))
