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

# Буфер
