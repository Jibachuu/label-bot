import os
import re
import base64
import logging
import httpx
from io import BytesIO

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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8663079063:AAGgB5D0kzZQhj12_pO_loeFfTn9miajKFI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyCje5Bd2I_sCTaG8QwRLJWv0hGUGGSj3uQ")

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
SIZES = {
    "300_no_cap":   ("🧴 300мл без крышки",  150.0,   58.0),
    "500_no_cap":   ("🧴 500мл без крышки",  176.48,  76.48),
    "500_with_cap": ("🧴 500мл с крышкой",   179.8,   76.665),
}
DEFAULT_SIZE = "300_no_cap"

# ── Палитра ────────────────────────────────────────────────────────────────
SPOT_COLORS = [("Spot_1", "#FFFFFF", "белый — Spot_1 (именованная Spot-краска)")]
CMYK_COLORS = [
    ("cmyk_cyan",    "#00AEEF", "голубой (C)"),
    ("cmyk_magenta", "#EC008C", "пурпурный (M)"),
    ("cmyk_yellow",  "#FFF200", "жёлтый (Y)"),
    ("cmyk_black",   "#000000", "чёрный (K)"),
]

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ── SVG helpers ────────────────────────────────────────────────────────────

def build_spot_defs() -> str:
    lines = ["  <defs>", "    <!-- Spot_1 = белая краска (RIP читает id как имя Spot) -->"]
    for spot_id, hex_val, _ in SPOT_COLORS:
        lines.append(f'    <linearGradient id="{spot_id}"><stop offset="0" stop-color="{hex_val}"/></linearGradient>')
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
    cmyk_list = "\n".join(f'  fill="{h}"  →  {d}' for _, h, d in CMYK_COLORS)
    return f"""ПРАВИЛА ЦВЕТОВ:
✅ Белый — ТОЛЬКО fill="url(#Spot_1)"  (Spot-краска, НЕ #ffffff)
✅ CMYK — обычные hex:
{cmyk_list}
❌ НЕ используй другие цвета, rgb(), hsl(), named colors."""

def size_for_prompt(size_key: str) -> str:
    label, w, h = SIZES[size_key]
    return (
        f"Размер макета: {w} x {h} мм ({label})\n"
        f'SVG атрибуты: width="{w}mm" height="{h}mm" viewBox="0 0 {w} {h}"\n'
        f"Единицы координат = миллиметры.\n"
        f"Подложка — ПРОЗРАЧНАЯ. НЕ рисуй background rect на весь размер."
    )

# ── Скачивание фото ────────────────────────────────────────────────────────

async def download_photo_base64(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    photo = update.message.photo
    if not photo:
        return None
    file = await ctx.bot.get_file(photo[-1].file_id)
    buf = BytesIO()
    await file.download_to_memory(buf)
    return base64.b64encode(buf.getvalue()).decode("utf-8")

# ── Генерация SVG ──────────────────────────────────────────────────────────

async def generate_svg(prompt: str, model_key: str, size_key: str, image_b64: str | None = None) -> str:
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
- Прозрачный фон — никакого <rect> на весь размер
- Красивый, детализированный дизайн этикетки
- Используй <path>, <circle>, <rect>, <text>, <g> и другие SVG примитивы
- Текст — читаемый, font-family="sans-serif"
"""

    user_parts = []
    if image_b64:
        user_parts.append({"inline_data": {"mime_type": "image/jpeg", "data": image_b64}})
        user_parts.append({"text": (
            f"На фото — логотип или изображение брендинга. "
            f"Точно воспроизведи его в SVG макете этикетки для флакона. "
            f"Описание стиля: {prompt}"
        )})
    else:
        user_parts.append({"text": f"Создай SVG-этикетку для флакона: {prompt}"})

    payload = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": user_parts}],
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

# ── Генерация PNG ──────────────────────────────────────────────────────────

async def generate_image(prompt: str) -> bytes | str:
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(_url("gemini-2.0-flash-preview-image-generation"), json=payload)
        r.raise_for_status()
        data = r.json()
    for part in data["candidates"][0]["content"]["parts"]:
        if part.get("inlineData"):
            return base64.b64decode(part["inlineData"]["data"])
    return "Gemini не вернул изображение. Попробуй изменить описание."

# ── Отправка SVG ───────────────────────────────────────────────────────────

async def send_svg(update: Update, svg_code: str, prompt: str, size_key: str, model_label: str):
    size_label, w, h = SIZES[size_key]
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
    intro = "```xml\n" + f"<!-- {size_label} ({w}×{h}мм) | Spot_1=белый | прозрачный фон -->\n"
    outro = "\n```"
    max_body = 4096 - len(intro) - len(outro)
    chunks = [svg_code[i:i + max_body] for i in range(0, len(svg_code), max_body)]
    for i, chunk in enumerate(chunks):
        prefix = intro if i == 0 else "```xml\n"
        await update.message.reply_text(prefix + chunk + outro, parse_mode="Markdown")

# ── Клавиатуры ─────────────────────────────────────────────────────────────

def size_keyboard(current: str) -> InlineKeyboardMarkup:
    buttons = []
    for k, (label, w, h) in SIZES.items():
        prefix = "✅ " if k == current else ""
        buttons.append([InlineKeyboardButton(f"{prefix}{label}  ({w}×{h}мм)", callback_data=f"size:{k}")])
    return InlineKeyboardMarkup(buttons)

def model_keyboard(current: str) -> InlineKeyboardMarkup:
    buttons = []
    for k, (_, label) in MODELS.items():
        prefix = "✅ " if k == current else ""
        buttons.append([InlineKeyboardButton(prefix + label, callback_data=f"model:{k}")])
    return InlineKeyboardMarkup(buttons)

# ── Команды ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sizes_text = "\n".join(f"  {label}  ({w}×{h}мм)" for _, (label, w, h) in SIZES.items())
    cmyk_text  = "\n".join(f"  `{h}`  —  {d}" for _, h, d in CMYK_COLORS)
    text = (
        "👋 Привет! Создаю макеты этикеток для флаконов.\n\n"
        "🖼 *Картинка PNG:*\n"
        "`/img <описание>` — сгенерировать изображение\n\n"
        "✏️ *SVG мокап без логотипа:*\n"
        "`/svg <описание>` — векторный макет со Spot-цветами\n\n"
        "📸 *SVG мокап с твоим логотипом:*\n"
        "Прикрепи фото + напиши `/svg <описание>` в подписи\n"
        "Пример: отправь фото логотипа, в подписи напиши\n"
        "`/svg шампунь с ромашкой, зелёные тона`\n\n"
        "⚙️ *Настройки:*\n"
        "`/size` — размер флакона\n"
        "`/model` — модель Gemini\n\n"
        "📐 *Размеры:*\n"
        f"{sizes_text}\n\n"
        "🎨 *Цвета SVG:*\n"
        "  `url(#Spot_1)`  —  белый _(Spot-краска для станка)_\n"
        f"{cmyk_text}\n"
        "  Подложка — прозрачная"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current = ctx.user_data.get("svg_size", DEFAULT_SIZE)
    label, w, h = SIZES[current]
    await update.message.reply_text(
        f"Текущий размер: *{label}* ({w}×{h}мм)\n\nВыбери размер флакона:",
        parse_mode="Markdown", reply_markup=size_keyboard(current),
    )

async def callback_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split(":")[1]
    ctx.user_data["svg_size"] = key
    label, w, h = SIZES[key]
    await query.edit_message_text(
        f"✅ Размер выбран: *{label}* ({w}×{h}мм)",
        parse_mode="Markdown", reply_markup=size_keyboard(key),
    )

async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current = ctx.user_data.get("svg_model", DEFAULT_MODEL)
    await update.message.reply_text(
        f"Текущая модель: *{MODELS[current][1]}*\n\nВыбери модель:",
        parse_mode="Markdown", reply_markup=model_keyboard(current),
    )

async def callback_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split(":")[1]
    ctx.user_data["svg_model"] = key
    await query.edit_message_text(
        f"✅ Модель выбрана: *{MODELS[key][1]}*",
        parse_mode="Markdown", reply_markup=model_keyboard(key),
    )

async def cmd_img(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(ctx.args)
    if not prompt:
        await update.message.reply_text(
            "✏️ Укажи описание.\nПример: `/img шампунь с ромашкой`",
            parse_mode="Markdown",
        )
        return
    msg = await update.message.reply_text("🎨 Генерирую картинку...")
    try:
        result = await generate_image(prompt)
        if isinstance(result, bytes):
            await update.message.reply_photo(photo=BytesIO(result), caption=f"🖼 *{prompt}*", parse_mode="Markdown")
            await msg.delete()
        else:
            await msg.edit_text(f"⚠️ {result}")
    except httpx.HTTPStatusError as e:
        logger.error("Gemini image error: %s", e.response.text)
        await msg.edit_text("❌ Ошибка Gemini API.")
    except Exception as e:
        logger.exception("Unexpected error in /img")
        await msg.edit_text(f"❌ Ошибка: {e}")

# ── /svg — работает и с фото и без ────────────────────────────────────────

async def handle_svg(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает две ситуации:
    1. /svg описание                     — обычное текстовое сообщение с командой
    2. фото + подпись "/svg описание"    — фото с командой в caption
    """
    # Получаем текст — либо из caption фото, либо из текста сообщения
    raw_text = update.message.caption or update.message.text or ""

    # Убираем команду /svg из начала
    prompt = re.sub(r"^/svg\s*", "", raw_text, flags=re.IGNORECASE).strip()
    if not prompt:
        await update.message.reply_text(
            "✏️ Укажи описание после команды.\n"
            "Пример: `/svg шампунь с ромашкой, зелёные тона`\n\n"
            "Или отправь фото с подписью `/svg описание`",
            parse_mode="Markdown",
        )
        return

    model_key   = ctx.user_data.get("svg_model", DEFAULT_MODEL)
    size_key    = ctx.user_data.get("svg_size",  DEFAULT_SIZE)
    model_label = MODELS[model_key][1]
    size_label, w, h = SIZES[size_key]

    # Есть ли фото в сообщении?
    has_photo = bool(update.message.photo)
    photo_note = " + логотип 📸" if has_photo else ""

    msg = await update.message.reply_text(
        f"✏️ Генерирую SVG{photo_note}…\n{size_label} ({w}×{h}мм) | {model_label}"
    )
    try:
        image_b64 = await download_photo_base64(update, ctx) if has_photo else None
        svg_code  = await generate_svg(prompt, model_key, size_key, image_b64)
        await send_svg(update, svg_code, prompt, size_key, model_label)
        await msg.delete()
    except httpx.HTTPStatusError as e:
        logger.error("Gemini error: %s", e.response.text)
        await msg.edit_text("❌ Ошибка Gemini API.")
    except Exception as e:
        logger.exception("Unexpected error in handle_svg")
        await msg.edit_text(f"❌ Ошибка: {e}")

# ── Запуск ─────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_start))
    app.add_handler(CommandHandler("size",  cmd_size))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("img",   cmd_img))

    # /svg как текст
    app.add_handler(CommandHandler("svg", handle_svg))

    # фото с подписью /svg ... — Telegram передаёт как photo + caption
    app.add_handler(MessageHandler(
        filters.PHOTO & filters.CaptionRegex(r"(?i)^/svg"),
        handle_svg
    ))

    app.add_handler(CallbackQueryHandler(callback_size,  pattern=r"^size:"))
    app.add_handler(CallbackQueryHandler(callback_model, pattern=r"^model:"))

    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()


