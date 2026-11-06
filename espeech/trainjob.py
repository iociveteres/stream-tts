"""Обучение LoRA как задача espeech-server (POST /train и др., docs/espeech.md, «Обучение LoRA в сервере»).

Одно обучение за раз. Всё, что трогает модель, — в рабочем потоке Engine: старт (мел реплик, Trainer, проверка на
шаге 0) — одно задание, дальше каждый шаг — отдельное задание, которое в конце ставит следующий шаг в конец очереди.
Запросы синтеза, пришедшие во время шага, поэтому выполняются сразу после него (синтез между шагами обучение не
меняет и пик видеопамяти не поднимает, samples/espeech-lora-small). Остановка, сохранение и сброс — тоже задания
потока: выполняются после текущего шага, а следующий шаг видит, что обучение закончено, и ничего не делает.

Результаты — снимки адаптера в памяти процесса (fp16 на CPU, ~12 МБ каждый): «last» — текущий (во время обучения
берётся из параметров), «best» — на минимуме проверочного лосса (только с отложенными репликами). Их можно слушать
(POST /tts с adapter), сохранить в файл голоса (POST /train/save) или выбросить. Конец процесса — конец результатов.

Состояния: idle (нет результатов) → running → stopping (после /train/stop, до конца текущего шага) → done
(результаты в памяти) или error (код: oom — не хватило видеопамяти, nan, internal; снимки, если успели, остаются)."""
import datetime, logging, os, shutil, threading, time
from collections import deque

from . import pairs as P

log = logging.getLogger("espeech.train")
IDLE, RUNNING, STOPPING, DONE, ERROR = "idle", "running", "stopping", "done", "error"
SNAPSHOTS = ("last", "best")
MAX_STEPS = 20000
# сверх загруженной модели (рост nvidia-smi, samples/espeech-lora-small): 3,7 ГБ на процесс без checkpointing,
# 1,3–1,4 ГБ с ним, у загруженной модели — 0,87–0,9 ГБ
VRAM_NEED_MB = {"default": 2800, "checkpointing": 500}


class TrainError(Exception):
    """Ошибка запроса обучения: HTTP-статус, код для программы и текст для человека."""

    def __init__(self, status, code, text):
        super().__init__(text)
        self.status, self.code = status, code


def messages(code, checkpointing):
    if code == "oom":
        return ("Не хватило видеопамяти: закройте игры и программы на видеокарте и запустите обучение снова" +
                ("" if checkpointing else " — или включите экономию видеопамяти"))
    if code == "nan":
        return "Обучение разошлось (лосс не число): запустите заново с другим seed"
    return "Обучение прервано ошибкой сервера (подробности — в его логе)"


class Training:
    def __init__(self, engine):
        self.e = engine
        self.lock = threading.Lock()
        self.id = 0
        self._reset()

    def _reset(self):
        self.state = IDLE
        self.tr = None
        self.voice = self.path = None
        self.steps = self.step = 0
        self.seed, self.checkpointing = 1, False
        self.data = {}
        self.snaps = {}                 # "last" / "best" → (тензоры, метаданные) — после конца обучения
        self.best = None                # {step, val}
        self.best_snap = None           # снимок на минимуме во время обучения
        self.val, self.loss_hist = [], []
        self.overfit_step = None
        self.recent = deque(maxlen=50)  # (лосс, с/шаг) последних шагов
        self.job_sec = 0.0              # сумма времени шагов с проверкой — для оценки оставшегося
        self.started = self.ended = None
        self.finished = None            # steps / stopped / unload
        self.error = self.message = None
        self.saved = None
        self.memory = {}

    # --- запросы (потоки HTTP) ---
    @property
    def busy(self):
        return self.state in (RUNNING, STOPPING)

    @property
    def unsaved(self):
        """Результаты законченного обучения, которые ещё не сохранены: новый старт их не выбросит молча. После ошибки
        снимки (если успели) сохранить можно, но новый старт их выбрасывает: после oom его и ждут — с экономией."""
        return self.state == DONE and bool(self.snaps) and self.saved is None

    def check_free(self):
        if self.busy:
            raise TrainError(409, "busy", f"уже идёт обучение голоса {self.voice}")
        if self.unsaved:
            raise TrainError(409, "unsaved", f"есть несохранённые результаты обучения голоса {self.voice}: "
                                             "сохраните (/train/save) или выбросьте (/train/discard)")

    def request_stop(self):
        with self.lock:
            if self.state == RUNNING:
                self.state = STOPPING
                return True
        return False

    def status(self, full=True):
        with self.lock:
            d = dict(state=self.state, voice=self.voice, step=self.step, steps=self.steps)
            if not full:
                return d
            rec = list(self.recent)
            sps = round(sum(s for _, s in rec) / len(rec), 3) if rec else None
            eta = (round((self.steps - self.step) * self.job_sec / self.step) if self.step and self.busy else None)
            now = self.ended or time.time()
            d.update(loss=round(sum(x for x, _ in rec) / len(rec), 4) if rec else None, s_per_step=sps, eta_sec=eta,
                     elapsed_sec=round(now - self.started) if self.started else None, seed=self.seed,
                     checkpointing=self.checkpointing, data=dict(self.data), val_history=list(self.val),
                     loss_history=list(self.loss_hist), best=self.best, overfit=self.overfit_step is not None,
                     overfit_step=self.overfit_step, snapshots=self._available(), finished=self.finished,
                     saved=self.saved, memory=dict(self.memory), error=self.error, message=self.message)
            return d

    def _available(self):
        if self.busy:
            return ["last"] + (["best"] if self.best_snap else [])
        return [k for k in SNAPSHOTS if k in self.snaps]

    # --- рабочий поток ---
    def start(self, voice, path, train, val, steps=None, checkpointing=False, seed=1, val_note=None):
        """train, val — [(звук, текст с ударениями)]. → status()."""
        self.check_free()   # повтор проверки обработчика: два старта подряд
        e = self.e
        e.require()
        tts = e.tts
        with self.lock:
            self._reset()
            self.id += 1
            self.voice, self.path, self.seed, self.checkpointing = voice, path, seed, checkpointing
            self.state, self.started = RUNNING, time.time()
        err = None
        try:
            self._setup(tts, train, val, steps, checkpointing, seed, val_note)
        except Exception as ex:
            err = self._describe(ex)
        if err:
            self._fail(*err)
        else:
            self._queue_step()
        return self.status()

    def _setup(self, tts, train, val, steps, checkpointing, seed, val_note):
        """Мел реплик, Trainer, проверка на шаге 0 (при ошибке мел освобождается вместе с кадром функции)."""
        from . import train as T
        items, vitems = T.prepare(tts, train), T.prepare(tts, val)
        n = steps or T.default_steps([i.frames for i in items])
        cfg = T.Config(steps=n, checkpointing=checkpointing, seed=seed)
        tr = T.Trainer(tts, self.e.adapters, items, vitems, cfg)
        with self.lock:
            self.tr, self.steps = tr, n
            self.data = dict(train_items=len(items), train_sec=round(tr.train_sec, 1), val_items=len(vitems),
                             val_sec=round(sum(i.frames for i in vitems) / T.M.FPS, 1), val_note=val_note,
                             batches_per_epoch=T.epoch_batches([i.frames for i in items], cfg.frames),
                             steps_default=steps is None)
        log.info("обучение %s: реплик %d (%.1f мин), в проверке %d, шагов %d%s, checkpointing %s, seed %d",
                 self.voice, len(items), tr.train_sec / 60, len(vitems), n,
                 " (по объёму)" if steps is None else "", checkpointing, seed)
        v = tr.validate()
        if v is not None:
            self._on_val(0, v)

    def _queue_step(self):
        import queue
        self.e.jobs.put((lambda i=self.id: self._step(i), queue.Queue()))

    def _step(self, job_id):
        if job_id != self.id or self.tr is None:
            return
        if self.state == STOPPING:
            return self.finish("stopped")
        tr, t0, err = self.tr, time.perf_counter(), None
        try:
            rec = tr.step()
        except Exception as ex:
            err = self._describe(ex)
        if err:
            return self._fail(*err)
        with self.lock:
            self.step = rec["step"]
            self.recent.append((rec["loss"], rec["sec"]))
            self.job_sec += time.perf_counter() - t0
            self.loss_hist = list(tr.history["train"])
            self.memory = tr.memory()
        if rec.get("val") is not None:
            self._on_val(rec["step"], rec["val"])
        if rec["step"] % 100 == 0:
            log.info("обучение %s: шаг %d/%d, лосс %.4f%s", self.voice, rec["step"], self.steps,
                     sum(x for x, _ in self.recent) / len(self.recent),
                     f", проверка {rec['val']}" if rec.get("val") is not None else "")
        if tr.done:
            return self.finish("steps")
        self._queue_step()

    def _on_val(self, step, v):
        from . import train as T
        with self.lock:
            self.val.append(dict(step=step, val=v))
            new_best = self.best is None or v < self.best["val"]
            if self.overfit_step is None:
                self.overfit_step = T.overfit([(x["step"], x["val"]) for x in self.val])
                if self.overfit_step is not None:
                    log.info("обучение %s: пора остановиться (шаг %d, лучший %d)", self.voice, step, self.best["step"])
        if new_best:
            snap = self.tr.adapter()
            with self.lock:
                self.best, self.best_snap = dict(step=step, val=v), snap

    def finish(self, reason):
        """Конец обучения (шаги кончились, остановка, выгрузка модели): снимки — в память, видеопамять — системе."""
        tr = self.tr
        if tr is None:
            return
        err = None
        try:
            if tr.val and tr.step_no and (not self.val or self.val[-1]["step"] != tr.step_no):
                self._on_val(tr.step_no, tr.validate())   # последний снимок сравним с лучшим
            last = tr.adapter()
        except Exception as ex:
            err = self._describe(ex)
        if err:
            return self._fail(*err)
        self._close(last)
        with self.lock:
            self.state, self.finished, self.ended = DONE, reason, time.time()
        log.info("обучение %s: %s на шаге %d из %d, лучший шаг %s", self.voice,
                 dict(steps="закончено", stopped="остановлено", unload="остановлено выгрузкой модели")[reason],
                 self.step, self.steps, self.best and self.best["step"])

    def _close(self, last=None):
        tr = self.tr
        snaps = {}
        if last is not None and tr is not None and tr.step_no:
            snaps["last"] = last
        if self.best_snap is not None:
            snaps["best"] = self.best_snap
        with self.lock:
            self.snaps, self.tr, self.best_snap = snaps, None, None
        if tr is not None:
            tr.close()   # gc и empty_cache: видеопамять обучения — обратно
        else:            # ошибка до Trainer: мел реплик
            import gc, torch
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

    def _describe(self, ex):
        import torch
        if isinstance(ex, torch.cuda.OutOfMemoryError):
            code = "oom"   # текст torch на Windows (WDDM) мусорный — в лог, а не пользователю
        elif isinstance(ex, FloatingPointError):
            code = "nan"
        else:
            code = "internal"
        log.error("обучение %s: ошибка на шаге %d (%s): %s: %s", self.voice, self.step, code, type(ex).__name__,
                  str(ex).splitlines()[0] if str(ex) else "", exc_info=ex if code == "internal" else None)
        return code, messages(code, self.checkpointing)

    def _fail(self, code, message):
        last = None
        if self.tr is not None and self.tr.step_no:
            try:
                last = self.tr.adapter()
            except Exception:
                pass
        self._close(last)
        with self.lock:
            self.state, self.error, self.message, self.ended = ERROR, code, message, time.time()

    def stop(self):
        """После текущего шага (задание встаёт в очередь за ним)."""
        if self.state == STOPPING and self.tr is not None:
            self.finish("stopped")
        return self.status()

    def discard(self):
        if self.state != IDLE:
            log.info("обучение %s: результаты выброшены%s", self.voice, " (обучение остановлено)" if self.tr else "")
        if self.tr is not None:
            self._close()
        with self.lock:
            self._reset()
        return self.status()

    def unload(self):
        """Выгрузка модели во время обучения — его остановка: снимки на CPU остаются (сохранить можно и без модели,
        слушать — после /model/load)."""
        if self.busy:
            self.finish("unload")

    def adapter(self, which):
        """Кандидат для прослушивания → (ключ для lora.Adapters, тензоры, метаданные)."""
        if which not in SNAPSHOTS:
            raise TrainError(400, "bad_adapter", "adapter: last, best или none")
        if self.busy and self.tr is not None:
            if which == "last":
                t, md = self.tr.adapter()
                return ("train", self.id, "last", self.tr.step_no), t, md
            snap = self.best_snap
        else:
            snap = self.snaps.get(which)
        if snap is None:
            raise TrainError(409, "no_snapshot", "нет результата обучения" if which == "last" else
                             "нет лучшего снимка: обучение без отложенных реплик")
        step = snap[1].get("lora_steps")
        return ("train", self.id, which, step), snap[0], snap[1]

    def save(self, which):
        """Снимок → в файл голоса обучения. Перед первым сохранением результатов файл копируется в <файл>.bak (одна
        копия: состояние до последнего обучения; реестр её не видит — не *.espeech)."""
        from . import voices as V
        if which not in SNAPSHOTS:
            raise TrainError(400, "bad_snapshot", "snapshot: last или best")
        if self.busy:
            raise TrainError(409, "busy", "обучение ещё идёт: сначала /train/stop")
        snap = self.snaps.get(which)
        if snap is None:
            raise TrainError(409, "no_snapshot", "нет результата обучения" if which == "last" else
                             "нет лучшего снимка: обучение без отложенных реплик")
        if not os.path.isfile(self.path):
            raise TrainError(409, "voice_missing", f"файла голоса больше нет: {self.path}")
        backup = self.saved["backup"] if self.saved else None
        if backup is None:
            backup = self.path + ".bak"
            shutil.copy2(self.path, backup)
        tensors, meta = snap
        meta = dict(meta, lora_data_sec=f"{self.data.get('train_sec', 0):.1f}",
                    lora_date=datetime.datetime.now().isoformat(timespec="seconds"))
        V.save_adapter(self.path, self.path, tensors, meta)
        with self.lock:
            self.saved = dict(snapshot=which, step=int(meta["lora_steps"]), path=self.path, backup=backup)
        log.info("обучение %s: снимок %s (шаг %s) записан в %s, прежний файл — %s", self.voice, which,
                 meta["lora_steps"], self.path, backup)
        return dict(self.saved)


def limits():
    return dict(min_sec=P.MIN_SEC, max_sec=P.MAX_SEC, recommended_min_sec=60, max_steps=MAX_STEPS,
                val_min_items=P.VAL_MIN_ITEMS, vram_need_mb=VRAM_NEED_MB)
