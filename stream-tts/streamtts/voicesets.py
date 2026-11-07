"""Наборы голосов из zip: проверка архива, установка, удаление по модели, список установленных.

Набор голосов — zip с голосами (*.gguf CosyVoice, *.espeech ESpeech, *.supertonic Supertonic 3) и manifest.json
(ключи — имена голосов: title, post…) в корне или в одной папке. Имя набора — эта папка или имя zip; оно же группа на странице голосов
(voices.GROUPS). Набор ставится в paths.VOICES/<набор>/; голоса тех моделей, что есть в zip, заменяются целиком,
голоса другой модели в той же папке остаются (коллекция «модель + источник»: voice-sets/<модель>/<источник>.zip).
Голос, который у этой модели уже есть в другом наборе или без набора, пропускается. Удаляется набор тоже по модели.
Пока файлы меняются, сервер синтеза остановлен (Installer.import_set и delete_set, install.py: на Windows
cosyvoice-server держит открытые .gguf), потом запускается с новым списком голосов."""
import contextlib, json, os, re, shutil, zipfile

from . import paths, store
from .engines import ENGINES, cosyvoice, supertonic
from .i18n import tr
from .voices import NAME as VOICE_NAME

SET_NAME = re.compile(r"[^a-z0-9_-]+")
MAX_VOICE = 64 << 20       # голос CosyVoice ~200 КБ, ESpeech с адаптером ~10 МБ, Supertonic ~300 КБ; больше — не голос
# Модель по расширению файла голоса. Папка набора (источника) держит голоса обеих моделей: голос с тем же именем у
# другой модели — в том же наборе (editor.py). Коллекция в zip — голоса одной модели одного источника
# (voice-sets/<модель>/<источник>.zip, tools/voices/pack-sets.py): её установка и удаление не трогают файлы другой.
EXT_MODEL = {E.VOICE_EXT: i for i, E in ENGINES.items()}
VOICE_EXTS = tuple(EXT_MODEL)
MAX_ZIP_FILES = 2000
MAX_MANIFEST = 1 << 20     # manifest.json читается в память целиком: zip-бомба на гигабайты съела бы её


def _voice_files(d):
    """{модель: {голос: файл}} в папке d (без подпапок)."""
    out = {}
    with contextlib.suppress(OSError):
        for f in os.listdir(d):
            name, ext = os.path.splitext(f)
            if ext in EXT_MODEL and os.path.isfile(os.path.join(d, f)):
                out.setdefault(EXT_MODEL[ext], {})[name] = f
    return out


def list_sets():
    """Установленные наборы: подпапки paths.VOICES с голосами, по записи на модель (порядок ENGINES)."""
    out = []
    try:
        names = sorted(os.listdir(paths.VOICES))
    except OSError:
        return out
    for n in names:
        d = os.path.join(paths.VOICES, n)
        if n.startswith(".") or not os.path.isdir(d):
            continue
        files = _voice_files(d)
        for model in (m for m in ENGINES if m in files):
            out.append(dict(name=n, model=model, title=ENGINES[model].TITLE, voices=sorted(files[model]),
                            manifest=os.path.isfile(os.path.join(d, "manifest.json"))))
    return out


def _existing_voices(skip_set, model):
    """Голос модели -> где лежит (набор или корень), кроме заменяемого набора."""
    out = dict.fromkeys(_voice_files(paths.VOICES).get(model, {}), tr("голоса без набора"))
    for s in list_sets():
        if s["name"] != skip_set and s["model"] == model:
            out.update(dict.fromkeys(s["voices"], tr("набор {name}", name=s["name"])))
    return out


def _check_voice(z, i, ext):
    """Файл похож на голос своей модели: GGUF — по сигнатуре, .espeech (safetensors) — по заголовку JSON,
    .supertonic — JSON стиля с ключами style_ttl и style_dp."""
    if ext == supertonic.VOICE_EXT:
        try:
            d = json.loads(z.read(i).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        return isinstance(d, dict) and all(isinstance(d.get(k), dict) for k in ("style_ttl", "style_dp"))
    with z.open(i) as f:
        head = f.read(9)
    if ext == cosyvoice.VOICE_EXT:
        return head[:4] == b"GGUF"
    n = int.from_bytes(head[:8], "little")
    return len(head) == 9 and 0 < n < i.file_size and head[8:9] == b"{"


def _check_zip(path, filename):
    """Что ставить из zip: имя набора, голоса ({модель: {имя: запись в zip}}), manifest. Ошибка — ValueError."""
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise ValueError(tr("это не zip"))
    with z:
        entries = [i for i in z.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")]
        if len(entries) > MAX_ZIP_FILES:
            raise ValueError(tr("слишком много файлов в архиве"))
        tops = {i.filename.split("/")[0] for i in entries if "/" in i.filename}
        single = len(tops) == 1 and all("/" in i.filename for i in entries)
        raw = tops.pop() if single else os.path.splitext(os.path.basename(filename or "voices"))[0]
        name = SET_NAME.sub("-", raw.lower()).strip("-_") or "voices"
        voices, manifest, ignored = {}, None, []
        for i in entries:
            rel = i.filename.split("/", 1)[1] if single else i.filename
            base = rel.rsplit("/", 1)[-1]
            voice, ext = os.path.splitext(base)
            ext = ext.lower()
            if "/" in rel:  # только верхний уровень набора
                ignored.append(i.filename)
            elif base == "manifest.json":
                if i.file_size > MAX_MANIFEST:   # zipfile не читает больше file_size: его и проверяем
                    raise ValueError(tr("manifest.json больше 1 МБ"))
                manifest = i.filename
            elif ext in EXT_MODEL:
                if not VOICE_NAME.fullmatch(voice):
                    raise ValueError(tr("недопустимое имя голоса: {voice}", voice=voice))
                if i.file_size > MAX_VOICE:
                    raise ValueError(tr("{file}: слишком большой для голоса", file=base))
                if not _check_voice(z, i, ext):
                    raise ValueError(tr("{file}: не голос {model}", file=base, model=ENGINES[EXT_MODEL[ext]].TITLE))
                voices.setdefault(EXT_MODEL[ext], {})[voice] = i.filename
            else:
                ignored.append(i.filename)
        if not voices:
            raise ValueError(tr("в архиве нет голосов ({exts})", exts=", ".join("*" + e for e in VOICE_EXTS)))
        meta = {}
        if manifest:
            try:
                meta = json.loads(z.read(manifest).decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                raise ValueError(f"manifest.json: {e}")
            if not isinstance(meta, dict):
                raise ValueError(tr("manifest.json: ожидался объект {голос: {...}}"))
    return dict(name=name, voices=voices, manifest=meta, ignored=ignored)


def _prune_manifest(d, add=None, keep=()):
    """manifest.json набора: записи новых голосов поверх старых, без голосов, у которых не осталось файлов.
    Запись голоса из keep (у него есть файл другой модели) сливается по полям: манифест общий для обеих моделей, и
    коллекция ESpeech без post иначе стирала бы эффект голоса CosyVoice. Остальные записи заменяются целиком —
    новая версия коллекции может убрать поле."""
    path = os.path.join(d, "manifest.json")
    have = {v for vs in _voice_files(d).values() for v in vs}
    meta = store.read_json(path, {})
    meta = {k: v for k, v in (meta if isinstance(meta, dict) else {}).items() if k in have}
    for k, v in (add or {}).items():
        if k in have:
            old = meta.get(k)
            meta[k] = {**old, **v} if k in keep and isinstance(old, dict) and isinstance(v, dict) else v
    if meta:   # на месте, без .tmp, как и было: не store.write_json
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
    else:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


def _install_set(path, info):
    name = info["name"]
    target = os.path.join(paths.VOICES, name)
    added, skipped = [], []
    os.makedirs(paths.VOICES, exist_ok=True)
    tmp = os.path.join(paths.VOICES, f".{name}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    try:
        with zipfile.ZipFile(path) as z:   # сначала всё во временную папку: битый zip не трогает набор
            for model, voices in sorted(info["voices"].items()):
                taken = _existing_voices(name, model)
                ext = ENGINES[model].VOICE_EXT
                for voice, entry in sorted(voices.items()):
                    if voice in taken:
                        skipped.append(tr("{voice} (уже есть: {where})", voice=voice + ext, where=taken[voice]))
                        continue
                    with z.open(entry) as src, open(os.path.join(tmp, voice + ext), "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    if voice not in added:   # голос с файлами обеих моделей в одном zip — один голос
                        added.append(voice)
        if not added:
            raise ValueError(tr("все голоса набора уже установлены в других наборах: {voices}", voices=", ".join(skipped)))
        # голоса моделей из zip заменяются целиком, голоса другой модели остаются
        os.makedirs(target, exist_ok=True)
        old = _voice_files(target)
        for model in info["voices"]:
            for f in old.get(model, {}).values():
                os.remove(os.path.join(target, f))
        for f in os.listdir(tmp):
            os.replace(os.path.join(tmp, f), os.path.join(target, f))
        others = {v for m, vs in _voice_files(target).items() if m not in info["voices"] for v in vs}
        _prune_manifest(target, {k: v for k, v in info["manifest"].items() if k in added}, keep=others)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return dict(name=name, models=sorted(info["voices"]), added=added, skipped=skipped, ignored=info["ignored"])


def _delete_set(name, model=None):
    target = os.path.join(paths.VOICES, name)
    if model:
        for f in _voice_files(target).get(model, {}).values():
            os.remove(os.path.join(target, f))
        _prune_manifest(target)
    if model is None or not _voice_files(target):
        shutil.rmtree(target)
