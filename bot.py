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
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8663079063:AAGgB5D0kzZQhj12_pO_loeFfTn9miajKFI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AIzaSyCje5Bd2I_sCTaG8QwRLJWv0hGUGGSj3uQ")

def _url(model: str) -> str:
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={GEMINI_API_KEY}"
    )

MODELS = {
    "flash": ("gemini-3-flash-preview", "⚡ Flash 3.0  — быстро и дёшево"),
    "pro3":  ("gemini-3-pro-preview",   "🧠 Pro 3.0    — умнее"),
    "pro31": ("gemini-3.1-pro-preview", "✨ Pro 3.1    — максимум качества"),
}
DEFAULT_MODEL = "pro31"

SIZES = {
    "300_no_cap":   ("🧴 300мл без крышки",  150.0,   58.0),
    "500_no_cap":   ("🧴 500мл без крышки",  176.48,  76.48),
    "500_with_cap": ("🧴 500мл с крышкой",   179.8,   76.665),
}
DEFAULT_SIZE = "300_no_cap"

SPOT_COLORS = [("Spot_1", "#FFFFFF", "белый — Spot_1 (именованная Spot-краска)")]
CMYK_COLORS = [
    ("cmyk_cyan",    "#00AEEF", "голубой (C)"),
    ("cmyk_magenta", "#EC008C", "пурпурный (M)"),
    ("cmyk_yellow",  "#FFF200", "жёлтый (Y)"),
    ("cmyk_black",   "#000000", "чёрный (K)"),
]

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Буфер для группировки альбомов (media_group_id → список фото)
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
    lines = ["  <defs>", "    <!-- Spot_1 = белая краска (RIP читает id как имя Spot) -->"]
    for spot_id, hex_val, _ in SPOT_COLORS:
        lines.append(f'    <linearGradient id="{spot_id}"><stop offset="0" stop-color="{hex_val}"/></linearGradient>')
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
    """
    Принимает список фото (лого + флакон) и описание.
    Возвращает PNG с визуализацией готового флакона.
    """
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
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(_url("gemini-3-pro-image-preview"), json=payload)
        r.raise_for_status()
        data = r.json()
    for part in data["candidates"][0]["content"]["parts"]:
        if part.get("inlineData"):
            return base64.b64decode(part["inlineData"]["data"])
    return "Gemini не вернул изображение."

# ── Генерация SVG (режим печати) ───────────────────────────────────────────

async def generate_svg(prompt: str, model_key: str, size_key: str, images_b64: list[str] | None = None) -> str:
    model_id = MODELS[model_key][0]
    _, w, h = SIZES[size_key]

    system = f"""Ты — эксперт по SVG-макетам для профессиональной печати этикеток.
Отвечай ТОЛЬКО валидным SVG-кодом. Без пояснений. Без markdown. Без ```.
Первый символ — '<', последний — '>'.

{palette_for_prompt()}
{size_for_prompt(size_key)}

ТРЕБОВАНИЯ:
- xmlns="http://www.w3.org/2000/svg"
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
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(_url(model_id), json=payload)
        r.raise_for_status()
        data = r.json()
    raw = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    match = re.search(r"(<svg[\s\S]*?</svg>)", raw, re.IGNORECASE)
    svg_code = match.group(1) if match else raw
    return inject_spot_defs(svg_code)

# ── Отправка SVG ───────────────────────────────────────────────────────────

async def send_svg(update: Update, svg_code: str, prompt: str, size_key: str):
    size_label, w, h = SIZES[size_key]
    svg_bytes = BytesIO(svg_code.encode("utf-8"))
    await update.message.reply_document(
        document=svg_bytes,
        filename=f"label_{size_key}.svg",
        caption=f"📄 *{prompt}*\n📐 {size_label} — {w}×{h}мм\n🎨 Spot_1 (белый) + CMYK | прозрачный фон",
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            ("✅ " if k == current else "") + f"{label}  ({w}×{h}мм)",
            callback_data=f"size:{k}"
        )]
        for k, (label, w, h) in SIZES.items()
    ])

def model_keyboard(current: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("✅ " if k == current else "") + label, callback_data=f"model:{k}")]
        for k, (_, label) in MODELS.items()
    ])

# ── Команды ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sizes_text = "\n".join(f"  {label}  ({w}×{h}мм)" for _, (label, w, h) in SIZES.items())
    cmyk_text  = "\n".join(f"  `{h}`  —  {d}" for _, h, d in CMYK_COLORS)
    text = (
        "👋 Привет! Создаю макеты этикеток для флаконов.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🖼 *РЕЖИМ 1 — Визуализация для клиента (PNG):*\n"
        "Отправь альбом из 2 фото (лого + флакон) с подписью\n"
        "Пример подписи: `шампунь с ромашкой, надпись сверху`\n\n"
        "Или одно фото с подписью начинающейся на /img:\n"
        "`/img шампунь с ромашкой`\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "✏️ *РЕЖИМ 2 — SVG макет для печати:*\n"
        "Отправь фото лого с подписью `/svg описание`\n"
        "Или просто: `/svg описание` без фото\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚙️ *Настройки:*\n"
        "`/size` — размер флакона\n"
        "`/model` — модель Gemini\n\n"
        "📐 *Размеры SVG:*\n"
        f"{sizes_text}\n\n"
        "🎨 *Цвета SVG:*\n"
        "  `url(#Spot_1)`  —  белый _(Spot для станка)_\n"
        f"{cmyk_text}\n"
        "  Подложка — прозрачная"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current = ctx.user_data.get("svg_size", DEFAULT_SIZE)
    label, w, h = SIZES[current]
    await update.message.reply_text(
        f"Текущий: *{label}* ({w}×{h}мм)\n\nВыбери размер:",
        parse_mode="Markdown", reply_markup=size_keyboard(current),
    )

async def callback_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split(":")[1]
    ctx.user_data["svg_size"] = key
    label, w, h = SIZES[key]
    await query.edit_message_text(f"✅ *{label}* ({w}×{h}мм)", parse_mode="Markdown", reply_markup=size_keyboard(key))

async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current = ctx.user_data.get("svg_model", DEFAULT_MODEL)
    await update.message.reply_text(
        f"Текущая: *{MODELS[current][1]}*\n\nВыбери модель:",
        parse_mode="Markdown", reply_markup=model_keyboard(current),
    )

async def callback_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split(":")[1]
    ctx.user_data["svg_model"] = key
    await query.edit_message_text(f"✅ *{MODELS[key][1]}*", parse_mode="Markdown", reply_markup=model_keyboard(key))

# ── Обработчик альбома (несколько фото) ───────────────────────────────────

async def process_album(chat_id: int, media_group_id: str, ctx: ContextTypes.DEFAULT_TYPE, update: Update):
    """Вызывается через 1.5 сек после последнего фото альбома."""
    await asyncio.sleep(1.5)

    photos = album_buffer.pop(media_group_id, [])
    if not photos:
        return

    # Берём caption из первого сообщения альбома
    caption = photos[0].get("caption", "") or ""
    prompt  = re.sub(r"^/(img|svg)\s*", "", caption, flags=re.IGNORECASE).strip()
    if not prompt:
        prompt = "красивая этикетка для флакона"

    is_svg = caption.lower().startswith("/svg")
    file_ids = [p["file_id"] for p in photos]

    msg = await ctx.bot.send_message(chat_id, "⏳ Скачиваю фото   ")
    anim = asyncio.create_task(loading_animation(msg, "Генерирую макет"))

    try:
        images_b64 = [await photo_to_b64(ctx.bot, fid) for fid in file_ids]

        if is_svg:
            anim.cancel()
            size_key    = ctx.user_data.get("svg_size",  DEFAULT_SIZE)
            model_key   = ctx.user_data.get("svg_model", DEFAULT_MODEL)
            size_label, w, h = SIZES[size_key]
            await msg.edit_text(f"✏️ Создаю SVG макет…\n{size_label} ({w}×{h}мм)")
            svg_code = await generate_svg(prompt, model_key, size_key, images_b64)
            await msg.delete()
            # Отправляем от имени первого сообщения альбома
            class _FakeUpdate:
                def __init__(self, message):
                    self.message = message
            await send_svg(_FakeUpdate(photos[0]["message"]), svg_code, prompt, size_key)
        else:
            anim.cancel()
            await msg.edit_text("🎨 Рисую визуализацию для клиента…")
            result = await generate_mockup(prompt, images_b64)
            await msg.delete()
            if isinstance(result, bytes):
                await photos[0]["message"].reply_photo(
                    photo=BytesIO(result),
                    caption=f"🖼 *{prompt}*",
                    parse_mode="Markdown",
                )
            else:
                await photos[0]["message"].reply_text(f"⚠️ {result}")

    except httpx.HTTPStatusError as e:
        anim.cancel()
        logger.error("Gemini error: %s", e.response.text)
        await msg.edit_text("❌ Ошибка Gemini API.")
    except Exception as e:
        anim.cancel()
        logger.exception("Unexpected error in process_album")
        await msg.edit_text(f"❌ Ошибка: {e}")

    album_tasks.pop(media_group_id, None)


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Получает каждое фото — одиночное или из альбома."""
    msg    = update.message
    photo  = msg.photo[-1]
    caption = msg.caption or ""
    media_group_id = msg.media_group_id

    if media_group_id:
        # Альбом — буферизуем и ждём все фото
        album_buffer[media_group_id].append({
            "file_id": photo.file_id,
            "caption": caption,
            "message": msg,
        })
        # Отменяем предыдущую задачу если есть, запускаем новую
        if media_group_id in album_tasks:
            album_tasks[media_group_id].cancel()
        album_tasks[media_group_id] = asyncio.create_task(
            process_album(msg.chat_id, media_group_id, ctx, update)
        )
    else:
        # Одиночное фото
        prompt  = re.sub(r"^/(img|svg)\s*", "", caption, flags=re.IGNORECASE).strip()
        is_svg  = caption.lower().startswith("/svg")

        if not prompt:
            await msg.reply_text(
                "📸 Фото получено!\n\n"
                "Добавь подпись к фото:\n"
                "• `/svg описание` — SVG макет для печати\n"
                "• `/img описание` — PNG визуализация для клиента\n\n"
                "Или отправь альбом (2 фото: лого + флакон) с подписью.",
                parse_mode="Markdown",
            )
            return

        status_msg = await msg.reply_text("⏳ Скачиваю фото   ")
        anim = asyncio.create_task(loading_animation(status_msg, "Генерирую" + (" SVG" if is_svg else " визуализацию")))

        try:
            image_b64 = await photo_to_b64(ctx.bot, photo.file_id)

            if is_svg:
                anim.cancel()
                size_key  = ctx.user_data.get("svg_size",  DEFAULT_SIZE)
                model_key = ctx.user_data.get("svg_model", DEFAULT_MODEL)
                size_label, w, h = SIZES[size_key]
                await status_msg.edit_text(f"✏️ Создаю SVG…\n{size_label} ({w}×{h}мм)")
                svg_code = await generate_svg(prompt, model_key, size_key, [image_b64])
                await status_msg.delete()
                await send_svg(update, svg_code, prompt, size_key)
            else:
                anim.cancel()
                await status_msg.edit_text("🎨 Рисую визуализацию для клиента…")
                result = await generate_mockup(prompt, [image_b64])
                await status_msg.delete()
                if isinstance(result, bytes):
                    await msg.reply_photo(photo=BytesIO(result), caption=f"🖼 *{prompt}*", parse_mode="Markdown")
                else:
                    await status_msg.edit_text(f"⚠️ {result}")

        except httpx.HTTPStatusError as e:
            anim.cancel()
            logger.error("Gemini error: %s", e.response.text)
            await status_msg.edit_text("❌ Ошибка Gemini API.")
        except Exception as e:
            anim.cancel()
            logger.exception("handle_photo error")
            await status_msg.edit_text(f"❌ Ошибка: {e}")


async def handle_svg_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/svg без фото — текстовая команда."""
    prompt = " ".join(ctx.args).strip()
    if not prompt:
        await update.message.reply_text(
            "✏️ Укажи описание.\nПример: `/svg шампунь с ромашкой`\n\n"
            "Или отправь фото с подписью `/svg описание`",
            parse_mode="Markdown",
        )
        return
    size_key  = ctx.user_data.get("svg_size",  DEFAULT_SIZE)
    model_key = ctx.user_data.get("svg_model", DEFAULT_MODEL)
    size_label, w, h = SIZES[size_key]
    msg  = await update.message.reply_text(f"⏳ Генерирую SVG   ")
    anim = asyncio.create_task(loading_animation(msg, f"Создаю SVG макет {size_label}"))
    try:
        svg_code = await generate_svg(prompt, model_key, size_key)
        anim.cancel()
        await msg.delete()
        await send_svg(update, svg_code, prompt, size_key)
    except httpx.HTTPStatusError as e:
        anim.cancel()
        logger.error("Gemini error: %s", e.response.text)
        await msg.edit_text("❌ Ошибка Gemini API.")
    except Exception as e:
        anim.cancel()
        logger.exception("handle_svg_text error")
        await msg.edit_text(f"❌ Ошибка: {e}")

# ── Запуск ─────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_start))
    app.add_handler(CommandHandler("size",  cmd_size))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("svg",   handle_svg_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(callback_size,  pattern=r"^size:"))
    app.add_handler(CallbackQueryHandler(callback_model, pattern=r"^model:"))
    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()


