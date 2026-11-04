"""Встроенный сервер синтеза: модель и её загрузка, устройство, проверка скорости, наборы голосов, необязательные
пакеты, CUDA, ESpeech, выгрузка моделей из памяти, обновления программы. Всё, что перезапускает или останавливает
сервер, — через _restart_guard (common.py): обучение LoRA живёт в процессе сервера."""
import logging, urllib.parse

from .. import downloads
from ..engines.client import LIVE
from ..engines.espeech import train as lora
from ..i18n import tr
from .common import _restart_guard, fail, ok, upload

log = logging.getLogger("web")


MAX_SET_UPLOAD = 2 << 30   # коллекция ESpeech: ~10 МБ на голос (адаптер LoRA), WC3 — ~680 МБ


# --- первый запуск: модель, устройство, наборы голосов ---
def _install(ctx):
    if not ctx.install:   # готовый сервер в тестах: ставить некуда
        raise ValueError("no built-in server")
    return ctx.install


async def post_model_download(request, ctx, data):
    _install(ctx).start_download(str(data.get("quant") or downloads.DEFAULT_QUANT), bool(data.get("frontend")))
    return ok()


async def post_model_cancel(request, ctx, data):
    _install(ctx).cancel()
    return ok()


async def post_model_use(request, ctx, data):
    inst = _install(ctx)
    inst.use_model(str(data["name"]))
    if ctx.runtime.launcher is inst.cosy:   # выбрана ESpeech — квант CosyVoice пригодится при возврате к ней
        await ctx.runtime.restart()
    return ok()


async def post_backend(request, ctx, data):
    """Устройство синтеза выбранной модели (у каждой своя настройка: Launcher.DEVICE_KEY)."""
    _install(ctx)
    if stop := await _restart_guard(ctx, data):
        return stop
    backend, launcher = str(data.get("backend", "auto")), ctx.runtime.launcher
    devices = launcher.public()["devices"]
    if backend not in devices:
        return fail(tr("ожидалось {values}", values=", ".join(devices)))
    ctx.settings.update(**{launcher.DEVICE_KEY: backend})
    await ctx.runtime.restart()
    return ok()


async def post_bench(request, ctx, data):
    _install(ctx)
    if data.get("all"):   # сервер перезапускается на каждом устройстве
        if stop := await _restart_guard(ctx, data):
            return stop
    elif (t := await ctx.train.server_state()) and t.get("state") in lora.BUSY:
        return fail(tr("идёт обучение LoRA: видеокарта занята им, замер был бы неверным — проверьте скорость после"))
    ctx.bench.start(bool(data.get("all")))
    return ok()


async def post_voiceset_delete(request, ctx, data):
    inst = _install(ctx)
    if stop := await _restart_guard(ctx, data):
        return stop
    await inst.delete_set(str(data["name"]), data.get("model") or None)
    return ok()


async def api_voiceset_import(request):
    """Набор голосов: zip сырым телом, имя файла — в X-Filename (URL-кодировано)."""
    ctx = request.app["ctx"]
    try:
        inst = _install(ctx)
    except ValueError as e:
        return fail(str(e))
    if stop := await _restart_guard(ctx, dict(force=request.headers.get("X-Force") == "1")):
        return stop
    filename = urllib.parse.unquote(request.headers.get("X-Filename", "voices.zip"))
    try:
        async with upload(request, ".zip", MAX_SET_UPLOAD, tr("архив больше 2 ГБ")) as tmp:
            result = await inst.import_set(tmp, filename)
    except ValueError as e:
        return fail(str(e))
    ctx.voices.forget(result["added"])  # голос мог смениться: пример пересинтезировать
    ctx.voices.wake.set()
    ctx.engine.dirty.set()
    return ok(**result)


# --- необязательные пакеты ---
async def post_package_download(request, ctx, data):
    _install(ctx).start_package(str(data["id"]))
    return ok()


async def post_package_delete(request, ctx, data):
    inst = _install(ctx)
    if (downloads.PACKAGES.get(str(data["id"])) or {}).get("restart") and (stop := await _restart_guard(ctx, data)):
        return stop   # сервер останавливается, пока удаляются файлы фронтенда
    await inst.delete_package(str(data["id"]))
    return ok()


async def post_cuda_download(request, ctx, data):
    inst = _install(ctx)
    if stop := await _restart_guard(ctx, data):   # распаковка — при остановленном сервере
        return stop
    inst.start_cuda()
    return ok()


async def post_espeech_download(request, ctx, data):
    inst = _install(ctx)
    if stop := await _restart_guard(ctx, data):   # замена рантайма и перезапуск ESpeech после скачивания
        return stop
    inst.start_espeech()
    return ok()


async def post_espeech_delete(request, ctx, data):
    inst = _install(ctx)
    if stop := await _restart_guard(ctx, data):   # сервер ESpeech останавливается, пока удаляются файлы
        return stop
    await inst.delete_espeech()
    return ok()


# --- модели в памяти (вкладка «Статистика») ---
async def post_model_unload(request, ctx, data):
    what = str(data["id"])
    if what == "tts":
        if not ctx.runtime:   # готовый сервер в тестах
            return fail("no built-in server")
        if stop := await _restart_guard(ctx, data):   # выгрузка — остановка процесса сервера
            return stop
        # после текущего запроса к серверу, иначе он оборвётся на середине
        async with ctx.tts.lane.hold(LIVE, tr("выгрузка модели")):
            await ctx.runtime.unload()
        ctx.tts.ok, ctx.tts.error = False, tr("модель выгружена")
        log.info("модель синтеза выгружена: сервер остановлен")
    elif what == "frontend":
        try:
            await ctx.editor.unload_frontend()
        except Exception as e:
            return fail(str(e) or type(e).__name__)
    elif what == "music":
        ctx.editor.separator.unload()
    else:
        return fail(tr("нет такой модели"))
    return ok()


async def post_model_load(request, ctx, data):
    if str(data["id"]) != "tts" or not ctx.runtime:
        return fail(tr("эта модель загружается сама, когда нужна"))
    ctx.runtime.load()
    log.info("модель синтеза: запуск сервера")
    return ok()


# --- обновления (updates.py) ---
async def post_update_check(request, ctx, data):
    await ctx.updates.check()
    return ok(update=ctx.updates.public())


async def post_update_dismiss(request, ctx, data):
    """Скрыть плашку о новой версии до следующей."""
    ctx.settings.update(update_dismissed=str(data["version"]))
    return ok()


POSTS = {
    "/api/model/download": post_model_download,
    "/api/model/cancel": post_model_cancel,
    "/api/model/use": post_model_use,
    "/api/backend": post_backend,
    "/api/bench": post_bench,
    "/api/voicesets/delete": post_voiceset_delete,
    "/api/package/download": post_package_download,
    "/api/package/delete": post_package_delete,
    "/api/cuda/download": post_cuda_download,
    "/api/espeech/download": post_espeech_download,
    "/api/espeech/delete": post_espeech_delete,
    "/api/models/unload": post_model_unload,
    "/api/models/load": post_model_load,
    "/api/update/check": post_update_check,
    "/api/update/dismiss": post_update_dismiss,
}
RAW = {
    "/api/voicesets/import": api_voiceset_import,
}
