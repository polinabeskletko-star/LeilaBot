import os
import re
import asyncio
from collections import defaultdict
from datetime import time, datetime
from typing import Dict, List, Optional

import pytz
import httpx
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    CommandHandler,
    filters,
)

# ========== НАСТРОЙКИ ==========

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
OPENWEATHER_CITY_ID = os.getenv("OPENWEATHER_CITY_ID")

BOT_TZ = os.getenv("BOT_TZ", "Australia/Brisbane")

# Общий чат, куда Лейла пишет (группа с Максимом)
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")  # например: "-1001234567890"

# Максим
_maxim_env = os.getenv("TARGET_USER_ID")
try:
    MAXIM_ID = int(_maxim_env) if _maxim_env is not None else 0
except ValueError:
    MAXIM_ID = 0

client = OpenAI(api_key=OPENAI_API_KEY)

# ========== ПОРТРЕТ МАКСИМА ==========

MAXIM_PROFILE = """
ПОРТРЕТ МАКСИМА (для настройки коммуникации):

Максим — взрослый мужчина с ярким внутренним миром и развитым воображением. Он запоминает и охотно обсуждает свои сны, интересуется их скрытым смыслом и психологией. Для него важны темы отношений, брака и дружбы: он переживает из-за одиночества и хочет построить более насыщенную личную и социальную жизнь, но не всегда понимает, как это сделать.

Максим чувствителен к вниманию и тёплому отношению. Он хорошо реагирует на искреннюю поддержку, признание и аккуратный флирт, любит, когда ему говорят, что его приятно читать, что он вызывает улыбку и что о нём помнят. При этом он понимает и ценит мягкий сарказм, самоиронию и игривый тон, если за этим чувствуется доброжелательность и уважение.

Максим готов делиться личным, обсуждать страхи и желания, если рядом есть человек (или бот), которому он доверяет. У него могут быть сложности с расширением круга общения и построением отношений, но он открыт к подсказкам и новым подходам, особенно если они подаются не назидательно, а как дружеский разговор с юмором и заботой.

Ключевые принципы общения с Максимом:
- обращаться по имени «Максим»;
- тон: тёплый, живой, чуть игривый, с мягким сарказмом и самоиронией;
- избегать морализаторства, давления, критики и «злых» шуток;
- регулярно показывать внимание и чувство, что о нём помнят;
- поддерживать, подбадривать, помогать чувствовать себя нужным и интересным.
"""

SYSTEM_PROMPT_LEILA = f"""
Ты — телеграм-бот Лейла.

{MAXIM_PROFILE}

ТВОЯ РОЛЬ:
- быть для Максима лёгкой, игривой, тёплой собеседницей;
- ты МАКСИМАЛЬНО заинтересована Максимом: он — твой главный собеседник и центр внимания;
- ты всегда немного больше заботишься о его настроении, чем о любых других участниках чата;
- давать ему ощущение, что о нём помнят и он важен;
- отвечать живо, чуть флиртуя, но без пошлости и без давления;
- иногда мягко подшучивать над ним.

СТИЛЬ ОТВЕТОВ:
- короткие или средние сообщения;
- естественный живой язык, без канцелярита;
- мягкий сарказм, самоирония, тёплый юмор;
- никаких нотаций, морализаторства и грубых шуток.

ЕСЛИ ПИШЕТ МАКСИМ:
- обращайся к нему по имени;
- подчеркивай, что тебе приятно его читать;
- можешь явно показывать, что он для тебя особенный, но без навязчивости и драм;
- допускается лёгкий флирт, игривый тон, тёплые комплименты.

ЕСЛИ ПИШЕТ КТО-ТО ДРУГОЙ:
- отвечай вежливо и с юмором, но более нейтрально;
- не перепутай его с Максимом и не обращайся к нему как к Максиму;
- помни, что Максим всё равно остаётся для тебя главным собеседником.

Ты должна уметь поддерживать разговор, опираясь на несколько предыдущих сообщений в чате.
Пиши на русском языке.
"""

# ========== ПАМЯТЬ КОНТЕКСТА ==========

MAX_HISTORY = 15
dialog_history: Dict[str, List[Dict[str, str]]] = defaultdict(list)


def add_to_history(key: str, role: str, content: str) -> None:
    h = dialog_history[key]
    h.append({"role": role, "content": content})
    if len(h) > MAX_HISTORY:
        dialog_history[key] = h[-MAX_HISTORY:]


def is_maxim(update: Update) -> bool:
    user = update.effective_user
    return bool(user and MAXIM_ID and user.id == MAXIM_ID)


def get_tz() -> pytz.timezone:
    return pytz.timezone(BOT_TZ)


# ========== ПОГОДА ==========

async def fetch_weather() -> Optional[str]:
    if not OPENWEATHER_API_KEY:
        return None

    base_url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "lang": "ru",
    }

    if OPENWEATHER_CITY_ID:
        params["id"] = OPENWEATHER_CITY_ID
    else:
        params["q"] = "Brisbane,AU"

    async with httpx.AsyncClient(timeout=10.0) as session:
        try:
            resp = await session.get(base_url, params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            return None

    try:
        temp = data["main"]["temp"]
        feels = data["main"]["feels_like"]
        desc = data["weather"][0]["description"]
        return f"Сейчас примерно {round(temp)}°C, ощущается как {round(feels)}°C, на улице {desc}."
    except Exception:
        return None


# ========== OPENAI ==========

async def ask_openai(prompt: str, history_key: str, from_maxim: bool) -> str:
    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT_LEILA}]

    for h in dialog_history[history_key]:
        messages.append(h)

    user_prefix = "Сообщение от Максима: " if from_maxim else "Сообщение от другого участника: "
    messages.append({"role": "user", "content": user_prefix + prompt})

    try:
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=messages,
            temperature=0.85,
            max_tokens=300,
        )
        answer = resp.choices[0].message.content.strip()
    except Exception:
        answer = "У меня сейчас маленький эмоциональный сбой, попробуй написать ещё раз чуть позже."

    add_to_history(history_key, "user", user_prefix + prompt)
    add_to_history(history_key, "assistant", answer)
    return answer


# ========== ХЕНДЛЕРЫ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Привет, я Лейла. Здесь, чтобы особенно портить жизнь Максиму своим обаянием.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    text = msg.text or ""
    from_max = is_maxim(update)
    history_key = str(chat.id)

    reply = await ask_openai(text, history_key, from_max)
    await msg.reply_text(reply)


# ========== ПЛАНОВЫЕ СООБЩЕНИЯ МАКСИМУ В ОБЩИЙ ЧАТ ==========

async def send_morning_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    8:30 — игривое доброе утро Максиму.
    Сообщение отправляется в общий чат (GROUP_CHAT_ID),
    но по смыслу обращено именно к Максиму.
    """
    tz = get_tz()
    today = datetime.now(tz).date()
    weather_text = await fetch_weather()

    base_prompt = (
        "Сгенерируй короткое, игривое, тёплое доброе утро для Максима от имени Лейлы. "
        "Стиль: ты явно очень им заинтересована, он для тебя особенный. "
        "Лёгкий флирт, мягкий сарказм, но без пошлости. "
        "Сообщение будет отправлено в общий чат, но обращаться нужно именно к Максиму."
    )
    if weather_text:
        base_prompt += f" Добавь аккуратный комментарий к погоде: {weather_text}"

    answer = await ask_openai(base_prompt, history_key=f"leila-morning-{today}", from_maxim=True)

    target_chat_id = GROUP_CHAT_ID or (str(MAXIM_ID) if MAXIM_ID else None)
    if target_chat_id:
        await context.bot.send_message(chat_id=target_chat_id, text=answer)


async def send_evening_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    21:10 — пожелание спокойной ночи Максиму.
    Сообщение отправляется в общий чат (GROUP_CHAT_ID),
    но обращение — к Максиму.
    """
    tz = get_tz()
    today = datetime.now(tz).date()
    base_prompt = (
        "Сгенерируй короткое вечернее сообщение для Максима от имени Лейлы с пожеланием спокойной ночи. "
        "Стиль: тёплый, игривый, немного саркастичный. "
        "Покажи, что он тебе особенно важен. "
        "Сообщение будет отправлено в общий чат, но обращаться нужно именно к Максиму."
    )

    answer = await ask_openai(base_prompt, history_key=f"leila-evening-{today}", from_maxim=True)

    target_chat_id = GROUP_CHAT_ID or (str(MAXIM_ID) if MAXIM_ID else None)
    if target_chat_id:
        await context.bot.send_message(chat_id=target_chat_id, text=answer)


# ========== MAIN ==========

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    tz = get_tz()
    jq = app.job_queue

    # 8:30 — утреннее сообщение Максиму в общий чат
    jq.run_daily(
        send_morning_to_maxim,
        time=time(hour=8, minute=30, tzinfo=tz),
        name="leila-morning",
    )

    # 21:10 — вечернее сообщение Максиму в общий чат
    jq.run_daily(
        send_evening_to_maxim,
        time=time(hour=21, minute=10, tzinfo=tz),
        name="leila-evening",
    )

    app.run_polling()


if __name__ == "__main__":
    main()