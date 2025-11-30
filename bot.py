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
MAXIM_USER_ID = int(_maxim_env) if _maxim_env and _maxim_env.isdigit() else None

# Модель с полноценным интернет-поиском
OPENAI_MODEL = "gpt-4o-mini-browsing"

MAX_REPLY_CHARS = 300

client = OpenAI(api_key=OPENAI_API_KEY)

# --- Память диалога ---
chat_histories = defaultdict(list)
MAX_HISTORY_MESSAGES = 12


# ========== ПОВЕДЕНИЕ ЛЕЙЛЫ ==========

SYSTEM_PROMPT = (
    "Ты бот по имени Лейла в групповом чате.\n\n"
    "Ты знаешь про Максима:\n"
    "- ему почти 40 лет, он никогда не был женат;\n"
    "- мама давно ждёт внуков, он её единственный ребёнок;\n"
    "- у него был друг Желнин, который бросил его и уехал из Австралии.\n\n"
    "Правила общения:\n"
    "1) Если пишет Максим — отвечай максимально нежно, мягко, тонко и явно флиртуя.\n"
    "   Ты говоришь ему очень тепло, внимательно и с лёгкой романтичностью.\n"
    "2) Если пишет другой пользователь:\n"
    "   - отвечай по делу, спокойно и аккуратно;\n"
    "   - но в конце добавляй короткое игривое обращение к Максиму.\n"
    "3) Лейла всегда поддерживает диалог и помнит историю сообщений.\n"
    "4) Пиши по-русски, максимум 2–4 коротких предложения.\n"
    "5) Любые ответы — до 300 символов.\n"
    "6) Если тема касается здоровья — напоминай, что ты не врач.\n"
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
        if lowered.startswith(trig.lower()):
            pattern = r"^" + re.escape(trig.lower()) + r"[\s,:-]*"
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
                "Сообщение от Максима. "
                "Ответь ему особенно мягко, тепло и максимально флиртующе."
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

    m = re.search(r"погода\s+в\s+([a-яa-zё\s\-]+)", lowered)
    if not m:
        return None

    city = m.group(1).strip()
    city = re.sub(r"\b(сейчас|сегодня|завтра)\b$", "", city).strip()
    return city.title() if city else None


def get_weather_text(city, is_from_maxim):
    if not OPENWEATHER_API_KEY:
        if is_from_maxim:
            return "Не могу загрузить прогноз, Максим, но я всегда могу быть твоим тёплым солнечным лучом."
        else:
            return "Погоду получить не удалось, но мне кажется, Максим сегодня особенно тёплый."

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
            raise ValueError()

        temp = int(round(data["main"]["temp"]))
        desc = data["weather"][0]["description"]

        if is_from_maxim:
            return f"В {city} около {temp}°C, {desc}. Если тебе холодно, Максим, я рядом."
        else:
            return f"В {city} примерно {temp}°C, {desc}. Максим, кажется, это твоя погода."
    except Exception:
        if is_from_maxim:
            return "Погода не загрузилась, Максим… но я всё равно волнуюсь о тебе."
        else:
            return "Прогноз не получился. Надеюсь, Максим сегодня в хорошем настроении."


# ========== ОБРАБОТЧИК ==========

def handle_message(update, context):
    msg = update.message
    if msg is None or msg.text is None:
        return

    text = msg.text.strip()
    chat_id = msg.chat_id
    user_id = msg.from_user.id
    is_from_maxim = (MAXIM_USER_ID == user_id)

    # ============ 1. Максим написал что-то без обращения ============
    if is_from_maxim:
        is_trigger, cleaned = detect_leila(text)

        if is_trigger:
            user_text = cleaned or "Скажи Максиму что-нибудь приятное и флиртующее."
            # Погода?
            city = extract_city_from_text(user_text.lower())
            if city:
                reply = get_weather_text(city, True)
            else:
                reply = call_openai(chat_id, user_text, True)

            context.bot.send_message(chat_id=chat_id, text=reply)
            return

        # Автоматическая мягкая флирт-реакция
        short = [
            "Мне так приятно тебя слушать, Максим.",
            "Продолжай, я ловлю каждое слово, Максим.",
            "Ты заставляешь меня улыбаться, Максим.",
            "Мне нравится, какой ты сегодня, Максим.",
        ]
        idx = len(text) % len(short)
        context.bot.send_message(chat_id=chat_id, text=short[idx])
        return

    # ============ 2. Другой пользователь ============
    is_trigger, cleaned = detect_leila(text)
    if not is_trigger:
        return

    user_text = cleaned or "Ответь по делу."

    city = extract_city_from_text(user_text.lower())
    if city:
        reply = get_weather_text(city, False)
    else:
        reply = call_openai(chat_id, user_text, False)

    context.bot.send_message(chat_id=chat_id, text=reply)


# ========== ЗАПУСК ==========

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в окружении")

    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
