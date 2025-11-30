import os
import re
import requests
from collections import defaultdict

from openai import OpenAI
from telegram.ext import Updater, MessageHandler, Filters

# ========== НАСТРОЙКИ ==========

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# ID Максима (из TARGET_USER_ID)
_maxim_env = os.getenv("TARGET_USER_ID")
try:
    MAXIM_USER_ID = int(_maxim_env) if _maxim_env else None
except ValueError:
    MAXIM_USER_ID = None

# Модель — по умолчанию с браузингом, но можно переопределить через переменную
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini-browsing")

MAX_REPLY_CHARS = 300

client = OpenAI(api_key=OPENAI_API_KEY)

# Память диалога по чатам
chat_histories = defaultdict(list)
MAX_HISTORY_MESSAGES = 12


# ========== ПОВЕДЕНИЕ ЛЕЙЛЫ ==========

SYSTEM_PROMPT = (
    "Ты бот по имени Лейла в групповом чате.\n\n"
    "Ты знаешь про Максима:\n"
    "- ему почти 40 лет, он никогда не был женат;\n"
    "- мама давно ждёт внуков, он её единственный ребёнок;\n"
    "- у него был друг Желнин, который бросил его и уехал из Австралии.\n\n"
    "Правила:\n"
    "1) Если пишет Максим — отвечай максимально нежно, мягко и открыто флиртуя.\n"
    "2) Если пишет другой пользователь — отвечай по делу, спокойно; "
    "в конце можешь добавить одну короткую игривую фразу к Максиму.\n"
    "3) Используй историю сообщений для поддержки диалога.\n"
    "4) Пиши по-русски, 2–4 коротких предложения, максимум 300 символов.\n"
    "5) При темах здоровья напоминай, что ты не врач.\n"
    "6) У тебя есть доступ к интернету через встроенный веб-поиск модели.\n"
)

TRIGGERS = ["лейла", "leila", "@лейла", "@leila"]


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def detect_leila(text):
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


# ========== ПОГОДА ==========

def extract_city_from_text(text):
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


def get_weather_text(city, is_from_maxim):
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
            return "В %s примерно %d°C, %s. Кажется, это погода, в которую Максиму стоит немного прогуляться." % (
                city, temp, desc
            )
    except Exception:
        if is_from_maxim:
            return "Не получилось загрузить погоду, Максим, но я всё равно забочусь о тебе."
        else:
            return "Погода не загрузилась, но я надеюсь, что у Максима сегодня тёплый день."


# ========== ОБРАБОТЧИК СООБЩЕНИЙ ==========

def handle_message(update, context):
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
            user_text = cleaned or "Скажи Максиму что-нибудь приятное и флиртующее."
            city = extract_city_from_text(user_text.lower())
            if city:
                reply = get_weather_text(city, True)
                add_history(chat_id, "user", user_text)
                add_history(chat_id, "assistant", reply)
            else:
                reply = call_openai(chat_id, user_text, True)

            context.bot.send_message(chat_id=chat_id, text=reply)
            return

        # Авто-реакция на любое сообщение Максима
        short_replies = [
            "Мне очень приятно тебя читать, Максим.",
            "Продолжай, Максим, мне важно, что ты чувствуешь.",
            "Ты вызываешь у меня тёплую улыбку, Максим.",
            "Мне нравится твой тон сегодня, Максим.",
        ]
        idx = len(text) % len(short_replies)
        reply = short_replies[idx]
        context.bot.send_message(chat_id=chat_id, text=reply)
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

    context.bot.send_message(chat_id=chat_id, text=reply)


# ========== ЗАПУСК ==========

def main():
    if not TELEGRAM_TOKEN:
        # Не кидаем исключение с трейсбеком, а просто печатаем и выходим
        print("ERROR: BOT_TOKEN (переменная окружения) не задан")
        return

    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
