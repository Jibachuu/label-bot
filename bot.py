import os
import re
import logging
import httpx
from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ── Настройки ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "ВАШ_ТОКЕН_СЮДА")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "ВАШ_GEMINI_KEY_СЮДА")

def _url(model: str) -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={GEMINI_API_KEY}"
    )

# ── Модели ─────────────────────────────────────────────────────────────────
MODELS = {
    "flash": ("gemini-2.0-flash",             "⚡ Flash 2.0  — быстро и дёшево"),
    "pro25": ("gemini-2.5-pro-preview-06-05", "🧠 Pro 2.5    — умнее"),
    "pro31": ("gemini-2.5-pro-preview-06-05", "✨ Pro 3.1    — максимум качества"),
}
DEFAULT_MODEL = "pro31"

# ── Размеры макетов (мм) ───────────────────────────────────────────────────
# key → (label, width_mm, height_mm)
SIZES = {
    "300_no_cap":  ("🧴 300мл без крышки",   150.0,   58.0),
    "500_no_cap":  ("🧴 500мл без крышки",   176.48,  76.48),
    "500_with_cap":("🧴 500мл с крышкой",    179.8,   76.665),
}
DEFAULT_SIZE = "300_no_cap"

# ── Палитра ────────────────────────────────────────────────────────────────
SPOT_COLORS = [
    ("Spot_1", "#FFFFFF", "белый — Spot_1 (именованная Spot-краска)"),
]
CMYK_COLORS = [
    ("cmyk_cyan",    "#00AEEF", "голубой (C)"),
    ("cmyk_magenta", "#EC008C", "пурпурный (M)"),
    ("cmyk_yellow",  "#FFF200", "жёлтый (Y)"),
    ("cmyk_black",   "#000000", "чёрный (K)"),
]

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── SVG helpers ────────────────────────────────────────────────────────────

def build_spot_defs() -> str:
    lines = ["  <defs>",
             "    <!-- Spot_1 = белая краска (RIP читает id как имя Spot) -->"]
    for spot_id, hex_val, _ in SPOT_COLORS:
        lines.append(
            f'    <linearGradient id="{spot_id}">'
            f'<stop offset="0" stop-color="{hex_val}"/>'
            f'</linearGradient>'
        )
    lines.append("    <!-- CMYK цвета (не Spot, используются напрямую как hex) -->")
    for _, hex_val, desc in CMYK_COLORS:
        lines.append(f'    <!-- {desc}: {hex_val} -->')
    lines.append("  </defs>")
    return "\n".join(lines)


def inject_spot_defs(svg_code: str) -> str:
    defs_block = build_spot_defs()
    if re.search(r"<defs[\s>]", svg_code):
        return re.sub(r"(<defs[\s>])", defs_block + "\n  \\1", svg_code, count=1)
    return re.sub(r"(<svg[^>]*>)", r"\1\n" + defs_block, svg_code, count=1)


def palette_for_prompt() -> str:
    cmyk_list = "\n".join(
        f'  fill="{h}"  →  {d}' for _, h, d in CMYK_COLORS
    )
    return f"""ПРАВИЛА ЦВЕТОВ:
✅ Белый — ТОЛЬКО fill="url(#Spot_1)"  (Spot-краска, НЕ #ffffff)
✅ CMYK — обычные hex:
{cmyk_list}
❌ НЕ используй другие цвета, rgb(), hsl(), named colors."""


def size_for_prompt(size_key: str) -> str:
    label, w, h = SIZES[size_key]
    return (
        f"Размер макета: {w} x {h} мм ({label})\n"
        f'SVG атрибуты: width="{w}mm" height="{h}mm" '
        f'viewBox="0 0 {w} {h}"\n'
        f"Единицы координат = миллиметры (1 единица = 1 мм).\n"
        f"Подложка (фон) — ПРОЗРАЧНАЯ. НЕ рисуй background rect. "
        f"Корневой <svg> должен иметь style=\"background:transparent\" или просто без фона."
    )

# ── Генерация ──────────────────────────────────────────────────────────────

async def generate_svg(prompt: str, model_key: str, size_key: str) -> str:
    model_id = MODELS[model_key][0]
    _, w, h = SIZES[size_key]

    system = f"""Ты — эксперт по SVG-макетам для профессиональной печати этикеток на флаконы.

Отвечай ТОЛЬКО валидным SVG-кодом. Без пояснений. Без markdown. Без ```-блоков.
Первый символ — '<', последний — '>' закрывающего </svg>.

{palette_for_prompt()}

{size_for_prompt(size_key)}

ТЕХНИЧЕСКИЕ ТРЕБОВАНИЯ:
- xmlns="http://www.w3.org/2000/svg" обязателен
- width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}"
- Прозрачный фон — никакого <rect> на весь размер без fill="url(#Spot_1)" или hex-цвета
- Красивый, детализированный дизайн этикетки
- Используй <path>, <circle>, <rect>, <text>, <g> и другие SVG примитивы
- Текст — читаемый, с font-family="sans-serif" или "serif"
"""

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": f"Создай SVG-этикетку для флакона: {prompt}"}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 8192},
    }

    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(_url(model_id), json=payload)
        r.raise_for_status()
        data = r.json()

    raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    match = re.search(r"(<svg[\s\S]*?</svg>)", raw, re.IGNORECASE)
    svg_code = match.group(1) if match else raw
    return inject_spot_defs(svg_code)

# ── Клавиатуры ─────────────────────────────────────────────────────────────

def size_keyboard(current: str) -> InlineKeyboardMarkup:
    buttons = []
    for k, (label, w, h) in SIZES.items():
        prefix = "✅ " if k == current else ""
        buttons.append([InlineKeyboardButton(
            f"{prefix}{label}  ({w}×{h}мм)",
            callback_data=f"size:{k}"
        )])
    return InlineKeyboardMarkup(buttons)


def model_keyboard(current: str) -> InlineKeyboardMarkup:
    buttons = []
    for k, (_, label) in MODELS.items():
        prefix = "✅ " if k == current else ""
        buttons.append([InlineKeyboardButton(prefix + label, callback_data=f"model:{k}")])
    return InlineKeyboardMarkup(buttons)

# ── Команды ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sizes_text = "\n".join(
        f"  {label}  ({w}×{h}мм)" for _, (label, w, h) in SIZES.items()
    )
    cmyk_text = "\n".join(
        f"  `{h}`  —  {d}" for _, h, d in CMYK_COLORS
    )
    text = (
        "👋 Привет! Генерирую SVG-макеты этикеток для флаконов.\n\n"
        "✏️ *Команды:*\n"
        "`/svg <описание>` — сгенерировать макет\n"
        "`/size` — выбрать размер флакона\n"
        "`/model` — выбрать модель Gemini\n\n"
        "📐 *Размеры макетов:*\n"
        f"{sizes_text}\n\n"
        "🎨 *Цвета:*\n"
        "  `url(#Spot_1)`  —  белый _(Spot-краска для станка)_\n"
        f"{cmyk_text}\n"
        "  Подложка — прозрачная\n\n"
        "Пример: `/svg шампунь с ромашкой, нежный стиль, зелёные тона`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current = ctx.user_data.get("svg_size", DEFAULT_SIZE)
    label, w, h = SIZES[current]
    await update.message.reply_text(
        f"Текущий размер: *{label}* ({w}×{h}мм)\n\nВыбери размер флакона:",
        parse_mode="Markdown",
        reply_markup=size_keyboard(current),
    )


async def callback_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split(":")[1]
    ctx.user_data["svg_size"] = key
    label, w, h = SIZES[key]
    await query.edit_message_text(
        f"✅ Размер выбран: *{label}* ({w}×{h}мм)",
        parse_mode="Markdown",
        reply_markup=size_keyboard(key),
    )


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current = ctx.user_data.get("svg_model", DEFAULT_MODEL)
    await update.message.reply_text(
        f"Текущая модель: *{MODELS[current][1]}*\n\nВыбери модель:",
        parse_mode="Markdown",
        reply_markup=model_keyboard(current),
    )


async def callback_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split(":")[1]
    ctx.user_data["svg_model"] = key
    await query.edit_message_text(
        f"✅ Модель выбрана: *{MODELS[key][1]}*",
        parse_mode="Markdown",
        reply_markup=model_keyboard(key),
    )


async def cmd_svg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(ctx.args)
    if not prompt:
        await update.message.reply_text(
            "✏️ Укажи описание этикетки после команды.\n"
            "Пример: `/svg шампунь с ромашкой, нежный стиль, зелёные тона`\n\n"
            "Размер: /size  |  Модель: /model",
            parse_mode="Markdown",
        )
        return

    model_key = ctx.user_data.get("svg_model", DEFAULT_MODEL)
    size_key  = ctx.user_data.get("svg_size",  DEFAULT_SIZE)
    model_label = MODELS[model_key][1]
    size_label, w, h = SIZES[size_key]

    msg = await update.message.reply_text(
        f"✏️ Генерирую макет…\n"
        f"Размер: {size_label} ({w}×{h}мм)\n"
        f"Модель: {model_label}"
    )
    try:
        svg_code = await generate_svg(prompt, model_key, size_key)

        # 1. SVG файлом
        svg_bytes = BytesIO(svg_code.encode("utf-8"))
        await update.message.reply_document(
            document=svg_bytes,
            filename=f"label_{size_key}.svg",
            caption=(
                f"📄 *{prompt}*\n"
                f"📐 {size_label} — {w}×{h}мм\n"
                f"🎨 Spot_1 (белый) + CMYK | прозрачный фон"
            ),
            parse_mode="Markdown",
        )

        # 2. SVG код текстом (частями если длинный)
        intro = (
            "```xml\n"
            f"<!-- Макет: {size_label} ({w}×{h}мм) | Spot_1=белый | прозрачный фон -->\n"
        )
        outro = "\n```"
        max_body = 4096 - len(intro) - len(outro)
        chunks = [svg_code[i:i + max_body] for i in range(0, len(svg_code), max_body)]
        for i, chunk in enumerate(chunks):
            prefix = intro if i == 0 else "```xml\n"
            await update.message.reply_text(prefix + chunk + outro, parse_mode="Markdown")

        await msg.delete()

    except httpx.HTTPStatusError as e:
        logger.error("Gemini error: %s", e.response.text)
        await msg.edit_text("❌ Ошибка Gemini API. Проверь ключ или попробуй позже.")
    except Exception as e:
        logger.exception("Unexpected error in /svg")
        await msg.edit_text(f"❌ Ошибка: {e}")


# ── Запуск ─────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_start))
    app.add_handler(CommandHandler("size",  cmd_size))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("svg",   cmd_svg))
    app.add_handler(CallbackQueryHandler(callback_size,  pattern=r"^size:"))
    app.add_handler(CallbackQueryHandler(callback_model, pattern=r"^model:"))

    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
