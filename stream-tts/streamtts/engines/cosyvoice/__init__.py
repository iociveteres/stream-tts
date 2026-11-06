"""Движок CosyVoice3: cosyvoice-server из cosyvoice.cpp (его код не наш). Описание, запуск сервера
(launch.py), клиент (client.py) и особенности редактора голоса (editor.py)."""
from .client import Client
from .editor import Maker
from .launch import BACKENDS as DEVICES, Launcher, find_server

ID = "cosyvoice"
TITLE = "CosyVoice3"
VOICE_EXT = ".gguf"   # голос: voices/<имя>.gguf или voices/<набор>/<имя>.gguf

# --- загрузки (install.py) ---
# Модель — с Hugging Face, квант на выбор (scripts/download-model.sh); ревизия, размеры и sha256 — в
# downloads.json (model)
REPO = "Lourdle/Fun-CosyVoice3-0.5B-2512-GGUF"
# Видеопамять cosyvoice-server на CUDA, МБ — обычно и на самых длинных образцах (замеры на RTX 3060 Ti, 2026-10-17;
# на Vulkan пик примерно на 0,5 ГБ больше). На 54 фразах (6 голосов × 3 seed) разборчивость по Whisper и
# сходство CAM++ у всех трёх в пределах шума. Ниже Q5 не проверяли, F16 не лучше Q8_0 и вдвое больше.
# Примеры звука — static/samples/cosyvoice-<квант>.mp3 (одна фраза, seed 42) — для выбора на вкладке «Загрузки». Голос
# ohf-denis — из датасета ru_RU-denis (OHF-Voice/voice-datasets, CC0), чтобы примеры можно было распространять;
# как он собран — docs/stream-tts-deploy.md, «Первый запуск».
QUANTS = {
    "Q8_0": dict(vram=1500, vram_max=1900),
    "Q6_K": dict(vram=1450, vram_max=1800),
    "Q5_K_M": dict(vram=1300, vram_max=1700),
}
DEFAULT_QUANT = "Q8_0"  # рекомендация автора модели
FRONTEND = ("frontend-onnx/campplus.int8.onnx", "frontend-onnx/speech_tokenizer_v3.int8.onnx")


def model_name(quant):
    return f"CosyVoice3-2512_{quant}.gguf"
