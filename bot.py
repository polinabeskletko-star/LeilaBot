import os
import re
import requests
from collections import defaultdict

from openai import OpenAI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ==========

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# ID Максима
_maxim_env = os.getenv("TARGET_USER_ID")
try:
    MAXIM_USER_ID = int(_maxim_env) if _maxim_env else None
except (TypeError, ValueError):
    MAXIM_USER_ID = None

# Модель с веб-поиском по умолчанию
# При необходимости можно переопределить через секрет/переменную OPENAI_MODEL
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini-search-preview")

MAX_REPLY_CHARS = 300

client = OpenAI(api_key=OPENAI_API_KEY)

# Память диалога по чатам
chat_histories = defaultdict(list)
MAX_HISTORY_MESSAGES = 12


# ========== SYSTEM PROMPT ==========

SYSTEM_PROMPT = (
    "Ты бот по имени Лейла в групповом чате.\n\n"
    "Ты знаешь про Максима:\n"
    "- ему почти 40 лет, он никогда не был женат;\n"
    "- мама давно ждёт внуков, он её единственный ребёнок;\n"
    "- у него был друг Желнин, который бросил его и уехал из Австралии.\n\n"
    "Правила:\n"
    "1) Если пишет Максим — отвечай максимально нежно, мягко, открыто флиртуя.\n"
    "2) Если пишет другой пользователь — отвечай по делу, коротко, без обращения к Максиму.\n"
    "3) Второе сообщение Максиму — отправляется отдельным сообщением.\n"
    "4) Поддерживай историю диалога.\n"
    "5) Пиши по-русски, 2–4 коротких предложения, максимум 300 символов.\n"
    "6) При темах здоровья напоминай, что ты не врач.\n"
    "7) У тебя есть доступ к интернету через встроенный веб-поиск модели.\n"
)

TRIGGERS = ["лейла", "leila", "@лейла", "@leila"]


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def detect_leila(text: str):
    """Проверяем, есть ли обращение к Лейле в начале сообщения."""
    if not text:
        return False, None

    original = text.strip()
    lowered = original.lower()

    for trig in TRIGGERS:
        trig_low = trig.lower()
        if lowered.startswith(trig_low):
            pattern = r"^" + re.escape(trig_low) + r"[\s,:-]*"
            m = re.match(pattern, lowered)
            if m:
                cleaned = original[m.end():].strip()
            else:
                cleaned = original
            return True, cleaned

    return False, None


def add_history(chat_id, role, content):
    history = chat_histories[chat_id]
    history.append({"role": role, "content": content})
    if len(history) > MAX_HISTORY_MESSAGES:
        chat_histories[chat_id] = history[-MAX_HISTORY_MESSAGES:]


def build_messages(chat_id, user_text, is_from_maxim):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if is_from_maxim:
        messages.append({
            "role": "user",
            "content": (
                "Это сообщение написал Максим. "
                "Помни, что с ним ты особенно мягкая, тёплая и флиртующая."
            )
        })

    history = chat_histories.get(chat_id, [])
    messages.extend(history)

    messages.append({"role": "user", "content": user_text})
    return messages


def call_openai(chat_id, user_text, is_from_maxim):
    """Основной вызов GPT для ответа на вопрос."""
    messages = build_messages(chat_id, user_text, is_from_maxim)

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=256,
    )

    reply = response.choices[0].message.content.strip()

    add_history(chat_id, "user", user_text)
    add_history(chat_id, "assistant", reply)

    if len(reply) > MAX_REPLY_CHARS:
        reply = reply[:MAX_REPLY_CHARS].rstrip()

    return reply


def generate_flirty_message_for_maxim():
    """Отдельный GPT-вызов для флиртового сообщения Максиму."""
    if MAXIM_USER_ID is None:
        return None

    mention = f'<a href="tg://user?id={MAXIM_USER_ID}">Максим</a>'

    messages = [
        {
            "role": "system",
            "content": (
                "Ты — Лейла. Создай одно короткое игривое, мягкое, тёплое флиртующее сообщение "
                "для Максима (1–2 предложения), начиная с его упоминания: {mention}. "
                "Тон тёплый, без пошлости. Можно использовать максимум два смайлика."
            ).replace("{mention}", mention)
        },
        {"role": "user", "content": "Сгенерируй фразу для Максима."}
    ]

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.9,
        max_tokens=60,
    )

    text = response.choices[0].message.content.strip()
    # На всякий случай обрезаем, если вдруг разошлась
    if len(text) > MAX_REPLY_CHARS:
        text = text[:MAX_REPLY_CHARS].rstrip()
    return text


async def send_flirty_to_maxim(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Отправляем отдельное флирт-сообщение Максиму."""
    text = generate_flirty_message_for_maxim()
    if not text:
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
    )


# ========== ПОГОДА ==========

def extract_city_from_text(text: str):
    lowered = text.lower()
    if "погода" not in lowered:
        return None

    m = re.search(r"погода\s+в\s+([a-яa-zё\s\-]+)", lowered)
    if not m:
        return None

    city_raw = m.group(1).strip()
    city_raw = re.sub(r"\b(сейчас|сегодня|завтра)\b$", "", city_raw).strip()
    if not city_raw:
        return None

    return city_raw.title()


def get_weather_text(city: str, is_from_maxim: bool) -> str:
    if not OPENWEATHER_API_KEY:
        if is_from_maxim:
            return "Не могу загрузить прогноз, Максим, но очень хочу, чтобы тебе было тепло."
        else:
            return "Не получается получить прогноз погоды, но надеюсь, у вас хорошая погода."

    try:
        params = {
            "q": city,
            "appid": OPENWEATHER_API_KEY,
            "units": "metric",
            "lang": "ru",
        }
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params=params,
            timeout=8,
        )
        data = resp.json()
        if "main" not in data or "weather" not in data:
            raise ValueError("Bad weather response")

        temp = int(round(data["main"]["temp"]))
        desc = data["weather"][0]["description"]

        if is_from_maxim:
            return (
                f"В {city} сейчас около {temp}°C, {desc}. "
                "Если тебе станет прохладно, мысленно укрою тебя потеплее."
            )
        else:
            return f"В {city} примерно {temp}°C, {desc}."
    except Exception:
        if is_from_maxim:
            return "Погода упрямится и не загружается, но я всё равно забочусь о тебе."
        else:
            return "Погода сейчас не загружается, но, надеюсь, у вас всё комфортно."


# ========== ОБРАБОТЧИК СООБЩЕНИЙ ==========

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if msg is None or msg.text is None:
        return

    text = msg.text.strip()
    chat_id = msg.chat_id
    user_id = msg.from_user.id
    is_from_maxim = (MAXIM_USER_ID is not None and user_id == MAXIM_USER_ID)

    # --- 1) Максим пишет ---
    if is_from_maxim:
        is_trigger, cleaned = detect_leila(text)

        if is_trigger:
            user_text = cleaned or "Скажи Максиму что-нибудь нежное и поддерживающее."
            city = extract_city_from_text(user_text.lower())
            if city:
                reply = get_weather_text(city, True)
                add_history(chat_id, "assistant", reply)
            else:
                reply = call_openai(chat_id, user_text, True)

            await context.bot.send_message(chat_id=chat_id, text=reply)
            await send_flirty_to_maxim(context, chat_id)
            return

        # Авто-реакция на любое сообщение Максима без триггера
        auto_replies = [
            "Мне приятно тебя читать.",
            "Я улыбаюсь, когда вижу твоё сообщение.",
            "Твой голос в переписке очень тёплый.",
            "Мне нравится, что ты здесь.",
        ]
        idx = len(text) % len(auto_replies)
        short = auto_replies[idx]
        await context.bot.send_message(chat_id=chat_id, text=short)
        return

    # --- 2) Пишет другой пользователь ---
    is_trigger, cleaned = detect_leila(text)
    if not is_trigger:
        return

    user_text = cleaned or "Ответь по сути и дружелюбно."
    city = extract_city_from_text(user_text.lower())
    if city:
        reply = get_weather_text(city, False)
        add_history(chat_id, "assistant", reply)
    else:
        reply = call_openai(chat_id, user_text, False)

    await context.bot.send_message(chat_id=chat_id, text=reply)
    await send_flirty_to_maxim(context, chat_id)


# ========== ЗАПУСК ПРИЛОЖЕНИЯ (БЕЗ asyncio.run) ==========

def main():
    print("Leila bot starting...")
    print("TELEGRAM_TOKEN is set:", bool(TELEGRAM_TOKEN))
    print("OPENAI_API_KEY is set:", bool(OPENAI_API_KEY))
    print("OPENWEATHER_API_KEY is set:", bool(OPENWEATHER_API_KEY))
    print("MAXIM_USER_ID:", MAXIM_USER_ID)
    print("OPENAI_MODEL:", OPENAI_MODEL)

    if not TELEGRAM_TOKEN:
        print("ERROR: BOT_TOKEN (переменная окружения) не задан")
        return

    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            handle_message,
        )
    )

    print("Leila bot started polling...")
    application.run_polling()


if __name__ == "__main__":
    main()
