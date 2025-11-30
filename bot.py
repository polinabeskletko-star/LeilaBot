import os
import re
import requests
from collections import defaultdict

from openai import OpenAI
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

# ========== ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ==========

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# ID Максима
_maxim_env = os.getenv("TARGET_USER_ID")
try:
    MAXIM_USER_ID = int(_maxim_env) if _maxim_env else None
except ValueError:
    MAXIM_USER_ID = None

# Модель с веб-поиском (важно!)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini-search-preview")

MAX_REPLY_CHARS = 300

client = OpenAI(api_key=OPENAI_API_KEY)

# Память историй
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
    "2) Если пишет другой пользователь — отвечай по делу, коротко, БЕЗ обращения к Максиму.\n"
    "3) Второе сообщение Максиму — отдельным сообщением (всегда генерируется ИИ).\n"
    "4) Поддерживай историю диалога.\n"
    "5) Пиши по-русски, 2–4 коротких предложения, максимум 300 символов.\n"
    "6) При темах здоровья напоминай, что ты не врач.\n"
    "7) У тебя есть доступ к интернету через встроенный веб-поиск модели.\n"
)

TRIGGERS = ["лейла", "leila", "@лейла", "@leila"]


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def detect_leila(text: str):
    if not text:
        return False, None

    original = text.strip()
    lowered = original.lower()

    for trig in TRIGGERS:
        if lowered.startswith(trig):
            pattern = r"^" + re.escape(trig) + r"[\s,:-]*"
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
            "content": "Это сообщение написал Максим. Помни — с ним ты очень мягкая и флиртующая."
        })

    history = chat_histories.get(chat_id, [])
    messages.extend(history)

    messages.append({"role": "user", "content": user_text})
    return messages


def call_openai(chat_id, user_text, is_from_maxim):
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


# ========== ГЕНЕРАЦИЯ ФЛИРТА ДЛЯ МАКСИМА ==========

def generate_flirty_message_for_maxim():
    if MAXIM_USER_ID is None:
        return None

    mention = f'<a href="tg://user?id={MAXIM_USER_ID}">Максим</a>'

    messages = [
        {
            "role": "system",
            "content": (
                "Ты — Лейла. Создай одно короткое игривое, мягкое флиртующее сообщение "
                "для Максима (1–2 предложения), начиная с его упоминания: {mention}. "
                "Тон тёплый, без пошлости. Можешь использовать максимум два смайлика."
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

    return response.choices[0].message.content.strip()


async def send_flirty_to_maxim(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    text = generate_flirty_message_for_maxim()
    if not text:
        return
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML"
    )


# ========== ПОГОДА ==========

def extract_city_from_text(text):
    lowered = text.lower()
    if "погода" not in lowered:
        return None
    m = re.search(r"погода\s+в\s+([a-яa-zё\s\-]+)", lowered)
    if not m:
        return None
    city = m.group(1).strip()
    city = re.sub(r"\b(сейчас|сегодня|завтра)\b$", "", city).strip()
    return city.title() if city else None


def get_weather_text(city, is_from_maxim):
    if not OPENWEATHER_API_KEY:
        return (
            "Не могу загрузить прогноз, Максим, но надеюсь, тебе тепло."
            if is_from_maxim else
            "Погода не загрузилась."
        )
    try:
        params = {
            "q": city,
            "appid": OPENWEATHER_API_KEY,
            "units": "metric",
            "lang": "ru"
        }
        resp = requests.get("https://api.openweathermap.org/data/2.5/weather",
                            params=params, timeout=8)
        data = resp.json()
        temp = int(round(data["main"]["temp"]))
        desc = data["weather"][0]["description"]
        if is_from_maxim:
            return f"В {city} сейчас {temp}°C, {desc}. Если станет прохладно, я мысленно укрою тебя потеплее."
        return f"В {city} около {temp}°C, {desc}."
    except Exception:
        return "Не выходит получить погоду сейчас."


# ========== ОБРАБОТЧИК СООБЩЕНИЙ ==========

async def handle_message(update, context):
    msg = update.message
    if not msg or not msg.text:
        return

    text = msg.text.strip()
    chat_id = msg.chat_id
    user_id = msg.from_user.id
    is_from_maxim = (MAXIM_USER_ID is not None and user_id == MAXIM_USER_ID)

    # === Если пишет Максим ===
    if is_from_maxim:
        is_trigger, cleaned = detect_leila(text)

        if is_trigger:
            user_text = cleaned or "Скажи Максиму что-нибудь нежное."
            city = extract_city_from_text(user_text.lower())
            if city:
                reply = get_weather_text(city, True)
                add_history(chat_id, "assistant", reply)
            else:
                reply = call_openai(chat_id, user_text, True)

            await context.bot.send_message(chat_id=chat_id, text=reply)
            await send_flirty_to_maxim(context, chat_id)
            return

        # --- Автоматическая реакция на любое сообщение Максима ---
        auto_replies = [
            "Мне приятно тебя читать.",
            "Твой голос в чате звучит особенно мягко.",
            "Я невольно улыбаюсь, когда вижу твоё сообщение.",
            "Мне нравится, когда ты пишешь.",
        ]
        idx = len(text) % len(auto_replies)
        short = auto_replies[idx]
        await context.bot.send_message(chat_id=chat_id, text=short)
        return

    # === Если пишет кто-то другой ===
    is_trigger, cleaned = detect_leila(text)
    if not is_trigger:
        return

    user_text = cleaned or "Ответь по существу."
    city = extract_city_from_text(user_text.lower())
    if city:
        reply = get_weather_text(city, False)
        add_history(chat_id, "assistant", reply)
    else:
        reply = call_openai(chat_id, user_text, False)

    await context.bot.send_message(chat_id=chat_id, text=reply)

    # Второе сообщение — только Максиму
    await send_flirty_to_maxim(context, chat_id)


# ========== ЗАПУСК ПРИЛОЖЕНИЯ ==========

async def main():
    print("Starting Leila…")
    print("BOT_TOKEN:", bool(TELEGRAM_TOKEN))
    print("OPENAI_API_KEY:", bool(OPENAI_API_KEY))
    print("OPENWEATHER_API_KEY:", bool(OPENWEATHER_API_KEY))
    print("MAXIM_USER_ID:", MAXIM_USER_ID)

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await app.run_polling()


if __name__ == "__main__":
    import asyncio
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
