"""ESpeech-TTS-1_SFT-256K (F5-TTS: DiT 22 блока, flow matching) и вокодер Vocos 24 кГц — свой код без f5-tts,
x-transformers, einx, vocos, torchaudio и librosa. Повторяет облегчённый синтез из исследования
samples/espeech-lean (f5-tts 1.1.22, vocos 0.1.0) бит в бит: те же операции в том же порядке и типах, поэтому имена
модулей совпадают с ключами весов (`transformer.…`, `backbone.…`, `head.…`).

Отличия от f5-tts: текст образца и фразы кодируется один раз на синтез (у f5-tts — кеш на первом шаге), CFG всегда на
всех шагах (cond и uncond одним батчем 2), нет масок батча (синтез всегда по одной фразе). STFT образца и iSTFT
вокодера — на CPU: так рантайму не нужны cuFFT и NVRTC (в облегчённом torch они — заглушки, docs/espeech.md).

Веса — выход tools/espeech/prepare.py: espeech_fp16.safetensors (одна EMA-копия в fp16), vocos.safetensors, vocab.txt."""
import math, os

import torch
import torch.nn.functional as F
from safetensors.torch import load_file
from torch import nn

HOP, SR, N_FFT, N_MELS = 256, 24000, 1024, 100
FPS = SR / HOP            # кадров мела в секунду (93,75)
TARGET_RMS = 0.1          # тихий образец f5-tts поднимает до этой громкости, синтез потом опускает обратно
CFG, SWAY = 2.0, -1.0     # сила CFG и sway sampling f5-tts
# сетки EPSS f5-tts (из 32 шагов); для других чисел шагов — равномерная
EPSS = {5: [0, 2, 4, 8, 16, 32], 6: [0, 2, 4, 6, 8, 16, 32], 7: [0, 2, 4, 6, 8, 16, 24, 32],
        10: [0, 2, 4, 6, 8, 12, 16, 20, 24, 28, 32], 12: [0, 2, 4, 6, 8, 10, 12, 14, 16, 20, 24, 28, 32],
        16: [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 14, 16, 20, 24, 28, 32]}


class Stopped(Exception):
    """Синтез прерван (POST /tts/stop)."""


# ------------------------------------------------------------------------------------------- мел образца
def mel_fb(n_freqs=N_FFT // 2 + 1, n_mels=N_MELS, sr=SR):
    """torchaudio melscale_fbanks(norm=None, mel_scale="htk") в float32, как там (в float64 расходится в 1e-5)."""
    all_f = torch.linspace(0, sr // 2, n_freqs)
    m_pts = torch.linspace(2595.0 * math.log10(1.0), 2595.0 * math.log10(1.0 + (sr / 2) / 700.0), n_mels + 2)
    f_pts = 700.0 * (10.0 ** (m_pts / 2595.0) - 1.0)
    f_diff = f_pts[1:] - f_pts[:-1]
    slopes = f_pts.unsqueeze(0) - all_f.unsqueeze(1)
    down, up = (-1.0 * slopes[:, :-2]) / f_diff[:-1], slopes[:, 2:] / f_diff[1:]
    return torch.max(torch.zeros(1), torch.min(down, up))


_FB = None


def log_mel(wav):
    """wav (1, n) float32 на CPU → (1, 100, кадры) — get_vocos_mel_spectrogram f5-tts (power=1, center, reflect)."""
    global _FB
    if _FB is None:
        _FB = (mel_fb(), torch.hann_window(N_FFT))
    fb, win = _FB
    spec = torch.stft(wav, N_FFT, HOP, N_FFT, win, center=True, pad_mode="reflect", return_complex=True).abs()
    return torch.matmul(spec.transpose(-1, -2), fb).transpose(-1, -2).clamp(min=1e-5).log()


# ------------------------------------------------------------------------------------------- DiT
def precompute_freqs_cis(dim, end, theta=10000.0):
    freqs = 1.0 / (theta ** (torch.arange(0, dim, 2)[: (dim // 2)].float() / dim))
    t = torch.arange(end, device=freqs.device)
    freqs = torch.outer(t, freqs).float()
    return torch.cat([torch.cos(freqs), torch.sin(freqs)], dim=-1)


class GRN(nn.Module):
    def __init__(self, dim):
        super().__init__()
        self.gamma = nn.Parameter(torch.zeros(1, 1, dim))
        self.beta = nn.Parameter(torch.zeros(1, 1, dim))

    def forward(self, x):
        Gx = torch.norm(x, p=2, dim=1, keepdim=True)
        Nx = Gx / (Gx.mean(dim=-1, keepdim=True) + 1e-6)
        return self.gamma * (x * Nx) + self.beta + x


class ConvNeXtV2Block(nn.Module):
    def __init__(self, dim, intermediate_dim):
        super().__init__()
        self.dwconv = nn.Conv1d(dim, dim, kernel_size=7, padding=3, groups=dim)
        self.norm = nn.LayerNorm(dim, eps=1e-6)
        self.pwconv1 = nn.Linear(dim, intermediate_dim)
        self.act = nn.GELU()
        self.grn = GRN(intermediate_dim)
        self.pwconv2 = nn.Linear(intermediate_dim, dim)

    def forward(self, x):
        residual = x
        x = self.dwconv(x.transpose(1, 2)).transpose(1, 2)
        x = self.pwconv2(self.grn(self.act(self.pwconv1(self.norm(x)))))
        return residual + x


class TextEmbedding(nn.Module):
    def __init__(self, text_num_embeds, text_dim, conv_layers, conv_mult=2):
        super().__init__()
        self.text_embed = nn.Embedding(text_num_embeds + 1, text_dim)   # 0 — заполнитель
        self.register_buffer("freqs_cis", precompute_freqs_cis(text_dim, 8192), persistent=False)
        self.text_blocks = nn.Sequential(*[ConvNeXtV2Block(text_dim, text_dim * conv_mult) for _ in range(conv_layers)])

    def forward(self, text, seq_len, drop_text=False):
        text = text + 1
        text = text[:, :seq_len]   # символов больше, чем кадров, — хвост текста отбрасывается
        text = F.pad(text, (0, seq_len - text.shape[1]), value=0)
        text_mask = (text == 0).unsqueeze(-1)
        if drop_text:
            text = torch.zeros_like(text)
        text = self.text_embed(text) + self.freqs_cis[:seq_len, :]
        text = text.masked_fill(text_mask.expand(-1, -1, text.size(-1)), 0.0)
        for block in self.text_blocks:
            text = block(text)
            text = text.masked_fill(text_mask.expand(-1, -1, text.size(-1)), 0.0)
        return text


class ConvPositionEmbedding(nn.Module):
    def __init__(self, dim, kernel_size=31, groups=16):
        super().__init__()
        self.conv1d = nn.Sequential(nn.Conv1d(dim, dim, kernel_size, groups=groups, padding=kernel_size // 2), nn.Mish(),
                                    nn.Conv1d(dim, dim, kernel_size, groups=groups, padding=kernel_size // 2), nn.Mish())

    def forward(self, x):
        return self.conv1d(x.permute(0, 2, 1)).permute(0, 2, 1)


class InputEmbedding(nn.Module):
    def __init__(self, mel_dim, text_dim, out_dim):
        super().__init__()
        self.proj = nn.Linear(mel_dim * 2 + text_dim, out_dim)
        self.conv_pos_embed = ConvPositionEmbedding(dim=out_dim)

    def forward(self, x, cond, text_embed, drop_audio_cond=False):
        if drop_audio_cond:
            cond = torch.zeros_like(cond)
        x = self.proj(torch.cat((x, cond, text_embed), dim=-1))
        return self.conv_pos_embed(x) + x


class RotaryEmbedding(nn.Module):
    """x-transformers RotaryEmbedding без xpos: частоты в типе inv_freq (у модели fp16, как в f5-tts)."""

    def __init__(self, dim, base=10000):
        super().__init__()
        self.register_buffer("inv_freq", 1.0 / (base ** (torch.arange(0, dim, 2).float() / dim)))

    def forward(self, seq_len):
        t = torch.arange(seq_len, device=self.inv_freq.device)[None]
        freqs = torch.einsum("b i , j -> b i j", t.type_as(self.inv_freq), self.inv_freq) / 1.0
        return torch.stack((freqs, freqs), dim=-1).flatten(-2)   # [f0, f0, f1, f1, …]


def rotate_half(x):
    x = x.unflatten(-1, (-1, 2))
    x1, x2 = x.unbind(dim=-1)
    return torch.stack((-x2, x1), dim=-1).flatten(-2)


def apply_rotary(t, freqs):
    freqs = freqs[:, None]   # b n d → b 1 n d
    return (t * freqs.cos() + rotate_half(t) * freqs.sin()).type(t.dtype)


class Attention(nn.Module):
    def __init__(self, dim, heads, dim_head):
        super().__init__()
        self.heads = heads
        inner = dim_head * heads
        self.to_q = nn.Linear(dim, inner)
        self.to_k = nn.Linear(dim, inner)
        self.to_v = nn.Linear(dim, inner)
        self.to_out = nn.ModuleList([nn.Linear(inner, dim), nn.Identity()])   # [1] — Dropout у f5-tts

    def forward(self, x, rope):
        b = x.shape[0]
        q, k, v = self.to_q(x), self.to_k(x), self.to_v(x)
        hd = k.shape[-1] // self.heads
        q, k, v = (y.view(b, -1, self.heads, hd).transpose(1, 2) for y in (q, k, v))
        q, k = apply_rotary(q, rope), apply_rotary(k, rope)
        x = F.scaled_dot_product_attention(q, k, v, dropout_p=0.0, is_causal=False)
        x = x.transpose(1, 2).reshape(b, -1, self.heads * hd).to(q.dtype)
        return self.to_out[0](x)


class AdaLayerNorm(nn.Module):
    def __init__(self, dim, n):
        super().__init__()
        self.silu = nn.SiLU()
        self.linear = nn.Linear(dim, dim * n)
        self.norm = nn.LayerNorm(dim, elementwise_affine=False, eps=1e-6)


class DiTBlock(nn.Module):
    def __init__(self, dim, heads, dim_head, ff_mult):
        super().__init__()
        self.attn_norm = AdaLayerNorm(dim, 6)
        self.attn = Attention(dim, heads, dim_head)
        self.ff_norm = nn.LayerNorm(dim, elementwise_affine=False, eps=1e-6)
        inner = int(dim * ff_mult)
        self.ff = nn.Module()
        self.ff.ff = nn.Sequential(nn.Sequential(nn.Linear(dim, inner), nn.GELU(approximate="tanh")), nn.Identity(),
                                   nn.Linear(inner, dim))

    def forward(self, x, t, rope):
        n = self.attn_norm
        shift_msa, scale_msa, gate_msa, shift_mlp, scale_mlp, gate_mlp = torch.chunk(n.linear(n.silu(t)), 6, dim=1)
        norm = n.norm(x) * (1 + scale_msa[:, None]) + shift_msa[:, None]
        x = x + gate_msa.unsqueeze(1) * self.attn(norm, rope)
        norm = self.ff_norm(x) * (1 + scale_mlp[:, None]) + shift_mlp[:, None]
        return x + gate_mlp.unsqueeze(1) * self.ff.ff(norm)


class TimestepEmbedding(nn.Module):
    def __init__(self, dim, freq_dim=256):
        super().__init__()
        self.freq_dim = freq_dim
        self.time_mlp = nn.Sequential(nn.Linear(freq_dim, dim), nn.SiLU(), nn.Linear(dim, dim))

    def forward(self, t):
        half = self.freq_dim // 2
        emb = torch.exp(torch.arange(half, device=t.device).float() * -(math.log(10000) / (half - 1)))
        emb = 1000 * t.unsqueeze(1) * emb.unsqueeze(0)
        emb = torch.cat((emb.sin(), emb.cos()), dim=-1).to(t.dtype)
        return self.time_mlp(emb)


class DiT(nn.Module):
    def __init__(self, vocab_size, dim=1024, depth=22, heads=16, dim_head=64, ff_mult=2, text_dim=512, conv_layers=4,
                 mel_dim=N_MELS):
        super().__init__()
        self.time_embed = TimestepEmbedding(dim)
        self.text_embed = TextEmbedding(vocab_size, text_dim, conv_layers)
        self.input_embed = InputEmbedding(mel_dim, text_dim, dim)
        self.rotary_embed = RotaryEmbedding(dim_head)
        self.transformer_blocks = nn.ModuleList([DiTBlock(dim, heads, dim_head, ff_mult) for _ in range(depth)])
        self.norm_out = AdaLayerNorm(dim, 2)
        self.proj_out = nn.Linear(dim, mel_dim)

    def forward(self, x, cond, text_cond, text_uncond, time):
        """CFG одним батчем: → (pred, null). text_cond/text_uncond — text_embed(…) этой фразы."""
        t = self.time_embed(time.repeat(1))
        x = torch.cat((self.input_embed(x, cond, text_cond), self.input_embed(x, cond, text_uncond, True)), dim=0)
        t = torch.cat((t, t), dim=0)
        rope = self.rotary_embed(x.shape[1])
        for block in self.transformer_blocks:
            x = block(x, t, rope)
        n = self.norm_out
        scale, shift = torch.chunk(n.linear(n.silu(t)), 2, dim=1)
        x = n.norm(x) * (1 + scale)[:, None, :] + shift[:, None, :]
        return self.proj_out(x).chunk(2, dim=0)


class CFM(nn.Module):
    """Обёртка ради ключей весов `transformer.…`."""

    def __init__(self, vocab_size):
        super().__init__()
        self.transformer = DiT(vocab_size)


# ------------------------------------------------------------------------------------------- Vocos
class VocosBlock(nn.Module):
    def __init__(self, dim, intermediate_dim):
        super().__init__()
        self.dwconv = nn.Conv1d(dim, dim, kernel_size=7, padding=3, groups=dim)
        self.norm = nn.LayerNorm(dim, eps=1e-6)
        self.pwconv1 = nn.Linear(dim, intermediate_dim)
        self.act = nn.GELU()
        self.pwconv2 = nn.Linear(intermediate_dim, dim)
        self.gamma = nn.Parameter(torch.ones(dim))

    def forward(self, x):
        residual = x
        x = self.dwconv(x).transpose(1, 2)
        x = self.gamma * self.pwconv2(self.act(self.pwconv1(self.norm(x))))
        return residual + x.transpose(1, 2)


class Vocos(nn.Module):
    """backbone + ISTFTHead(padding="center") vocos 0.1.0; iSTFT на CPU."""

    def __init__(self, dim=512, intermediate_dim=1536, num_layers=8):
        super().__init__()
        self.backbone = nn.Module()
        bb = self.backbone
        bb.embed = nn.Conv1d(N_MELS, dim, kernel_size=7, padding=3)
        bb.norm = nn.LayerNorm(dim, eps=1e-6)
        bb.convnext = nn.ModuleList([VocosBlock(dim, intermediate_dim) for _ in range(num_layers)])
        bb.final_layer_norm = nn.LayerNorm(dim, eps=1e-6)
        self.head = nn.Module()
        self.head.out = nn.Linear(dim, N_FFT + 2)
        self.head.istft = nn.Module()
        self.head.istft.register_buffer("window", torch.hann_window(N_FFT))

    def decode(self, mel):
        """mel (1, 100, кадры) float32 → wav (1, n) float32 на CPU."""
        bb = self.backbone
        x = bb.norm(bb.embed(mel).transpose(1, 2)).transpose(1, 2)
        for block in bb.convnext:
            x = block(x)
        x = bb.final_layer_norm(x.transpose(1, 2))
        mag, p = self.head.out(x).transpose(1, 2).float().cpu().chunk(2, dim=1)
        S = torch.clip(torch.exp(mag), max=1e2) * (torch.cos(p) + 1j * torch.sin(p))
        return torch.istft(S, N_FFT, HOP, N_FFT, self.head.istft.window.cpu(), center=True)


# ------------------------------------------------------------------------------------------- загрузка и синтез
def read_vocab(path):
    """vocab.txt f5-tts: символ → номер строки (в файле CRLF — текстовый режим Python читает и так)."""
    with open(path, encoding="utf-8") as f:
        return {line[:-1]: i for i, line in enumerate(f)}


class ESpeech:
    def __init__(self, weights_dir, device="cuda"):
        self.device = torch.device(device)
        # на CPU fp16 медленный и не везде поддержан — там fp32
        self.dtype = torch.float16 if self.device.type == "cuda" else torch.float32
        self.vocab = read_vocab(os.path.join(weights_dir, "vocab.txt"))
        torch.set_default_dtype(self.dtype)   # модель сразу в нужном типе и на устройстве: без копии в fp32
        try:
            with self.device:
                cfm = CFM(len(self.vocab))
        finally:
            torch.set_default_dtype(torch.float32)
        sd = load_file(os.path.join(weights_dir, "espeech_fp16.safetensors"), device=str(self.device))
        cfm.load_state_dict({k: v.to(self.dtype) for k, v in sd.items()})
        del sd
        self.dit = cfm.transformer.to(self.dtype).eval().requires_grad_(False)
        # requires_grad_(False): иначе копии адаптеров LoRA в буферы оставляют историю autograd (утечка на смене голоса)
        self.voc = Vocos()
        self.voc.load_state_dict(load_file(os.path.join(weights_dir, "vocos.safetensors")))
        self.voc = self.voc.eval().requires_grad_(False).to(self.device)

    def text_ids(self, text):
        return torch.tensor([[self.vocab.get(c, 0) for c in text]], device=self.device)

    def timesteps(self, steps):
        t = EPSS.get(steps)
        t = (torch.tensor(t, device=self.device, dtype=self.dtype) / 32 if t else
             torch.linspace(0, 1, steps + 1, device=self.device, dtype=self.dtype))
        return t + SWAY * (torch.cos(torch.pi / 2 * t) - 1 + t)

    def cond(self, wav):
        """Образец float32 numpy/tensor (n,) 24 кГц → (мел (1, кадры, 100) на устройстве, rms)."""
        # громкость — на устройстве, как в f5-tts: на CPU сумма квадратов может разойтись в последнем бите
        audio = torch.as_tensor(wav, dtype=torch.float32)[None].to(self.device)
        rms = audio.square().mean().sqrt()
        if rms < TARGET_RMS:
            audio = audio * TARGET_RMS / rms
        return log_mel(audio.cpu()).permute(0, 2, 1).to(self.device, self.dtype), float(rms)

    @torch.inference_mode()
    def sample(self, cond, ref_len, text, frames, steps=16, seed=1, stop=None, lag=1):
        """Мел ref_len кадров образца + frames кадров фразы. text — текст образца и фразы. stop — threading.Event:
        проверяется перед каждым шагом Эйлера. lag — сколько шагов CPU может опережать видеокарту (без этого после
        остановки она ещё досчитывает 2–3 поставленных в очередь шага, samples/espeech-sentences § 6).
        → мел фразы (1, frames', 100)."""
        dit = self.dit
        ids = self.text_ids(text)
        dur = max(ref_len + frames, max(ids.shape[1], cond.shape[1]) + 1)
        step_cond = F.pad(cond, (0, 0, 0, dur - cond.shape[1]))
        text_cond = dit.text_embed(ids, dur)
        text_uncond = dit.text_embed(ids, dur, drop_text=True)
        torch.manual_seed(seed)
        x = torch.randn(1, dur, N_MELS, device=self.device, dtype=self.dtype)
        t = self.timesteps(steps)
        events = []
        for i in range(steps):
            if stop is not None and stop.is_set():
                raise Stopped()
            pred, null = dit(x, step_cond, text_cond, text_uncond, t[i])
            x = x + (t[i + 1] - t[i]) * (pred + (pred - null) * CFG)
            if lag is not None and self.device.type == "cuda":
                ev = torch.cuda.Event()
                ev.record()
                events.append(ev)
                if len(events) > lag:
                    events.pop(0).synchronize()
        if stop is not None and stop.is_set():
            raise Stopped()
        return x[:, ref_len:]

    @torch.inference_mode()
    def decode(self, mel, rms):
        wav = self.voc.decode(mel.float().permute(0, 2, 1))
        if rms < TARGET_RMS:
            wav = wav * rms / TARGET_RMS
        return wav.squeeze(0).numpy()
