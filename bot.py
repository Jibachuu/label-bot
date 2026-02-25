import os
import re
import base64
import logging
import asyncio
import httpx
from io import BytesIO
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ── Настройки ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

def _url(model: str) -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={GEMINI_API_KEY}"
    )

SIZES = {
    "300_no_cap":   ("🧴 300мл без крышки",  58.0,   150.0),
    "500_no_cap":   ("🧴 500мл без крышки",  76.48,  176.48),
    "500_with_cap": ("🧴 500мл с крышкой",   76.665, 179.8),
}
DEFAULT_SIZE = "500_with_cap"

SPOT_COLORS = [("Spot_1", "#FFFFFF", "белый — Spot_1 (именованная Spot-краска)")]
CMYK_COLORS = [
    ("cmyk_cyan",    "#00AEEF", "голубой (C)"),
    ("cmyk_magenta", "#EC008C", "пурпурный (M)"),
    ("cmyk_yellow",  "#FFF200", "жёлтый (Y)"),
    ("cmyk_black",   "#000000", "чёрный (K)"),
]

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Буфер для группировки альбомов
album_buffer: dict[str, list] = defaultdict(list)
album_tasks:  dict[str, asyncio.Task] = {}

# ── Анимация загрузки ──────────────────────────────────────────────────────

async def loading_animation(msg, text: str):
    """Мигающие точки пока идёт генерация."""
    dots = ["   ", ".  ", ".. ", "..."]
    i = 0
    while True:
        try:
            await msg.edit_text(f"⏳ {text}{dots[i % 4]}")
            i += 1
            await asyncio.sleep(1.5)
        except asyncio.CancelledError:
            break
        except Exception:
            break

# ── SVG helpers ────────────────────────────────────────────────────────────

def build_spot_defs() -> str:
    lines = ["  <defs>", "    "]
    for spot_id, hex_val, _ in SPOT_COLORS:
        lines.append(f'    <linearGradient id="{spot_id}"><stop offset="0" stop-color="{hex_val}"/></linearGradient>')
    for _, hex_val, desc in CMYK_COLORS:
        lines.append(f'    ')
    lines.append("  </defs>")
    return "\n".join(lines)

def inject_spot_defs(svg_code: str) -> str:
    defs_block = build_spot_defs()
    if re.search(r"<defs[\s>]", svg_code):
        return re.sub(r"(<defs[\s>])", defs_block + "\n  \\1", svg_code, count=1)
    return re.sub(r"(<svg[^>]*>)", r"\1\n" + defs_block, svg_code, count=1)

def palette_for_prompt() -> str:
    cmyk_list = "\n".join(f'  fill="{h}"  →  {d}' for _, h, d in CMYK_COLORS)
    return f"""ПРАВИЛА ЦВЕТОВ:
✅ Белый — ТОЛЬКО fill="url(#Spot_1)"
✅ CMYK hex:
{cmyk_list}
❌ НЕ используй rgb(), hsl(), named colors."""

def size_for_prompt(size_key: str) -> str:
    label, w, h = SIZES[size_key]
    return (
        f"Размер: {w}x{h}мм ({label})\n"
        f'width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}"\n'
        f"Подложка ПРОЗРАЧНАЯ — никакого background rect."
    )

# ── Скачивание фото ────────────────────────────────────────────────────────

async def photo_to_b64(bot, file_id: str) -> str:
    file = await bot.get_file(file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

# ── Генерация PNG мокапа (режим визуализации) ──────────────────────────────

async def generate_mockup(prompt: str, images_b64: list[str]) -> bytes | str:
    parts = []
    labels = ["логотип/брендинг", "флакон"]
    for i, b64 in enumerate(images_b64):
        label = labels[i] if i < len(labels) else f"фото {i+1}"
        parts.append({"text": f"[{label}]:"})
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})

    parts.append({"text": (
        f"Создай реалистичный PNG мокап флакона с этикеткой. "
        f"Используй предоставленные фото: логотип нанеси на флакон. "
        f"Описание: {prompt}. "
        f"Результат — красивая рекламная визуализация для презентации клиенту."
    )})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    async with httpx.AsyncClient(timeout=900) as client:
        # Для фото используем Flash, чтобы работало моментально и без перегрузок серверов
        r = await client.post(_url("gemini-3-flash-preview"), json=payload)
        r.raise_for_status()
        data = r.json()
    for part in data["candidates"][0]["content"]["parts"]:
        if part.get("inlineData"):
            return base64.b64decode(part["inlineData"]["data"])
    return "Gemini не вернул изображение."

# ── Генерация SVG (режим печати) ───────────────────────────────────────────

async def generate_svg(prompt: str, size_key: str, images_b64: list[str] | None = None) -> str:
    # Для макетов жестко фиксируем самую мощную модель Pro 3.0
    model_id = "gemini-3-pro-preview"
    _, w, h = SIZES[size_key]

    system = f"""Ты — эксперт по SVG-макетам для профессиональной печати этикеток.
Отвечай ТОЛЬКО валидным SVG-кодом. Без пояснений. Без markdown. Без ```.
Первый символ — '<', последний — '>'.

{palette_for_prompt()}
{size_for_prompt(size_key)}

ТРЕБОВАНИЯ:
- xmlns="[http://www.w3.org/2000/svg](http://www.w3.org/2000/svg)"
- width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}"
- Прозрачный фон
- font-family="sans-serif"
"""
    parts = []
    if images_b64:
        labels = ["логотип/брендинг", "флакон для референса"]
        for i, b64 in enumerate(images_b64):
            label = labels[i] if i < len(labels) else f"фото {i+1}"
            parts.append({"text": f"[{label}]:"})
            parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})
        parts.append({"text": f"Создай SVG-этикетку используя логотип с фото. Описание: {prompt}"})
    else:
        parts.append({"text": f"Создай SVG-этикетку: {prompt}"})

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": parts}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192},
    }
    async with httpx.AsyncClient(timeout=900) as client:
        r = await client.post(_url(model_id), json=payload)
        r.raise_for_status()
        data = r.
