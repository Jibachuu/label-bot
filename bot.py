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
    "500_with_cap": ("🧴 500мл с крышкой",   76.665,   179.8),
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
    cmyk_list = "\n".join(f'  fill="{h}"  →  {d}' for
