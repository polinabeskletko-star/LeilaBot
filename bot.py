import os
import logging
import asyncio
from typing import Optional

import requests
from telegram import Update, MessageEntity
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------------------------
# ENVIRONMENT VARIABLES
# --------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY")

# Chat / user IDs (convert to int if present)
def _parse_int_env(name: str) -> Optional[int]:
    value = os.environ.get(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None

GROUP_CHAT_ID = _parse_int_env("GROUP_CHAT_ID")
OWNER_CHAT_ID = _parse_int_env("OWNER_CHAT_ID")
TARGET_USER_ID = _parse_int_env("TARGET_USER_ID")

# --------------------------------------------------------------------
# LOGGING
# --------------------------------------------------------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("LeilaBot")

# --------------------------------------------------------------------
# OPENAI CLIENT (sync, will be used via asyncio.to_thread)
# --------------------------------------------------------------------
try:
    from openai import OpenAI

    openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
except Exception as e:  # library not installed or other issue
    logger.error("OpenAI client init failed: %s", e)
    openai_client = None

LEILA_SYSTEM_PROMPT = (
    "Ты бот по имени Лейла. Ты вежливая, спокойная и поддерживающая. "
    "Отвечай кратко и по делу (до 5–6 предложений), без токсичности и грубости. "
    "Не шути про семью, внешность или здоровье. Если вопрос непонятен – уточни. "
    "Если тебя просят что-то сделать, сначала повтори задачу своими словами и предложи простой план."
)


async def generate_leila_reply(user_text: str) -> str:
    """
    Call OpenAI chat completion in a background thread so we don't block
    the asyncio event loop used by python-telegram-bot.
    """
    if not openai_client:
        return "У меня пока нет доступа к OpenAI API, попроси хозяйку проверить переменную OPENAI_API_KEY."

    def _call_openai() -> str:
        try:
            resp = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": LEILA_SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.5,
                max_tokens=350,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            logger.exception("OpenAI error")
            return f"Что-то пошло не так с OpenAI: {e}"

    return await asyncio.to_thread(_call_openai)


# --------------------------------------------------------------------
# WEATHER
# --------------------------------------------------------------------
async def get_weather_text(city: str) -> str:
    if not OPENWEATHER_API_KEY:
        return "У меня нет ключа OPENWEATHER_API_KEY. Попроси хозяйку его прописать."

    def _fetch() -> str:
        try:
            url = "https://api.openweathermap.org/data/2.5/weather"
            params = {
                "q": city,
                "appid": OPENWEATHER_API_KEY,
                "units": "metric",
                "lang": "ru",
            }
            r = requests.get(url, params=params, timeout=10)
            data = r.json()

            if r.status_code != 200:
                # OpenWeather обычно кладёт сообщение об ошибке в поле 'message'
                msg = data.get("message", "неизвестная ошибка")
                return f"Не удалось получить погоду для «{city}»: {msg}"

            name = data.get("name", city)
            main = data.get("weather", [{}])[0].get("description", "нет данных")
            temp = data.get("main", {}).get("temp")
            feels = data.get("main", {}).get("feels_like")

            return (
                f"Погода в {name}:\n"
                f"{main.capitalize()}\n"
                f"Температура: {temp} °C\n"
                f"Ощущается как: {feels} °C"
            )
        except Exception as e:
            logger.exception("Weather error")
            return f"Не получилось получить погоду: {e}"

    return await asyncio.to_thread(_fetch)


# --------------------------------------------------------------------
# HELPERS
# --------------------------------------------------------------------
def is_from_allowed_chat(update: Update) -> bool:
    """
    If GROUP_CHAT_ID is set, process group messages only from that chat.
    Private messages from OWNER_CHAT_ID (and optionally TARGET_USER_ID)
    are always allowed.
    """
    chat = update.effective_chat
    user = update.effective_user
    if not chat:
        return False

    chat_id = chat.id
    user_id = user.id if user else None

    # Private chat with owner or target user
    if chat.type == "private":
        if OWNER_CHAT_ID and user_id == OWNER_CHAT_ID:
            return True
        if TARGET_USER_ID and user_id == TARGET_USER_ID:
            return True
        # If OWNER_CHAT_ID not set, allow all private chats
        if not OWNER_CHAT_ID and not TARGET_USER_ID:
            return True
        return False

    # Group / supergroup
    if GROUP_CHAT_ID is not None:
        return chat_id == GROUP_CHAT_ID
    # If GROUP_CHAT_ID not set, allow all groups
    return True


def is_bot_addressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    In group chats Leila should answer only when:
    - the bot is mentioned (@username), or
    - message starts with 'Лейла' / 'Leila' (case-insensitive), or
    - message is a reply to a message from the bot.
    In private chats, always true.
    """
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return False

    # Private chat: always addressed
    if chat.type == "private":
        return True

    text = message.text or message.caption or ""
    text_stripped = text.strip()

    # 1) Replied to the bot
    if message.reply_to_message and message.reply_to_message.from_user:
        if message.reply_to_message.from_user.id == context.bot.id:
            return True

    # 2) Explicit mention of @username
    if message.entities:
        for ent in message.entities:
            if ent.type == MessageEntity.MENTION:
                mention = text[ent.offset : ent.offset + ent.length]
                # e.g. '@leilabot'
                if mention.lower().lstrip("@") == (context.bot.username or "").lower():
                    return True

    # 3) Starts with Leila/Лейла
    lowered = text_stripped.lower()
    if lowered.startswith("лейла") or lowered.startswith("leila"):
        return True

    return False


# --------------------------------------------------------------------
# HANDLERS
# --------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_from_allowed_chat(update):
        return

    text = (
        "Привет, я Лейла 🌸\n\n"
        "Я могу:\n"
        "• Отвечать на вопросы с помощью ИИ\n"
        "• Показывать погоду: /weather <город>\n\n"
        "В группах отвечаю только если меня упомянуть или написать «Лейла, ...»."
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_from_allowed_chat(update):
        return

    text = (
        "Команды Лейлы:\n"
        "/start – краткая информация\n"
        "/help – это сообщение\n"
        "/weather <город> – погода в городе\n\n"
        "В группе: упомяни меня или начни сообщение с «Лейла»."
    )
    await update.message.reply_text(text)


async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_from_allowed_chat(update):
        return

    if not context.args:
        await update.message.reply_text("Напиши так: /weather Москва")
        return

    city = " ".join(context.args)
    await update.message.reply_text("Секунду, узнаю погоду…")
    answer = await get_weather_text(city)
    await update.message.reply_text(answer)


async def ai_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """General text handler that sends messages to OpenAI when bot is addressed."""
    if not is_from_allowed_chat(update):
        return

    message = update.effective_message
    chat = update.effective_chat

    if not message or not message.text:
        return

    # In groups, only answer when explicitly addressed
    if chat.type in ("group", "supergroup") and not is_bot_addressed(update, context):
        return

    user_text = message.text.strip()

    # Remove bot name at the beginning if user wrote "Лейла, ..."
    lowered = user_text.lower()
    if lowered.startswith("лейла"):
        # cut first word "Лейла" and optional comma
        user_text = user_text.split(" ", 1)[1] if " " in user_text else ""
        user_text = user_text.lstrip(" ,")

    if not user_text:
        await message.reply_text("Да, я здесь. Что ты хочешь спросить?")
        return

    await message.chat.send_chat_action("typing")
    reply = await generate_leila_reply(user_text)
    await message.reply_text(reply)


async def owner_only_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Example of owner-only command:
    /say_to_group текст
    Leila will send this text to GROUP_CHAT_ID.
    """
    if OWNER_CHAT_ID and update.effective_user and update.effective_user.id != OWNER_CHAT_ID:
        return

    if not GROUP_CHAT_ID:
        await update.message.reply_text("GROUP_CHAT_ID не настроен.")
        return

    if not context.args:
        await update.message.reply_text("Напиши текст после команды, например: /say_to_group Всем привет!")
        return

    text = " ".join(context.args)
    try:
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text)
        await update.message.reply_text("Сообщение отправлено в группу.")
    except Exception as e:
        logger.exception("Broadcast failed")
        await update.message.reply_text(f"Не удалось отправить сообщение: {e}")


# --------------------------------------------------------------------
# MAIN
# --------------------------------------------------------------------
def main() -> None:
    logger.info("Starting Leila bot...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("say_to_group", owner_only_broadcast))

    # Text messages
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            ai_chat_handler,
        )
    )

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
