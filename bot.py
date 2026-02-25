import os
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

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "ВАШ_ТОКЕН_СЮДА")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "ВАШ_GEMINI_KEY_СЮДА")

MODELS = {
    "flash": {
        "id": "gemini-2.5-flash",
        "label": "⚡ Gemini 2.5 Flash  — быстро и дёшево",
    },
    "pro": {
        "id": "gemini-2.5-pro",
        "label": "✨ Gemini 2.5 Pro  — умнее и мощнее",
    },
    "pro3": {
        "id": "gemini-3-pro-preview",
        "label": "🚀 Gemini 3 Pro  — максимум качества",
    },
}
DEFAULT_MODEL = "pro"

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

def api_url(model_id: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent?key={GEMINI_API_KEY}"

def model_keyboard(current: str) -> InlineKeyboardMarkup:
    buttons = []
    for k, m in MODELS.items():
        prefix = "✅ " if k == current else ""
        buttons.append([InlineKeyboardButton(prefix + m["label"], callback_data=f"model:{k}")])
    return InlineKeyboardMarkup(buttons)

# ── Отправка запроса в Gemini ──────────────────────────────────────────────

async def ask_gemini(model_id: str, history: list) -> str:
    payload = {
        "contents": history,
        "generationConfig": {"maxOutputTokens": 8192},
    }
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(api_url(model_id), json=payload)
        r.raise_for_status()
        data = r.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()

# ── Команды ────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current = ctx.user_data.get("model", DEFAULT_MODEL)
    await update.message.reply_text(
        "👋 Привет! Я Gemini внутри Telegram.\n\n"
        "Просто пиши мне — я отвечу.\n"
        "Можешь отправлять текст или фото.\n\n"
        f"Текущая модель: *{MODELS[current]['label']}*\n\n"
        "⚙️ Команды:\n"
        "`/model` — сменить модель\n"
        "`/draw <описание>` — нарисовать картинку\n"
        "`/clear` — очистить историю диалога\n"
        "`/start` — это сообщение",
        parse_mode="Markdown",
    )

async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    current = ctx.user_data.get("model", DEFAULT_MODEL)
    await update.message.reply_text(
        "Выбери модель:",
        reply_markup=model_keyboard(current),
    )

async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["history"] = []
    await update.message.reply_text("🗑 История диалога очищена.")

async def callback_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split(":")[1]
    ctx.user_data["model"] = key
    ctx.user_data["history"] = []  # сбрасываем историю при смене модели
    await query.edit_message_text(
        f"✅ Модель: *{MODELS[key]['label']}*\nИстория очищена.",
        parse_mode="Markdown",
        reply_markup=model_keyboard(key),
    )

# ── Обработка текста ───────────────────────────────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    model_key = ctx.user_data.get("model", DEFAULT_MODEL)
    model_id  = MODELS[model_key]["id"]
    history   = ctx.user_data.get("history", [])

    history.append({"role": "user", "parts": [{"text": text}]})

    msg = await update.message.reply_text("⏳")
    try:
        await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
        reply = await ask_gemini(model_id, history)
        history.append({"role": "model", "parts": [{"text": reply}]})
        ctx.user_data["history"] = history[-20:]  # храним последние 20 сообщений

        await msg.delete()

        # Разбиваем длинные ответы на части
        for i in range(0, len(reply), 4000):
            await update.message.reply_text(reply[i:i+4000])

    except httpx.HTTPStatusError as e:
        logger.error("Gemini error: %s", e.response.text)
        await msg.edit_text(f"❌ Ошибка Gemini: {e.response.status_code}")
    except Exception as e:
        logger.exception("handle_text error")
        await msg.edit_text(f"❌ Ошибка: {e}")

# ── Обработка фото ─────────────────────────────────────────────────────────

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    model_key = ctx.user_data.get("model", DEFAULT_MODEL)
    model_id  = MODELS[model_key]["id"]
    history   = ctx.user_data.get("history", [])

    caption = update.message.caption or "Опиши что на фото"

    # Скачиваем фото
    photo = update.message.photo[-1]
    file  = await ctx.bot.get_file(photo.file_id)
    buf   = BytesIO()
    await file.download_to_memory(buf)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    history.append({
        "role": "user",
        "parts": [
            {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
            {"text": caption},
        ]
    })

    msg = await update.message.reply_text("⏳")
    try:
        await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
        reply = await ask_gemini(model_id, history)
        history.append({"role": "model", "parts": [{"text": reply}]})
        ctx.user_data["history"] = history[-20:]

        await msg.delete()
        for i in range(0, len(reply), 4000):
            await update.message.reply_text(reply[i:i+4000])

    except httpx.HTTPStatusError as e:
        logger.error("Gemini error: %s", e.response.text)
        await msg.edit_text(f"❌ Ошибка Gemini: {e.response.status_code}")
    except Exception as e:
        logger.exception("handle_photo error")
        await msg.edit_text(f"❌ Ошибка: {e}")


async def handle_draw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/draw — генерация картинки через Gemini image generation."""
    # Поддерживаем и текст и фото с подписью /draw
    caption = update.message.caption or ""
    text    = update.message.text or ""
    raw     = caption if caption else text
    prompt  = raw.replace("/draw", "").strip()

    if not prompt:
        await update.message.reply_text(
            "✏️ Напиши что нарисовать.\nПример: `/draw флакон шампуня с логотипом ÉCLAT`\n\n"
            "Или отправь фото с подписью `/draw описание`",
            parse_mode="Markdown",
        )
        return

    parts = []
    # Если есть фото — добавляем его
    if update.message.photo:
        photo = update.message.photo[-1]
        file  = await ctx.bot.get_file(photo.file_id)
        buf   = BytesIO()
        await file.download_to_memory(buf)
        b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})

    parts.append({"text": prompt})

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
    }

    msg = await update.message.reply_text("🎨 Рисую...")
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(api_url("gemini-3-pro-image-preview"), json=payload)
            r.raise_for_status()
            data = r.json()

        image_bytes = None
        for part in data["candidates"][0]["content"]["parts"]:
            if part.get("inlineData"):
                image_bytes = base64.b64decode(part["inlineData"]["data"])
                break

        if image_bytes:
            await msg.delete()
            await update.message.reply_photo(
                photo=BytesIO(image_bytes),
                caption=f"🖼 {prompt}",
            )
        else:
            await msg.edit_text("⚠️ Gemini не вернул картинку. Попробуй изменить описание.")

    except httpx.HTTPStatusError as e:
        logger.error("Draw error: %s", e.response.text)
        await msg.edit_text(f"❌ Ошибка Gemini: {e.response.status_code}")
    except Exception as e:
        logger.exception("handle_draw error")
        await msg.edit_text(f"❌ Ошибка: {e}")

# ── Запуск ─────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_start))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("draw",  handle_draw))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CallbackQueryHandler(callback_model, pattern=r"^model:"))
    app.add_handler(MessageHandler(filters.PHOTO & filters.CaptionRegex(r'(?i)^/draw'), handle_draw))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
