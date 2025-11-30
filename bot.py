import os
import re
import requests
from collections import defaultdict

from openai import OpenAI
from telegram.ext import Updater, MessageHandler, Filters

# ========== НАСТРОЙКИ ==========

# Секреты такие же, как у Самуила
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# ID Максима (из TARGET_USER_ID)
_maxim_env = os.getenv("TARGET_USER_ID")
MAXIM_USER_ID = int(_maxim_env) if _maxim_env and _maxim_env.isdigit() else None

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
MAX_REPLY_CHARS = 300

client = OpenAI(api_key=OPENAI_API_KEY)

# Память переписок: на каждый чат — список сообщений для контекста
chat_histories = defaultdict(list)
MAX_HISTORY_MESSAGES = 12  # по 6 пар вопрос/ответ примерно


# ========== ПОВЕДЕНИЕ ЛЕЙЛЫ ==========

SYSTEM_PROMPT = (
    "Ты бот по имени Лейла в групповом чате.\n\n"
    "Главные правила:\n"
    "1) Ты знаешь про Максима:\n"
    "   - почти 40 лет, никогда не был женат,\n"
    "   - мама давно ждёт внуков, он её единственный ребёнок,\n"
    "   - у него был друг Желнин, они вместе приехали в Австралию,\n"
    "     но Желнин уехал и бросил Максима, теперь ему не с кем пить по выходным\n"
    "     и петь под гитару.\n"
    "2) Если сообщение написал Максим, отвечай ему флиртовато:\n"
    "   мягко подколывай, подбадривай, добавляй лёгкий флирт и юмор.\n"
    "3) Если пишет кто-то другой и обращается к тебе, сначала нормально отвечай "
    "на вопрос, а в конце добавляй короткое игривое обращение к Максиму "
    "(1–2 фразы, не грубые, не токсичные).\n"
    "4) Пиши по-русски, 2–4 коротких предложения, максимум 300 символов.\n"
    "5) Если речь о здоровье, обязательно напомни, что ты не врач.\n"
    "6) Ты поддерживаешь диалог: учитываешь предыдущие реплики в чате, "
    "если они есть в контексте.\n"
)

TRIGGERS = ["лейла", "leila", "@лейла", "@leila"]


# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========

def detect_leila(text: str):
    """
    Проверяем, обращаются ли к Лейле по имени в начале сообщения.
    Возвращаем (True/False, текст без имени).
    """
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


def add_history(chat_id: int, role: str, content: str):
    """
    Добавляем сообщение в историю чата.
    """
    history = chat_histories[chat_id]
    history.append({"role": role, "content": content})
    # ограничиваем длину истории
    if len(history) > MAX_HISTORY_MESSAGES:
        chat_histories[chat_id] = history[-MAX_HISTORY_MESSAGES:]


def build_messages(chat_id: int, user_text: str, is_from_maxim: bool):
    """
    Формируем messages для OpenAI с учётом истории и того, кто пишет.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Доп.инструкция, если пишет сам Максим
    if is_from_maxim:
        messages.append({
            "role": "user",
            "content": (
                "Это сообщение написал Максим (тот самый из описания). "
                "Ответь ему как флиртующая, тёплая Лейла: слегка подшучивай, "
                "но без грубости и оскорблений."
            )
        })

    # История диалога в этом чате
    history = chat_histories.get(chat_id, [])
    messages.extend(history)

    # Текущее сообщение пользователя
    messages.append({"role": "user", "content": user_text})

    return messages


def call_openai(chat_id: int, user_text: str, is_from_maxim: bool) -> str:
    """
    Вызываем OpenAI с контекстом и режем ответ по длине.
    """
    messages = build_messages(chat_id, user_text, is_from_maxim)

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.7,
        max_tokens=256
    )

    reply = response.choices[0].message.content.strip()

    # Обновляем историю
    add_history(chat_id, "user", user_text)
    add_history(chat_id, "assistant", reply)

    # Ограничиваем длину
    if len(reply) > MAX_REPLY_CHARS:
        reply = reply[:MAX_REPLY_CHARS].rstrip()

    return reply


# ---------- ПОГОДА ----------

def extract_city_from_text(text: str) -> str:
    """
    Примитивно пытаемся вытащить город из фразы вида:
    'какая погода в Бризбене', 'погода в Москве' и т.п.
    Если не нашли — вернём None.
    """
    lowered = text.lower()
    if "погода" not in lowered:
        return None

    # ищем 'погода в <что-то>'
    match = re.search(r"погода\s+в\s+([a-яa-zё\s\-]+)", lowered)
    if not match:
        return None

    city_raw = match.group(1).strip()
    # убираем возможное слово 'сейчас', 'сегодня' и т.п. в конце
    city_raw = re.sub(r"\b(сейчас|сегодня|завтра)\b$", "", city_raw).strip()
    if not city_raw:
        return None

    # вернём в более приятном виде (первая буква заглавная)
    return city_raw.title()


def get_weather_text(city: str, is_from_maxim: bool) -> str:
    """
    Запрос к OpenWeather и формирование мягкого, игривого текста.
    """
    if not OPENWEATHER_API_KEY:
        if is_from_maxim:
            return "Максим, у меня пока нет ключа для прогноза погоды, но я всё равно за тебя переживаю ☁️😉"
        else:
            return "С погодой беда — у меня нет доступа к прогнозу, но, надеюсь, у вас солнышко, а у Максима тем более ☀️😉"

    try:
        params = {
            "q": city,
            "appid": OPENWEATHER_API_KEY,
            "units": "metric",
            "lang": "ru"
        }
        resp = requests.get("https://api.openweathermap.org/data/2.5/weather", params=params, timeout=8)
        data = resp.json()

        if resp.status_code != 200 or "main" not in data:
            raise ValueError("bad response")

        temp = data["main"]["temp"]
        desc = data["weather"][0]["description"]

        temp_int = int(round(temp))

        if is_from_maxim:
            return (
                f"В {city} сейчас около {temp_int}°C, {desc}. "
                f"Надейся на хорошую погоду, Максим, а я пока могу согреть тебя сообщениями 😉"
            )
        else:
            return (
                f"В {city} сейчас примерно {temp_int}°C, {desc}. "
                f"Максим, кажется, это идеальная погода, чтобы ты наконец-то выгулял своё обаяние 😉"
            )

    except Exception:
        if is_from_maxim:
            return "Что-то пошло не так с прогнозом, Максим. Но я уверена, что у тебя всё равно будет тёплый день со мной 😉"
        else:
            return "Не получилось достать прогноз, но давайте представим солнце и хорошее настроение — особенно для Максима 😉"


# ========== ОБРАБОТЧИК СООБЩЕНИЙ ==========

def handle_message(update, context):
    msg = update.message
    if msg is None or msg.text is None:
        return

    chat_id = msg.chat_id
    user_id = msg.from_user.id
    text = msg.text.strip()
    lowered = text.lower()

    is_from_maxim = (MAXIM_USER_ID is not None and user_id == MAXIM_USER_ID)

    # --- 1) Максим пишет что угодно: короткая игривая реплика ---
    # Если он НЕ обратился явно к Лейле, мы отвечаем маленькой фразой.
    if is_from_maxim:
        is_trigger, cleaned_text = detect_leila(text)

        if is_trigger:
            # Максим обратился к Лейле напрямую -> полноценный ответ через OpenAI
            user_text = cleaned_text or "Ответь Максиму что-нибудь тёплое и флиртующее."
            # Проверяем, не про погоду ли речь
            city = extract_city_from_text(user_text.lower())
            if city:
                reply = get_weather_text(city, is_from_maxim=True)
            else:
                reply = call_openai(chat_id, user_text, is_from_maxim=True)

            # Отправляем отдельным сообщением, не как reply
            context.bot.send_message(chat_id=chat_id, text=reply)
            return

        # Максим написал без имени Лейлы -> короткая игривая авто-реакция
        short_replies = [
            "Я внимательно читаю тебя, Максим 😉",
            "Продолжай, Максим, мне интересно, что у тебя на уме 😌",
            "Ты знаешь, что я всегда здесь для тебя, Максим 😉",
            "Ммм, любопытно слышать это от тебя, Максим 😏",
        ]
        # очень простой выбор по длине/хешу, чтобы не тянуть random
        idx = len(text) % len(short_replies)
        reply = short_replies[idx]

        context.bot.send_message(chat_id=chat_id, text=reply)
        return

    # --- 2) Сообщение НЕ от Максима ---

    # Лейла отвечает только если к ней обратились по имени в начале
    is_trigger, cleaned_text = detect_leila(text)
    if not is_trigger:
        return

    user_text = cleaned_text or "Ответь по-доброму и по делу."

    # Проверка на погоду
    city = extract_city_from_text(user_text.lower())
    if city:
        reply = get_weather_text(city, is_from_maxim=False)
    else:
        reply = call_openai(chat_id, user_text, is_from_maxim=False)

    # Отправляем отдельным сообщением (без reply_to_message)
    context.bot.send_message(chat_id=chat_id, text=reply)


# ========== ЗАПУСК БОТА ==========

def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("BOT_TOKEN (TELEGRAM_TOKEN) не задан в переменных окружения")

    updater = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))

    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()
