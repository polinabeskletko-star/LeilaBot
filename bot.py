import os
import re
from openai import OpenAI
from telegram.ext import Updater, MessageHandler, Filters

# ========== НАСТРОЙКИ ==========

# Токен Telegram-бота (тот же секрет, что и у Самуила)
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")

# API ключ OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Модель (можно переопределить через переменную, если нужно)
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

# Ограничение длины ответа
MAX_REPLY_CHARS = 300

client = OpenAI(api_key=OPENAI_API_KEY)


# ========== ПОВЕДЕНИЕ ЛЕЙЛЫ ==========

SYSTEM_PROMPT = (
    "Ты бот по имени Лейла. Ты находишься в групповом чате. "
    "Ты отвечаешь ТОЛЬКО если сообщение начинается с обращения: "
    "Лейла / Leila / @Лейла / @leila.\n\n"
    "Характер Лейлы:\n"
    "• добрая, умная, поддерживающая, но честная\n"
    "• 2–4 коротких предложения\n"
    "• избегает токсичности\n"
    "• при темах здоровья — напоминает, что не врач\n"
    "• максимум 300 символов\n"
)

TRIGGERS = ["лейла", "leila", "@лейла", "@leila"]


# ========== ФУНКЦИИ ==========

def detect_leila(text: str):
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


def call_openai(user_text):
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_text},
    ]

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.6,
        max_tokens=256
    )

    reply = response.choices[0].message.content.strip()

    if len(reply) > MAX_REPLY_CHARS:
        reply = reply[:MAX_REPLY_CHARS].rstrip()

    return reply


def handle_message(update, context):
    msg = update.message
    if msg is None or msg.text is None:
        return

    text = msg.text
    is_for_leila, cleaned_text = detect_leila(text)

    if not is_for_leila:
        return

    if not cleaned_text:
        cleaned_text = "Ответь что-нибудь по теме, человек просто назвал твоё имя."

    reply = call_openai(cleaned_text)
    msg.reply_text(reply)


# ========== ЗАПУСК ==========

def main():
    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
