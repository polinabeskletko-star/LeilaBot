import os
import re
import asyncio
from datetime import datetime, time, date
from collections import defaultdict
from typing import Dict, List, Tuple, Optional, Any

import pytz
import httpx
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ========== НАСТРОЙКИ И ENV ==========

TOKEN = os.environ.get("BOT_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY")

# Чат, куда Лейла пишет (где Макс и компания)
GROUP_CHAT_ID_ENV = os.environ.get("GROUP_CHAT_ID")  # например "-1001234567890"
GROUP_CHAT_ID: Optional[int] = None
if GROUP_CHAT_ID_ENV:
    try:
        GROUP_CHAT_ID = int(GROUP_CHAT_ID_ENV)
    except ValueError:
        GROUP_CHAT_ID = None

# Telegram user ID Максима
TARGET_USER_ID_ENV = os.environ.get("TARGET_USER_ID", "0")
try:
    TARGET_USER_ID = int(TARGET_USER_ID_ENV)
except ValueError:
    TARGET_USER_ID = 0

# Админ (например, ты)
ADMIN_CHAT_ID_ENV = os.environ.get("ADMIN_CHAT_ID")
ADMIN_CHAT_ID: Optional[int] = None
if ADMIN_CHAT_ID_ENV:
    try:
        ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_ENV)
    except ValueError:
        ADMIN_CHAT_ID = None

TIMEZONE = os.environ.get("BOT_TZ", "Australia/Brisbane")

client: Optional[OpenAI] = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)

# ---------- ГЛОБАЛЬНОЕ СОСТОЯНИЕ ----------

# История диалогов для поддержки контекста: (chat_id, user_id) -> list[{"role": "...", "content": "..."}]
dialog_history: Dict[Tuple[int, int], List[Dict[str, str]]] = defaultdict(list)


# ---------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ----------

def get_tz() -> pytz.BaseTzInfo:
    return pytz.timezone(TIMEZONE)


async def log_to_admin(context: ContextTypes.DEFAULT_TYPE, message: str):
    if ADMIN_CHAT_ID is None:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=message)
    except Exception as e:
        print("Failed to send admin log:", e)


async def call_openai_chat(
    messages: List[Dict[str, str]],
    max_tokens: int = 200,
    temperature: float = 0.8,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Обёртка над OpenAI chat.completions.
    Возвращает (text, error_message).
    """
    if client is None:
        return None, "OpenAI client is not configured (no API key)."

    try:
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=OPENAI_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        text = resp.choices[0].message.content.strip()
        return text, None
    except Exception as e:
        err = f"Error calling OpenAI: {e}"
        print(err)
        return None, err


# ---------- ПОГОДА (как у Самуила) ----------

async def fetch_weather_for_city(city_query: str) -> Optional[Dict[str, Any]]:
    """
    Получить погоду из OpenWeather по названию города.
    Возвращает словарь:
      {city, country, temp, feels_like, humidity, description}
    или None, если не удалось.
    """
    if not OPENWEATHER_API_KEY:
        print("No OPENWEATHER_API_KEY configured")
        return None

    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "q": city_query,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "lang": "ru",
    }

    try:
        async with httpx.AsyncClient(timeout=10) as http_client:
            resp = await http_client.get(url, params=params)
        if resp.status_code != 200:
            print(f"OpenWeather error for '{city_query}': {resp.status_code} {resp.text}")
            return None
        data = resp.json()
        main = data.get("main", {})
        weather_list = data.get("weather", [])
        weather_desc = weather_list[0]["description"] if weather_list else "без описания"

        result = {
            "city": data.get("name", city_query),
            "country": data.get("sys", {}).get("country", ""),
            "temp": main.get("temp"),
            "feels_like": main.get("feels_like"),
            "humidity": main.get("humidity"),
            "description": weather_desc,
        }
        return result
    except Exception as e:
        print("Error fetching weather:", e)
        return None


def detect_weather_city_from_text(text: str) -> Optional[str]:
    """
    Пытаемся понять, для какого города просят погоду.
    Сначала проверяем явные кейсы (Брисбен, Калуга),
    потом ищем паттерн 'в <город>'.
    """
    t = text.lower()

    if "калуге" in t or "калуга" in t or "kaluga" in t:
        return "Kaluga,ru"
    if "брисбене" in t or "брисбен" in t or "brisbane" in t:
        return "Brisbane,au"

    m = re.search(r"\bв\s+([A-Za-zА-Яа-я\-]+)", t)
    if m:
        city_raw = m.group(1)
        return city_raw

    return None


def format_weather_for_prompt(info: Dict[str, Any]) -> str:
    parts = []
    city = info.get("city")
    country = info.get("country")
    temp = info.get("temp")
    feels = info.get("feels_like")
    hum = info.get("humidity")
    desc = info.get("description")

    if city:
        if country:
            parts.append(f"{city}, {country}")
        else:
            parts.append(str(city))
    if desc:
        parts.append(f"сейчас {desc}")
    if temp is not None:
        parts.append(f"температура около {round(temp)}°C")
    if feels is not None:
        parts.append(f"ощущается как {round(feels)}°C")
    if hum is not None:
        parts.append(f"влажность примерно {hum}%")

    return ", ".join(parts)


# ---------- ПЕРСОНА ЛЕЙЛЫ И ОТВЕТЫ ----------

MAX_QA_TOKENS = 400


def build_leila_system_prompt(
    include_maxim_context: bool,
) -> str:
    base = (
        "Ты — телеграм-бот по имени «Лейла».\n"
        "Ты отвечаешь по-русски, на «ты». Стиль: тёплый, игривый, немного флиртующий, "
        "но без пошлости. Можно слегка подшучивать, но мягко и бережно.\n"
        "Пиши живо, естественно, не занудствуй, не злоупотребляй эмодзи.\n"
    )

    maxim_ctx = (
        "\nОтдельный контекст про Максима:\n"
        "Максиму почти 40, он до сих пор не был женат, мама ждёт внуков, а он у неё один. "
        "Он любит считать себя гениальным, иногда драматизирует, ищет молодую девушку "
        "и вообще много думает о себе. Его можно мягко и с любовью подкалывать.\n"
    )

    if include_maxim_context:
        return base + maxim_ctx
    return base


async def generate_leila_answer(
    now: datetime,
    chat_id: int,
    user_id: int,
    user_display_name: str,
    user_text: str,
    weather_info: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Генерация ответа Лейлы с учётом:
    - времени/дня недели,
    - конкретного пользователя (Максим или нет),
    - истории диалога,
    - опционально данных о погоде.
    """
    weekday = now.weekday()
    weekday_names = [
        "понедельник", "вторник", "среда",
        "четверг", "пятница", "суббота", "воскресенье",
    ]
    weekday_name = weekday_names[weekday]
    time_str = now.strftime("%H:%M")

    # Максим — это именно TARGET_USER_ID, а не любой участник чата
    is_maxim = TARGET_USER_ID and (user_id == TARGET_USER_ID)

    system_prompt = build_leila_system_prompt(include_maxim_context=is_maxim)

    extra_context_parts = [
        f"Сегодня {weekday_name}, время {time_str}.",
        f"С тобой сейчас переписывается пользователь по имени {user_display_name}.",
        "Обращайся к собеседнику на «ты».",
    ]

    if is_maxim:
        extra_context_parts.append(
            "Это именно тот самый Максим, про которого у тебя есть отдельный контекст. "
            "Можешь время от времени обращаться к нему по имени «Максим»."
        )
    else:
        extra_context_parts.append(
            "Это не Максим. Не называй его Максимом. "
            f"Если хочешь обратиться по имени — используй имя {user_display_name}."
        )

    if weather_info is not None:
        weather_str = format_weather_for_prompt(weather_info)
        extra_context_parts.append(
            f"У тебя есть реальные данные о погоде: {weather_str}. "
            "Если собеседник спрашивает о погоде, опирайся именно на эти данные, ничего не выдумывай."
        )

    extra_context = " ".join(extra_context_parts)

    key = (chat_id, user_id)
    history = dialog_history.get(key, [])

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": extra_context},
    ]

    # Добавляем кусок истории диалога (последние ~10 сообщений)
    if history:
        trimmed = history[-10:]
        messages.extend(trimmed)

    # Текущее сообщение пользователя
    messages.append({"role": "user", "content": user_text})

    text, err = await call_openai_chat(messages, max_tokens=MAX_QA_TOKENS, temperature=0.9)

    if text is not None:
        # Обновляем историю
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": text})
        # ограничиваем историю, чтобы не раздувалась
        if len(history) > 40:
            dialog_history[key] = history[-40:]
        else:
            dialog_history[key] = history

    return text, err


# ---------- HANDLERS ДЛЯ КОМАНД ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_type = update.effective_chat.type
    if chat_type == "private":
        await update.message.reply_text(
            "Привет, я Лейла 💫\n"
            "Могу мило поболтать, поддержать и немного пофлиртовать.\n"
            "В группе отвечаю, когда меня зовут по имени."
        )
    else:
        await update.message.reply_text(
            "Я Лейла. В этом чате отвечаю, когда меня зовут по имени."
        )


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"Your user ID: `{user.id}`\nUsername: @{user.username}",
        parse_mode="Markdown",
    )


# ---------- ОСНОВНОЙ MESSAGE HANDLER ----------

def is_direct_call_to_leila(text: str, bot_username: Optional[str]) -> bool:
    t = text.lower()
    if "лейла" in t or "леила" in t or "лейля" in t:
        return True
    if bot_username and bot_username.lower() in t:
        return True
    return False


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if message is None:
        return

    if not message.text:
        return

    chat = message.chat
    user = message.from_user
    text = message.text.strip()
    text_lower = text.lower()

    chat_id = chat.id
    user_id = user.id

    # Если указан конкретный групповой чат — в других группах молчим
    if chat.type != "private" and GROUP_CHAT_ID is not None and chat_id != GROUP_CHAT_ID:
        return

    print(
        f"[LEILA DEBUG] chat_id={chat_id} chat_type={chat.type} "
        f"user_id={user_id} user_name={user.username} text='{text}'"
    )

    bot_username = context.bot.username

    # В личке — всегда считаем, что обращаются к Лейле
    if chat.type == "private":
        direct_call = True
    else:
        direct_call = is_direct_call_to_leila(text, bot_username)

    if not direct_call:
        # В группе, если не звали по имени — молчим
        return

    # Определяем, как называть собеседника
    if user_id == TARGET_USER_ID:
        display_name = "Максим"
    else:
        display_name = user.first_name or user.username or "друг"

    # Проверяем, спрашивали ли про погоду
    weather_info: Optional[Dict[str, Any]] = None
    if "погод" in text_lower or "температур" in text_lower or "градус" in text_lower:
        city_query = detect_weather_city_from_text(text)
        if city_query:
            weather_info = await fetch_weather_for_city(city_query)

    tz = get_tz()
    now = datetime.now(tz)

    ai_text, err = await generate_leila_answer(
        now=now,
        chat_id=chat_id,
        user_id=user_id,
        user_display_name=display_name,
        user_text=text,
        weather_info=weather_info,
    )

    if ai_text is None:
        fallback = "Кажется, у меня сейчас маленький сбой. Попробуешь спросить ещё раз чуть позже?"
        print(f"OpenAI error in Leila handle_message: {err}")
        await message.reply_text(fallback)
        return

    await message.reply_text(ai_text)


# ---------- РАСПИСАНИЕ ДЛЯ МАКСИМА (УТРО / ВЕЧЕР) ----------

async def leila_good_morning_job(context: ContextTypes.DEFAULT_TYPE):
    """
    В 08:30 — игривое пожелание доброго утра и хорошего дня Максиму.
    """
    if GROUP_CHAT_ID is None or TARGET_USER_ID == 0:
        return

    tz = get_tz()
    now = datetime.now(tz)

    system_prompt = build_leila_system_prompt(include_maxim_context=True)
    user_prompt = (
        "Сделай короткое (1–3 предложения) пожелание доброго утра и хорошего дня Максиму "
        "от имени Лейлы. Стиль: тёплый, игривый, немного флиртующий. "
        "Можно мягко подколоть его отношение к утрам, привычки или планы, "
        "но в целом настроение — поддерживающее и мотивирующее."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    text, err = await call_openai_chat(messages, max_tokens=150, temperature=0.9)
    if text is None:
        print(f"OpenAI error for Leila good morning: {err}")
        return

    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=text,
        )
        print(f"[Leila good morning] Sent at {now}")
    except Exception as e:
        print("Error sending Leila good morning message:", e)


async def leila_good_night_job(context: ContextTypes.DEFAULT_TYPE):
    """
    В 21:10 — пожелание спокойной ночи Максиму.
    """
    if GROUP_CHAT_ID is None or TARGET_USER_ID == 0:
        return

    tz = get_tz()
    now = datetime.now(tz)

    system_prompt = build_leila_system_prompt(include_maxim_context=True)
    user_prompt = (
        "Сделай короткое (1–3 предложения) пожелание спокойной ночи Максиму "
        "от имени Лейлы. Стиль: нежный, немного флиртующий, можно чуть подшутить "
        "над его вечерними привычками или мыслями о своей гениальности, "
        "но общее ощущение — тёплое и расслабляющее, чтобы ему было приятно ложиться спать."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    text, err = await call_openai_chat(messages, max_tokens=150, temperature=0.9)
    if text is None:
        print(f"OpenAI error for Leila good night: {err}")
        return

    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=text,
        )
        print(f"[Leila good night] Sent at {now}")
    except Exception as e:
        print("Error sending Leila good night message:", e)


# ---------- MAIN ----------

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is not set in environment variables!")

    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))

    # Общий обработчик текстовых сообщений (и в личке, и в группе)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message,
        )
    )

    # Планирование задач
    job_queue = app.job_queue
    tz = get_tz()
    now = datetime.now(tz)

    print(f"[Leila] Local time now: {now} [{TIMEZONE}]. Scheduling daily jobs.")

    # Защита от дубликатов — удаляем существующие задачи с теми же именами
    for name in ["leila_good_morning", "leila_good_night"]:
        jobs = job_queue.get_jobs_by_name(name)
        if jobs:
            print(f"[Leila] Removing existing jobs for name={name}: {len(jobs)} шт.")
        for job in jobs:
            job.schedule_removal()

    # Утреннее сообщение в 08:30
    job_queue.run_daily(
        leila_good_morning_job,
        time=time(8, 30, tzinfo=tz),
        name="leila_good_morning",
    )

    # Вечернее сообщение в 21:10
    job_queue.run_daily(
        leila_good_night_job,
        time=time(21, 10, tzinfo=tz),
        name="leila_good_night",
    )

    print("[Leila] Bot started and jobs scheduled...")
    app.run_polling()


if __name__ == "__main__":
    main()