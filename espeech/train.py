"""Обучение LoRA голоса ESpeech на уже загруженной модели (samples/espeech-lora): его зовут tools/espeech/train.py и
сервер (trainjob.py, POST /train). Реплики для него готовит pairs.py; шагов по объёму и признак «пора остановиться» —
default_steps и overfit (samples/espeech-lora-small).

Потеря — штатная CFM f5-tts 1.1.22 (CFM.forward): flow matching на случайном замаскированном куске 70–100 % мела
реплики, drop аудио 0,3 и всего условия 0,2 (для CFG), dropout 0,1 в attention и FFN, как у DiT f5-tts при обучении.
База заморожена в fp16, адаптеры — fp32 под autocast fp16 с GradScaler; AdamW lr 1e-4, разогрев 50 шагов, батч из
близких по длине реплик до 3000 кадров мела с паддингом. Rank 16, alpha 16 на тех же 132 слоях, что и lora.py.

Устройство:
- Своя копия весов не нужна: обучаемые A и B (fp32) кладутся в обёртки lora.LoRALinear (`fit`) только на время
  шага, у базы `requires_grad` не включается, модель всегда в eval. Между шагами модель — ровно в состоянии синтеза:
  запросы синтеза (и адаптеры других голосов в буферах lora.py) обслуживаются как обычно.
- Обучающий forward — здесь, а не в model.py: синтез (CFG одним батчем, без масок) остаётся бит в бит как у f5-tts.
  Он повторяет DiT f5-tts при обучении с маской батча: текст за длиной реплики — заполнитель, ConvPositionEmbedding
  обнуляет паддинг до и после свёрток, выход attention на паддинге обнуляется, ключи не маскируются
  (attn_mask_enabled=False в f5-tts).
- Случайность — своя (состояния ГСЧ torch подменяются на время шага, плюс свои random.Random): синтез между шагами
  делает torch.manual_seed(seed) и иначе повторял бы шум обучения, а сам он от обучения не зависит.
- Шаг — один вызов step(); run(stop) — генератор шагов до конца или до флага остановки. Проверочный лосс — на
  отложенных репликах без drop, с одним и тем же шумом, t и маской (сид 0) на каждой проверке: лоссы разных шагов
  сравнимы; 4 прохода — лосс flow matching шумный.
- checkpointing=True — пересчёт блоков DiT на обратном проходе (torch.utils.checkpoint): меньше видеопамяти ценой
  времени, для карт на 6 ГБ.

Вход — пары (звук float32 24 кГц моно, текст с ударениями «+»); выход — adapter(): тензоры `lora.<блок>.<q|k|v|o|
ff1|ff2>.<A|B>` в fp16 на CPU и метаданные lora_rank, lora_alpha, lora_steps (формат lora.py и voices.py)."""
import contextlib, dataclasses, gc, math, random, time

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn
from torch.utils.checkpoint import checkpoint

from . import model as M

AUDIO_DROP, COND_DROP = 0.3, 0.2   # CFM f5-tts: audio_drop_prob, cond_drop_prob
FRAC = (0.7, 1.0)                  # frac_lengths_mask: доля мела, которую модель восстанавливает


@dataclasses.dataclass
class Config:
    steps: int = 3000
    rank: int = 16
    alpha: float = 16
    lr: float = 1e-4
    weight_decay: float = 0.01
    warmup: int = 50
    frames: int = 3000        # кадров мела в батче (93,75 кадра/с), считая паддинг
    dropout: float = 0.1      # DiT f5-tts при обучении
    clip: float = 1.0         # clip_grad_norm
    val_every: int = 100
    val_reps: int = 4
    log_every: int = 50
    checkpointing: bool = False
    seed: int = 1


# шагов по умолчанию — по объёму, 1000 на минуту обучающих реплик: так выбрал пользователь на слух
# (samples/espeech-lora-small/слушать): 1 мин — 1000, 2 мин — 2000, от 3 мин — 3000. Минимум проверочного лосса
# раньше (1 мин — 500, 2 мин — 900–1300), но на слух переобучение до этих шагов не мешает
STEPS_PER_MIN, STEPS_MIN, STEPS_MAX = 1000, 500, 3000
# «пора остановиться»: проверочный лосс OVER_N замеров подряд выше своего минимума на OVER_BY (вдвое выше шума
# ±0,001–0,002); один замер даёт ложное срабатывание на 4 мин, «500 шагов без минимума» — на плато 4 и 6,6 мин
OVER_BY, OVER_N = 0.003, 2


def epoch_batches(frames, limit=3000):
    """Батчей на эпоху у Trainer._batches (без случайного разброса длин): реплики по длине, пока батч с паддингом
    не длиннее limit кадров."""
    n, cur = 0, []
    for f in sorted(frames):
        if cur and f * (len(cur) + 1) > limit:
            n, cur = n + 1, []
        cur.append(f)
    return n + bool(cur)


def default_steps(frames):
    """Шагов по объёму: STEPS_PER_MIN на минуту звука, округлено до 100, от STEPS_MIN до STEPS_MAX. frames — кадров
    мела обучающих реплик."""
    minutes = sum(frames) / M.FPS / 60
    return int(min(STEPS_MAX, max(STEPS_MIN, round(STEPS_PER_MIN * minutes, -2))))


def overfit(vals, by=OVER_BY, n=OVER_N):
    """По истории проверочного лосса [(шаг, лосс)] → шаг, на котором сработал признак «пора остановиться»
    (n замеров подряд выше минимума до них на ≥ by), или None."""
    best, over = None, 0
    for step, v in vals:
        if best is not None and v >= best + by:
            over += 1
            if over >= n:
                return step
        else:
            over = 0
        best = v if best is None else min(best, v)
    return None


class Item:
    """Реплика: мел (кадры, 100) на устройстве в типе модели и номера символов текста."""

    def __init__(self, mel, ids, text):
        self.mel, self.ids, self.text = mel, ids, text
        self.frames = mel.shape[0]


def lens_mask(lens, n):
    return torch.arange(n, device=lens.device)[None] < lens[:, None]


def span_mask(lens, frac, n):
    """mask_from_frac_lengths f5-tts: случайный кусок frac длины каждой реплики."""
    lengths = (frac * lens).long()
    start = ((lens - lengths) * torch.rand_like(frac)).long().clamp(min=0)
    seq = torch.arange(n, device=lens.device).long()
    return (seq[None] >= start[:, None]) & (seq[None] < (start + lengths)[:, None])


def _block(blk, x, t, rope, mask, p):
    """DiTBlock f5-tts при обучении: маска батча и dropout p (в синтезе — model.DiTBlock.forward без них)."""
    n = blk.attn_norm
    shift_msa, scale_msa, gate_msa, shift_mlp, scale_mlp, gate_mlp = torch.chunk(n.linear(n.silu(t)), 6, dim=1)
    norm = n.norm(x) * (1 + scale_msa[:, None]) + shift_msa[:, None]
    a, b = blk.attn, x.shape[0]
    q, k, v = a.to_q(norm), a.to_k(norm), a.to_v(norm)
    hd = k.shape[-1] // a.heads
    q, k, v = (y.view(b, -1, a.heads, hd).transpose(1, 2) for y in (q, k, v))
    q, k = M.apply_rotary(q, rope), M.apply_rotary(k, rope)
    o = F.scaled_dot_product_attention(q, k, v, dropout_p=0.0, is_causal=False)   # ключи без маски, как в f5-tts
    o = o.transpose(1, 2).reshape(b, -1, a.heads * hd).to(q.dtype)
    o = F.dropout(a.to_out[0](o), p, p > 0).masked_fill(~mask[..., None], 0.0)
    x = x + gate_msa.unsqueeze(1) * o
    norm = blk.ff_norm(x) * (1 + scale_mlp[:, None]) + shift_mlp[:, None]
    ff = blk.ff.ff
    return x + gate_mlp.unsqueeze(1) * ff[2](F.dropout(ff[0](norm), p, p > 0))


def dit_train_forward(dit, x, cond, ids, time, mask, drop_audio=False, drop_text=False, p=0.0, ckpt=False):
    """DiT.forward f5-tts без CFG с маской батча mask (b, n). ids (b, n) — номера символов, заполнитель −1."""
    n = x.shape[1]
    t = dit.time_embed(time)
    te = dit.text_embed(ids, n, drop_text=drop_text)
    ie = dit.input_embed
    if drop_audio:
        cond = torch.zeros_like(cond)
    h = ie.proj(torch.cat((x, cond, te), dim=-1))
    m = mask[:, None]
    c = h.permute(0, 2, 1).masked_fill(~m, 0.0)
    for layer in ie.conv_pos_embed.conv1d:
        c = layer(c)
        if isinstance(layer, nn.Conv1d):
            c = c.masked_fill(~m, 0.0)
    h = c.permute(0, 2, 1) + h
    rope = dit.rotary_embed(n)
    for blk in dit.transformer_blocks:
        if ckpt:
            h = checkpoint(_block, blk, h, t, rope, mask, p, use_reentrant=False)
        else:
            h = _block(blk, h, t, rope, mask, p)
    no = dit.norm_out
    scale, shift = torch.chunk(no.linear(no.silu(t)), 2, dim=1)
    h = no.norm(h) * (1 + scale)[:, None, :] + shift[:, None, :]
    return dit.proj_out(h)


def prepare(tts, pairs):
    """[(звук float32 24 кГц, текст с ударениями)] → [Item]: мел как у образца на синтезе (тихое — до RMS 0,1)."""
    out = []
    with torch.no_grad():
        for wav, text in pairs:
            mel, _ = tts.cond(wav)
            out.append(Item(mel[0].clone(), [tts.vocab.get(c, 0) for c in text], text))
    return out


class Trainer:
    """Обучение адаптера на модели tts (model.ESpeech) через обёртки adapters (lora.Adapters на tts.dit)."""

    def __init__(self, tts, adapters, train, val=(), cfg=None):
        self.tts, self.adapters, self.cfg = tts, adapters, cfg or Config()
        self.dev = tts.device
        self.cuda = self.dev.type == "cuda"
        self.train, self.val = list(train), list(val)
        if not self.train:
            raise ValueError("нет реплик для обучения")
        c = self.cfg
        self._gpu_idx = self.dev.index if self.dev.index is not None else 0
        with torch.random.fork_rng(devices=[self._gpu_idx] if self.cuda else []):
            torch.manual_seed(c.seed)
            self.params = {}
            for name, m in adapters.mods.items():   # как в исследовании: A ~ N(0, 1/in), B = 0 — в начале модель = база
                A = torch.randn(c.rank, m.inf, device=self.dev) / m.inf ** 0.5
                B = torch.zeros(m.outf, c.rank, device=self.dev)
                self.params[name] = (nn.Parameter(A), nn.Parameter(B))
            self._cpu_rng = torch.get_rng_state()
            self._gpu_rng = torch.cuda.get_rng_state(self.dev) if self.cuda else None
        self.rng_batch, self.rng_drop = random.Random(c.seed), random.Random(c.seed)
        flat = [p for ab in self.params.values() for p in ab]
        self.opt = torch.optim.AdamW(flat, lr=c.lr, weight_decay=c.weight_decay)
        self.sched = torch.optim.lr_scheduler.LambdaLR(self.opt, lambda s: min(1.0, (s + 1) / c.warmup))
        self.scaler = torch.amp.GradScaler(self.dev.type, enabled=self.cuda)
        self.scale = c.alpha / c.rank
        self.step_no = 0
        self.queue = []
        self.history = dict(train=[], val=[])
        self._acc, self._acc_sec = [], 0.0
        self.train_sec = sum(i.frames for i in self.train) / M.FPS
        if self.cuda:
            torch.cuda.reset_peak_memory_stats(self.dev)

    # --- служебное ---
    @contextlib.contextmanager
    def _rng(self, seed=None):
        """Свои состояния ГСЧ torch на время шага (seed — разовое, для проверки), снаружи — как было."""
        with torch.random.fork_rng(devices=[self._gpu_idx] if self.cuda else []):
            if seed is not None:
                torch.manual_seed(seed)
            else:
                torch.set_rng_state(self._cpu_rng)
                if self.cuda:
                    torch.cuda.set_rng_state(self._gpu_rng, self.dev)
            yield
            if seed is None:
                self._cpu_rng = torch.get_rng_state()
                if self.cuda:
                    self._gpu_rng = torch.cuda.get_rng_state(self.dev)

    @contextlib.contextmanager
    def _fit(self):
        """Обучаемые адаптеры — в обёртки lora.py на время прохода (с пересчётом checkpointing — и обратного)."""
        mods = self.adapters.mods
        try:
            for name, (A, B) in self.params.items():
                mods[name].fit = (A, B, self.scale)
            yield
        finally:
            for m in mods.values():
                m.fit = None

    def _autocast(self):
        return torch.autocast(self.dev.type, dtype=torch.float16, enabled=self.cuda)

    def _collate(self, items):
        n = max(i.frames for i in items)
        x = torch.zeros(len(items), n, M.N_MELS, device=self.dev, dtype=self.tts.dtype)
        ids = torch.full((len(items), n), -1, dtype=torch.long)
        for k, it in enumerate(items):
            x[k, :it.frames] = it.mel
            t = it.ids[:it.frames]   # символов больше, чем кадров, — хвост отбрасывается (как text_embed f5-tts)
            ids[k, :len(t)] = torch.tensor(t, dtype=torch.long)
        lens = torch.tensor([i.frames for i in items], device=self.dev)
        return x, ids.to(self.dev), lens

    def _batches(self):
        """Эпоха: близкие по длине реплики в один батч (меньше паддинга), порядок батчей случайный."""
        rng, items = self.rng_batch, self.train
        idx = sorted(range(len(items)), key=lambda i: items[i].frames * rng.uniform(0.9, 1.1))
        out, cur = [], []
        for i in idx:
            n = max([items[j].frames for j in cur] + [items[i].frames])
            if cur and n * (len(cur) + 1) > self.cfg.frames:
                out.append(cur)
                cur = []
            cur.append(i)
        if cur:
            out.append(cur)
        rng.shuffle(out)
        return out

    def _loss(self, items, train):
        """CFM.forward f5-tts: → средний MSE по замаскированному куску."""
        x1, ids, lens = self._collate(items)
        b, n = x1.shape[:2]
        mask = lens_mask(lens, n)
        frac = torch.zeros((b,), device=self.dev).float().uniform_(*FRAC)
        span = span_mask(lens, frac, n) & mask
        x0 = torch.randn_like(x1)
        tm = torch.rand((b,), dtype=x1.dtype, device=self.dev)
        t = tm[:, None, None]
        phi = (1 - t) * x0 + t * x1
        flow = x1 - x0
        cond = torch.where(span[..., None], torch.zeros_like(x1), x1)
        drop_audio = drop_text = False
        if train:
            drop_audio = self.rng_drop.random() < AUDIO_DROP
            if self.rng_drop.random() < COND_DROP:
                drop_audio = drop_text = True
        pred = dit_train_forward(self.tts.dit, phi, cond, ids, tm, mask, drop_audio, drop_text,
                                 self.cfg.dropout if train else 0.0, self.cfg.checkpointing and train)
        return F.mse_loss(pred, flow, reduction="none")[span].mean()

    # --- обучение ---
    @property
    def done(self):
        return self.step_no >= self.cfg.steps

    def step(self):
        """Один шаг обучения. → {step, loss, sec, lr[, val]}; sec — без проверки."""
        if not self.queue:
            self.queue = self._batches()
        items = [self.train[i] for i in self.queue.pop()]
        t0 = time.perf_counter()
        flat = [p for ab in self.params.values() for p in ab]
        with self._rng(), self._fit():
            with self._autocast():
                loss = self._loss(items, True)
            self.opt.zero_grad(set_to_none=True)
            self.scaler.scale(loss).backward()
            self.scaler.unscale_(self.opt)
            torch.nn.utils.clip_grad_norm_(flat, self.cfg.clip)
            self.scaler.step(self.opt)
            self.scaler.update()
            self.sched.step()
        loss = float(loss.detach())   # ждёт видеокарту: sec — честное время шага
        del flat
        sec = time.perf_counter() - t0
        self.step_no += 1
        rec = dict(step=self.step_no, loss=round(loss, 4), sec=round(sec, 3), lr=self.sched.get_last_lr()[0],
                   batch=len(items))
        self._acc.append(loss)
        self._acc_sec += sec
        c = self.cfg
        if self.step_no % c.log_every == 0 or self.done:
            k = len(self._acc)
            self.history["train"].append(dict(step=self.step_no, loss=round(float(np.mean(self._acc)), 4),
                                              s_per_step=round(self._acc_sec / k, 3), **self.memory()))
            self._acc, self._acc_sec = [], 0.0
        if self.val and (self.step_no % c.val_every == 0 or self.done):
            rec["val"] = self.validate()
        if not math.isfinite(loss) and not self.scaler.is_enabled():   # в fp16 такой шаг пропускает GradScaler
            raise FloatingPointError(f"лосс {loss} на шаге {self.step_no}")
        return rec

    def run(self, stop=None):
        """Генератор шагов до конца обучения или до флага stop (threading.Event): проверяется перед каждым шагом."""
        while not self.done and not (stop is not None and stop.is_set()):
            yield self.step()

    def validate(self):
        """Лосс на отложенных репликах (без drop и dropout, сид 0 — сравним между шагами); None — их нет."""
        if not self.val:
            return None
        tot = []
        with self._rng(seed=0), self._fit(), torch.no_grad(), self._autocast():
            for _ in range(self.cfg.val_reps):
                for it in self.val:
                    tot.append(float(self._loss([it], False)))
        v = round(float(np.mean(tot)), 4)
        self.history["val"].append(dict(step=self.step_no, val=v))
        return v

    def memory(self):
        if not self.cuda:
            return {}
        return dict(peak_mb=torch.cuda.max_memory_allocated(self.dev) >> 20,
                    reserved_mb=torch.cuda.max_memory_reserved(self.dev) >> 20)

    def adapter(self):
        """Текущий адаптер: ({"lora.<блок>.<цель>.A|B": fp16 на CPU}, метаданные для файла голоса)."""
        t = {}
        for name, (A, B) in self.params.items():
            t[f"lora.{name}.A"] = A.detach().to("cpu", torch.float16).contiguous()
            t[f"lora.{name}.B"] = B.detach().to("cpu", torch.float16).contiguous()
        c = self.cfg
        return t, dict(lora_rank=c.rank, lora_alpha=f"{c.alpha:g}", lora_steps=self.step_no)

    def close(self):
        """Освободить параметры, оптимизатор и мел реплик; модель остаётся как была (обёртки lora.py — без fit)."""
        for m in self.adapters.mods.values():
            m.fit = None
        self.params, self.opt, self.sched, self.scaler = {}, None, None, None
        self.train, self.val, self.queue = [], [], []
        gc.collect()
        if self.cuda:
            torch.cuda.empty_cache()
