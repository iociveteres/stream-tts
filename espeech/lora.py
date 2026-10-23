"""LoRA голоса поверх DiT без вливания в веса (решение: смена голоса — подмена адаптера, без копии весов).

132 слоя — to_q/k/v, to_out и оба слоя FFN в 22 блоках — оборачиваются один раз при загрузке модели, буферы адаптера
(fp16 на видеокарте, ~12 МБ при rank 16) выделены заранее; смена голоса копирует в них новый адаптер (~20 мс). Ветка
считается так: y = W x + b, затем y += (x A) (scale·B) на месте одним addmm_ — ×1,11 к базе против ×1,54 у наивной
LoRA в fp32 (samples/espeech-lora-pipe). Голос без LoRA — флаг выключен, цена нулевая.

Адаптер в файле голоса — тензоры `lora.<блок>.<q|k|v|o|ff1|ff2>.<A|B>` (A — rank × in, B — out × rank, fp16) и
метаданные `lora_rank`, `lora_alpha`; scale = alpha / rank вкладывается в B при загрузке."""
import torch
from torch import nn

TARGETS = ("q", "k", "v", "o", "ff1", "ff2")


class LoRALinear(nn.Module):
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.inf, self.outf = base.in_features, base.out_features
        self.on = False
        self.A = self.B = None

    def alloc(self, rank):
        w = self.base.weight
        self.A = torch.zeros(rank, self.inf, device=w.device, dtype=w.dtype)
        self.B = torch.zeros(self.outf, rank, device=w.device, dtype=w.dtype)

    def forward(self, x):
        y = self.base(x)
        if self.on:   # выход nn.Linear — новый непрерывный тензор: прибавлять на месте безопасно
            y.view(-1, self.outf).addmm_(x.reshape(-1, self.inf) @ self.A.t(), self.B.t())
        return y


def _slots(dit):
    for i, blk in enumerate(dit.transformer_blocks):
        yield f"{i}.q", blk.attn, "to_q"
        yield f"{i}.k", blk.attn, "to_k"
        yield f"{i}.v", blk.attn, "to_v"
        yield f"{i}.o", blk.attn.to_out, 0
        yield f"{i}.ff1", blk.ff.ff[0], 0
        yield f"{i}.ff2", blk.ff.ff, 2


class Adapters:
    """Обёртки LoRA на слоях DiT и текущий адаптер (current — имя голоса или None)."""

    def __init__(self, dit, rank=16):
        self.mods = {}
        for name, parent, key in _slots(dit):
            base = parent[key] if isinstance(key, int) else getattr(parent, key)
            m = LoRALinear(base)
            if isinstance(key, int):
                parent[key] = m
            else:
                setattr(parent, key, m)
            self.mods[name] = m
        self.rank = None
        self._alloc(rank)
        self.current = None

    def _alloc(self, rank):
        for m in self.mods.values():
            m.alloc(rank)
        self.rank = rank

    @staticmethod
    def check(tensors, meta, dit_blocks):
        """Подходит ли адаптер к модели: все ключи на месте и rank одинаков. → (rank, alpha) или ошибка."""
        rank = int(meta.get("lora_rank", 0))
        alpha = float(meta.get("lora_alpha", rank))
        names = {f"{i}.{t}" for i in range(dit_blocks) for t in TARGETS}
        for n in names:
            A, B = tensors.get(f"lora.{n}.A"), tensors.get(f"lora.{n}.B")
            if A is None or B is None or A.shape[0] != rank or B.shape[1] != rank:
                raise ValueError(f"адаптер не подходит к модели: lora.{n} (rank {rank})")
        return rank, alpha

    @torch.no_grad()   # без no_grad копии в буферы оставляют историю autograd: +0,4 МБ на каждую смену голоса
    def use(self, key, tensors=None, meta=None):
        """Включить адаптер голоса key (tensors — {имя: тензор} из файла голоса); tensors=None — выключить."""
        if tensors is None:
            for m in self.mods.values():
                m.on = False
            self.current = None
            return
        if key == self.current:
            return
        rank, alpha = self.check(tensors, meta or {}, len(self.mods) // len(TARGETS))
        if rank != self.rank:
            self._alloc(rank)
        scale = alpha / rank
        for n, m in self.mods.items():
            m.A.copy_(tensors[f"lora.{n}.A"], non_blocking=True)
            B = tensors[f"lora.{n}.B"]
            m.B.copy_(B * scale if scale != 1 else B, non_blocking=True)
            m.on = True
        self.current = key
