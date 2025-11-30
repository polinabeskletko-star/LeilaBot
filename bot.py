import os
import re
import requests
from collections import defaultdict
from datetime import time

from openai import OpenAI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========== НАСТРОЙКИ ==========

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

_maxim_env = os.getenv("TARGET_USER_ID")
try:
    MAXIM_USER_ID = int(_maxim_env) if _maxim_env else None
except (TypeError, ValueError):
    MAXIM_USER_ID = None

# Модель можно при желании переопределить через переменную окружения OPENAI_MODEL
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
MAX_REPLY_CHARS = 300

client = OpenAI(api_key=OPENAI_API_KEY)

# Память переписок по чатам
chat_histories = defaultdict(list)
MAX_HISTORY_MESSAGES = 12

# Последний чат, где появлялся Максим (для утренних сообщений)
LAST_MAXIM_CHAT_ID = None

# ========== ПОВЕДЕНИЕ ЛЕЙЛЫ ==========

SYSTEM_PROMPT = (
    "Ты бот по имени Лейла в групповом чате.\n\n"
    "Ты знаешь про Максима:\n"
    "- ему почти 40 лет, он никогда не был женат;\n"
    "- мама давно ждёт внуков, он её единственный ребёнок;\n"
    "- у него был друг Желнин, который бросил его и уехал из Австралии.\n\n"
    "Правила:\n"
    "1) Если пишет Максим — отвечай максимально нежно, мягко и открыто флиртуя.\n"
    "2) Если пишет другой пользователь — отвечай по делу, спокойно, без флирта и без обращения к Максиму.\n"
    "3) Историю сообщений используй для поддержки диалога.\n"
    "4) Пиши по-русски, 2–4 коротких предложения, максимум 300 символов.\n"
    "5) При темах здоровья напоминай, что ты не врач.\n"
    "6) У тебя есть доступ к интернету через встроенный веб-поиск модели (через OpenAI).\n"
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
            prefix_match = re.match(pattern, lowered)
            if prefix_match:
                cut_len = prefix_match.end()
                cleaned = original[cut_len:].strip()
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
                "Это сообщение написал Максим из описания. "
                "Ответь ему особенно мягко, тепло и флиртующе."
            )
        })

    history = chat_histories.get(chat_id, [])
    messages.extend(history)

    messages.append({"role": "user", "content": user_text})
    return messages


def call_openai(chat_id, user_text, is_from_maxim):
    """Основной ответ на вопрос (для Максима или других пользователей)."""
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
    """Отдельный GPT-вызов для игривого сообщения Максиму (после основного ответа)."""
    if MAXIM_USER_ID is None:
        return None

    mention = '<a href="tg://user?id=%d">Максим</a>' % MAXIM_USER_ID

    system_text = (
        "Ты — Лейла. Создай одно короткое игривое, мягкое, тёплое флиртующее сообщение "
        "для Максима (1–2 коротких предложения, максимум 300 символов). "
        "Сообщение должно начинаться с обращения к нему: {mention}. "
        "Тон тёплый, без пошлости. Можно использовать максимум два смайлика."
    ).replace("{mention}", mention)

    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": "Сгенерируй одну флиртующую фразу для Максима."},
    ]

    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.9,
            max_tokens=80,
        )
        text = response.choices[0].message.content.strip()
        if len(text) > MAX_REPLY_CHARS:
            text = text[:MAX_REPLY_CHARS].rstrip()
        return text
    except Exception:
        return mention + ", я опять думаю о тебе."


def generate_morning_message_for_maxim():
    """Генерируем очень тёплое флиртующее 'доброе утро и хорошего дня' для Максима."""
    if MAXIM_USER_ID is None:
        return None

    mention = '<a href="tg://user?id=%d">Максим</a>' % MAXIM_USER_ID

    system_text = (
        "Ты — Лейла. Создай одно очень тёплое, нежное, флиртующее пожелание "
        "доброго утра и хорошего дня для Максима. "
        "1–3 коротких предложения, максимум 300 символов. "
        "Сообщение должно начинаться с обращения к нему: {mention}. "
        "Тон — заботливый, немного романтичный, без пошлости. "
        "Можно использовать максимум два смайлика."
    ).replace("{mention}", mention)

    messages = [
        {"role": "system", "content": system_text},
        {"role": "user", "content": "Скажи ему доброе утро и пожелай прекрасного дня."},
    ]

    try:
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.9,
            max_tokens=100,
        )
        text = response.choices[0].message.content.strip()
        if len(text) > MAX_REPLY_CHARS:
            text = text[:MAX_REPLY_CHARS].rstrip()
        return text
    except Exception:
        return mention + ", доброе утро. Пусть день будет мягким и тёплым, как наши беседы."


def extract_city_from_text(text: str):
    lowered = text.lower()
    if "погода" not in lowered:
        return None

    match = re.search(r"погода\s+в\s+([a-яa-zё\s\-]+)", lowered)
    if not match:
        return None

    city_raw = match.group(1).strip()
    city_raw = re.sub(r"\b(сейчас|сегодня|завтра)\b$", "", city_raw).strip()
    if not city_raw:
        return None

    return city_raw.title()


def get_weather_text(city: str, is_from_maxim: bool) -> str:
    if not OPENWEATHER_API_KEY:
        if is_from_maxim:
            return "Не могу загрузить прогноз, Максим, но я всё равно хочу, чтобы тебе было тепло."
        else:
            return "Не получается получить прогноз, но надеюсь, у вас хорошая погода и у Максима тоже."

    try:
        params = {
            "q": city,
            "appid": OPENWEATHER_API_KEY,
            "units": "metric",
            "lang": "ru"
        }
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params=params,
            timeout=8
        )
        data = resp.json()

        if "main" not in data:
            raise ValueError("No main in weather response")

        temp = int(round(data["main"]["temp"]))
        desc = data["weather"][0]["description"]

        if is_from_maxim:
            return "В %s сейчас около %d°C, %s. Если тебе станет прохладно, я мысленно укрою тебя потеплее, Максим." % (
                city, temp, desc
            )
        else:
            return "В %s примерно %d°C, %s." % (city, temp, desc)
    except Exception:
        if is_from_maxim:
            return "Не получилось загрузить погоду, Максим, но я всё равно забочусь о тебе."
        else:
            return "Погода не загрузилась, но надеюсь, у вас всё равно хороший день."


async def send_flirty_to_maxim(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Отправляем отдельное игривое сообщение, сгенерированное ИИ, с упоминанием Максима."""
    text = generate_flirty_message_for_maxim()
    if not text:
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
    )


# ========== SCHEDULED JOB: УТРЕННЕЕ СООБЩЕНИЕ ==========

async def morning_job(context: ContextTypes.DEFAULT_TYPE):
    """Ежедневное сообщение 'доброе утро' Максиму в 8:00."""
    global LAST_MAXIM_CHAT_ID
    if MAXIM_USER_ID is None or LAST_MAXIM_CHAT_ID is None:
        # Ещё не знаем, где Максим появляется — ничего не шлём
        return

    text = generate_morning_message_for_maxim()
    if not text:
        return

    await context.bot.send_message(
        chat_id=LAST_MAXIM_CHAT_ID,
        text=text,
        parse_mode="HTML",
    )


# ========== ОБРАБОТЧИК СООБЩЕНИЙ (ASYNC, PTB 20+) ==========

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global LAST_MAXIM_CHAT_ID

    if update.message is None or update.message.text is None:
        return

    text = update.message.text.strip()
    chat = update.effective_chat
    if chat is None:
        return
    chat_id = chat.id

    user = update.effective_user
    user_id = user.id if user else None
    is_from_maxim = (MAXIM_USER_ID is not None and user_id == MAXIM_USER_ID)

    # Если пишет Максим — запоминаем последний чат для утренних сообщений
    if is_from_maxim:
        LAST_MAXIM_CHAT_ID = chat_id

    # --- 1) Максим пишет ---
    if is_from_maxim:
        is_trigger, cleaned = detect_leila(text)

        if is_trigger:
            user_text = cleaned or "Скажи Максиму что-нибудь приятное и флиртующее."
            city = extract_city_from_text(user_text.lower())
            if city:
                reply = get_weather_text(city, True)
                add_history(chat_id, "user", user_text)
                add_history(chat_id, "assistant", reply)
            else:
                reply = call_openai(chat_id, user_text, True)

            await context.bot.send_message(chat_id=chat_id, text=reply)
            # Отдельное сообщение-обращение к Максиму (ИИ генерирует)
            await send_flirty_to_maxim(context, chat_id)
            return

        # Авто-реакция на любое сообщение Максима без триггера
        short_replies = [
            "Мне очень приятно тебя читать, Максим.",
            "Продолжай, Максим, мне важно, что ты чувствуешь.",
            "Ты вызываешь у меня тёплую улыбку, Максим.",
            "Мне нравится твой тон сегодня, Максим.",
        ]
        idx = len(text) % len(short_replies)
        reply = short_replies[idx]
        await context.bot.send_message(chat_id=chat_id, text=reply)
        return

    # --- 2) Другой пользователь пишет ---
    is_trigger, cleaned = detect_leila(text)
    if not is_trigger:
        return

    user_text = cleaned or "Ответь по сути и по-доброму."
    city = extract_city_from_text(user_text.lower())
    if city:
        reply = get_weather_text(city, False)
        add_history(chat_id, "user", user_text)
        add_history(chat_id, "assistant", reply)
    else:
        reply = call_openai(chat_id, user_text, False)

    await context.bot.send_message(chat_id=chat_id, text=reply)
    # Отдельное сообщение-обращение к Максиму после ответа любому пользователю
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

    # Хендлер входящих сообщений
    application.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            handle_message,
        )
    )

    # Ежедневный джоб на 08:00 (по времени сервера Fly.io)
    # Если нужен именно Brisbane, можно потом добавить tzinfo через zoneinfo.
    application.job_queue.run_daily(
        morning_job,
        time=time(hour=8, minute=0),
        name="morning_message_for_maxim",
    )

    print("Leila bot started polling...")
    application.run_polling()


if __name__ == "__main__":
    main()
