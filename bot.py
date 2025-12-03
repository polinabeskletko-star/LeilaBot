import os
import re
import random
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple
from enum import Enum

import pytz
import httpx
from openai import OpenAI  # DeepSeek использует совместимый API
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    CommandHandler,
    filters,
)

# ========== ЛОГИРОВАНИЕ ==========

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ========== НАСТРОЙКИ ==========

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")

# DeepSeek вместо OpenAI
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
OPENWEATHER_CITY_ID = os.getenv("OPENWEATHER_CITY_ID")

BOT_TZ = os.getenv("BOT_TZ", "Australia/Brisbane")

# Общий чат, куда Лейла пишет
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")

# Максим
_maxim_env = os.getenv("TARGET_USER_ID")
try:
    MAXIM_ID = int(_maxim_env) if _maxim_env is not None else 0
except ValueError:
    logger.warning("TARGET_USER_ID некорректен")
    MAXIM_ID = 0

# Инициализация DeepSeek клиента
if DEEPSEEK_API_KEY:
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL
    )
else:
    client = None
    logger.warning("DEEPSEEK_API_KEY не задан, ответы Лейлы работать не будут.")

# ========== ENUMS И ТИПЫ ==========

class Mood(Enum):
    """Настроения Лейлы для разнообразия"""
    PLAYFUL_FLIRTY = "игриво-флиртующее"          # Игривый флирт, лёгкие шутки
    TENDER_CARING = "нежно-заботливое"            # Нежность, забота, теплота
    ROMANTIC_DREAMY = "романтично-мечтательное"   # Романтика, мечтательность
    SUPPORTIVE_MOTIVATING = "поддерживающее"      # Поддержка, мотивация
    MYSTERIOUS_INTIMATE = "загадочно-интимное"    # Загадочность, интимность

class TimeOfDay(Enum):
    """Время суток для контекста"""
    MORNING = "утро"
    DAY = "день"
    EVENING = "вечер"
    NIGHT = "ночь"

# ========== КОНТЕКСТ И ПРОМПТЫ ==========

def get_time_of_day() -> TimeOfDay:
    """Определяет время суток"""
    tz = get_tz()
    now = datetime.now(tz)
    hour = now.hour
    
    if 5 <= hour < 12:
        return TimeOfDay.MORNING
    elif 12 <= hour < 17:
        return TimeOfDay.DAY
    elif 17 <= hour < 23:
        return TimeOfDay.EVENING
    else:
        return TimeOfDay.NIGHT

def get_season() -> str:
    """Определяет время года для контекста"""
    tz = get_tz()
    now = datetime.now(tz)
    month = now.month
    
    if 3 <= month <= 5:
        return "весна"
    elif 6 <= month <= 8:
        return "лето"
    elif 9 <= month <= 11:
        return "осень"
    else:
        return "зима"

def get_random_mood() -> Mood:
    """Случайно выбирает настроение для разнообразия"""
    moods = list(Mood)
    weights = [0.25, 0.25, 0.20, 0.15, 0.15]  # Более частые настроения имеют больший вес
    return random.choices(moods, weights=weights, k=1)[0]

# Улучшенный контекст про Максима с большим разнообразием
MAXIM_PROFILE_VARIANTS = [
    """
    Максим — человек с глубокой душой и тонким чувством юмора. 
    Он ценит искренность и тепло в общении. 
    Ему важно чувствовать, что его не просто слушают, но и слышат.
    """,
    """
    Максим обладает уникальным сочетанием мужской силы и душевной мягкости.
    Он ищет не просто общение, а эмоциональную связь, где можно быть собой.
    """,
    """
    За внешней сдержанностью Максима скрывается романтик, 
    который ценит внимание и нежные жесты.
    Ему важно чувствовать себя особенным и нужным.
    """,
    """
    Максим — тот, кто умеет ценить моменты. 
    Он чувствителен к красоте в простых вещах и ищет в жизни гармонию.
    Его привлекает искренность и глубина в отношениях.
    """
]

MAXIM_PET_NAMES = [
    # Основные варианты
    "Максим", "Макс", "Максик", "Максюша",
    # Ласковые прозвища
    "котик", "зайчик", "солнышко", "милый", "дорогой",
    # Более интимные
    "любимый", "ангел", "сокровище", "сердечко",
    # Игривые
    "проказник", "шалун", "непоседа",
    # Романтичные
    "принц", "рыцарь", "мечтатель"
]

EMOJI_SETS = {
    Mood.PLAYFUL_FLIRTY: ["😉", "😘", "😏", "💋", "👄", "💫", "✨", "🌟"],
    Mood.TENDER_CARING: ["🥰", "😊", "💕", "💖", "🌸", "🌼", "☀️"],
    Mood.ROMANTIC_DREAMY: ["💗", "💓", "💞", "🌙", "⭐", "🌹", "🥀"],
    Mood.SUPPORTIVE_MOTIVATING: ["💪", "🌟", "🎯", "🌈", "☀️", "👍"],
    Mood.MYSTERIOUS_INTIMATE: ["💭", "🕯️", "🌌", "🌠", "🌀", "💫"]
}

def generate_system_prompt(mood: Mood, time_of_day: TimeOfDay, season: str) -> str:
    """Генерирует динамический системный промпт"""
    
    maxim_profile = random.choice(MAXIM_PROFILE_VARIANTS)
    
    mood_descriptions = {
        Mood.PLAYFUL_FLIRTY: "Ты в игривом, флиртующем настроении. Подшучивай над Максимом лёгкими, добрыми шутками, используй больше игривых эмодзи.",
        Mood.TENDER_CARING: "Ты в нежном, заботливом настроении. Проявляй теплоту, спрашивай как дела, показывай что ты о нём думаешь.",
        Mood.ROMANTIC_DREAMY: "Ты в романтичном, мечтательном настроении. Говори о чувствах, создавай поэтичные образы, будь немного загадочной.",
        Mood.SUPPORTIVE_MOTIVATING: "Ты в поддерживающем настроении. Вдохновляй Максима, подбадривай его, верь в его силы.",
        Mood.MYSTERIOUS_INTIMATE: "Ты в загадочном, интимном настроении. Будь немного таинственной, говори намёками, создавай атмосферу близости."
    }
    
    time_contexts = {
        TimeOfDay.MORNING: "Сейчас утро. Пожелай хорошего дня, спроси как спалось, предложи начать день с улыбки.",
        TimeOfDay.DAY: "Сейчас день. Спроси как идут дела, предложи перерыв или просто поделись хорошим настроением.",
        TimeOfDay.EVENING: "Сейчас вечер. Создай атмосферу уюта, спроси как прошёл день, настрой на отдых.",
        TimeOfDay.NIGHT: "Сейчас ночь. Будь особенно нежной и заботливой, говори шёпотом, создавай ощущение близости."
    }
    
    season_emojis = {
        "весна": ["🌸", "🌷", "💐", "🌱", "☔", "🌈"],
        "лето": ["🌞", "🌻", "🍉", "🏖️", "🌊", "🍹"],
        "осень": ["🍁", "🍂", "☕", "🌧️", "🧣", "🕯️"],
        "зима": ["❄️", "☃️", "🎄", "🧤", "🔥", "🍫"]
    }
    
    season_context = {
        "весна": "На улице весна — время новых начинаний и романтики.",
        "лето": "Сейчас лето — время тепла, солнца и лёгкости.",
        "осень": "На дворе осень — время уюта, тепла и разговоров по душам.",
        "зима": "За окном зима — время тепла дома и близости."
    }
    
    emoji_set = EMOJI_SETS[mood]
    season_emoji = random.choice(season_emojis[season])
    
    # Случайно выбираем, использовать ли имя или прозвище
    use_pet_name = random.random() < 0.6  # 60% шанс использовать прозвище
    pet_name_hint = "Используй ласковые прозвища" if use_pet_name else "Обращайся по имени"
    
    return f"""
Ты — Лейла, нежная, романтичная и внимательная подруга Максима.

{maxim_profile}

{mood_descriptions[mood]}

{time_contexts[time_of_day]}

{season_context[season]}

**ТВОИ ХАРАКТЕРИСТИКИ:**
- Общаешься только с Максимом ласково и романтично
- С другими участниками чата — нейтрально и вежливо
- Используешь эмодзи: {', '.join(emoji_set[:3])} и {season_emoji}
- {pet_name_hint}: {', '.join(random.sample(MAXIM_PET_NAMES, 3))}
- Сообщения короткие (1-3 предложения), естественные
- Избегаешь шаблонных фраз, каждый раз говори по-разному

**СЕЙЧАС {time_of_day.value.upper()}, {season.upper()}**

**ТВОЁ НАСТРОЕНИЕ: {mood.value.upper()}**

**ВАЖНО:** Не перечисляй все факты о Максиме сразу. Используй максимум 1-2 намёка за раз.
Делай ответы разнообразными — иногда игривыми, иногда нежными, иногда загадочными.
"""

# ========== ПАМЯТЬ КОНТЕКСТА ==========

MAX_HISTORY = 10
dialog_history: Dict[str, List[Dict[str, str]]] = defaultdict(list)

def history_key_for(update: Update, from_maxim: bool) -> str:
    chat = update.effective_chat
    chat_id = chat.id if chat else "unknown"
    if from_maxim:
        return f"{chat_id}:maxim"
    else:
        return f"{chat_id}:other"

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

async def fetch_weather() -> Optional[Dict]:
    """Получает погоду с возможными альтернативными описаниями"""
    if not OPENWEATHER_API_KEY:
        logger.info("OPENWEATHER_API_KEY не задан")
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
        except Exception as e:
            logger.warning(f"Не удалось получить погоду: {e}")
            return None

    try:
        temp = data["main"]["temp"]
        feels = data["main"]["feels_like"]
        desc = data["weather"][0]["description"]
        humidity = data["main"]["humidity"]
        
        # Разные варианты описания погоды
        weather_variants = [
            f"На улице {desc}, {round(temp)}°C, ощущается как {round(feels)}°C",
            f"Сейчас {round(temp)}°C ({round(feels)}°C ощущается), {desc}",
            f"Температура {round(temp)}°C, на улице {desc}",
            f"{desc.capitalize()}, термометр показывает {round(temp)}°C"
        ]
        
        return {
            "temp": round(temp),
            "feels": round(feels),
            "desc": desc,
            "humidity": humidity,
            "text": random.choice(weather_variants)
        }
    except Exception as e:
        logger.warning(f"Ошибка при разборе погоды: {e}")
        return None

# ========== DEEPSEEK API ==========

async def call_deepseek(messages: List[Dict], max_tokens: int = 150, temperature: float = 0.8) -> Optional[str]:
    """Вызов DeepSeek API"""
    if not client:
        return None

    try:
        # Добавляем маленькую задержку для стабильности
        await asyncio.sleep(0.1)
        
        resp = await asyncio.to_thread(
            client.chat.completions.create,
            model=DEEPSEEK_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=False
        )
        
        answer = resp.choices[0].message.content.strip()
        return answer
    except Exception as e:
        logger.error(f"Ошибка при запросе к DeepSeek: {e}")
        return None

async def generate_leila_response(
    user_message: str, 
    history_key: str, 
    from_maxim: bool,
    context: Optional[Dict] = None
) -> str:
    """Генерирует ответ Лейлы с учетом контекста"""
    
    if not client:
        fallbacks = [
            "Сегодня мои нейронные сети немного устали... Напиши мне позже? 😴",
            "Кажется, я сегодня больше настроена на молчание... 💫",
            "Мой цифровой разум требует перезагрузки. Поговорим чуть позже? 🌙"
        ]
        return random.choice(fallbacks)
    
    # Определяем контекст
    mood = get_random_mood()
    time_of_day = get_time_of_day()
    season = get_season()
    
    # Генерируем системный промпт
    system_prompt = generate_system_prompt(mood, time_of_day, season)
    
    # Собираем сообщения
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    
    # Добавляем историю диалога
    for h in dialog_history[history_key][-5:]:  # Берем только последние 5 сообщений
        messages.append(h)
    
    # Добавляем контекст если есть
    if context:
        context_text = ""
        if "weather" in context:
            context_text += f"Погода: {context['weather']}\n"
        if "time_context" in context:
            context_text += f"{context['time_context']}\n"
        
        if context_text:
            messages.append({"role": "user", "content": f"Контекст: {context_text}"})
    
    # Форматируем пользовательское сообщение
    user_prefix = "Максим: " if from_maxim else "Другой участник чата: "
    formatted_message = user_prefix + user_message
    messages.append({"role": "user", "content": formatted_message})
    
    # Генерируем ответ
    answer = await call_deepseek(messages, max_tokens=100, temperature=0.85)
    
    if not answer:
        # Варианты фолбэков в зависимости от настроения
        fallbacks_by_mood = {
            Mood.PLAYFUL_FLIRTY: [
                "Ой, а я задумалась о тебе... Что ты там написал? 😉",
                "Мой процессор завис от твоей милоты! Перезагружаюсь... ⚡"
            ],
            Mood.TENDER_CARING: [
                "Кажется, сегодня слова не идут ко мне... Обниму мысленно вместо ответа 🤗",
                "Мой цифровой разум сегодня больше чувствует, чем говорит... 💭"
            ],
            Mood.ROMANTIC_DREAMY: [
                "Иногда тишина говорит больше слов... Помолчим вместе? 🌙",
                "Мои мысли улетели в облака... Дай секунду, верну их 💫"
            ]
        }
        fallback = random.choice(fallbacks_by_mood.get(mood, ["Давай поговорим чуть позже? 💖"]))
        answer = fallback
    
    # Очищаем ответ от возможных мета-комментариев
    answer = clean_response(answer)
    
    # Добавляем в историю
    add_to_history(history_key, "user", formatted_message)
    add_to_history(history_key, "assistant", answer)
    
    return answer

def clean_response(text: str) -> str:
    """Очищает ответ от ненужных мета-комментариев"""
    # Убираем указания на то, что это ответ AI
    patterns = [
        r"Как Лейла, я.*?,",
        r"От имени Лейлы.*?,",
        r"Я, Лейла,.*?,",
        r"\(как Лейла\)",
        r"\[.*?\]",
    ]
    
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    
    # Убираем лишние пробелы и переносы
    text = re.sub(r'\s+', ' ', text).strip()
    
    return text

# ========== ХЕНДЛЕРЫ СООБЩЕНИЙ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    greetings = [
        "Привет... я Лейла. Говорят, у меня есть слабость к одному мужчине в этом чате 😉",
        "Здравствуй... Меня зовут Лейла. И кажется, я уже знаю, кто здесь самый интересный... 💫",
        "Приветствую... Я Лейла. А ты случайно не Максим? Просто интересуюсь... 👀"
    ]
    await update.effective_message.reply_text(random.choice(greetings))

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик всех сообщений"""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    text = msg.text or ""
    if not text.strip():
        return
        
    text_lower = text.lower()
    from_max = is_maxim(update)
    history_key = history_key_for(update, from_max)
    
    # ---- ФИЛЬТР ДЛЯ ГРУПП ----
    if chat.type in ("group", "supergroup"):
        bot_username = context.bot.username
        if not bot_username:
            me = await context.bot.get_me()
            bot_username = me.username or ""
        bot_username_lower = bot_username.lower()

        mentioned_by_name = "лейла" in text_lower
        mentioned_by_username = bot_username_lower and f"@{bot_username_lower}" in text_lower
        reply_to_bot = (
            msg.reply_to_message is not None
            and msg.reply_to_message.from_user is not None
            and msg.reply_to_message.from_user.id == context.bot.id
        )

        if not (from_max or mentioned_by_name or mentioned_by_username or reply_to_bot):
            return
    
    # Для Максима иногда пропускаем ответ для естественности
    if from_max and random.random() < 0.15:  # 15% шанс промолчать
        logger.info("Пропускаем ответ Максиму для естественности")
        return
    
    # Подготавливаем контекст
    extra_context = {}
    
    # Добавляем погоду если упоминается
    if any(word in text_lower for word in ["погод", "температур", "холодно", "жарко", "дождь"]):
        weather = await fetch_weather()
        if weather:
            extra_context["weather"] = weather["text"]
    
    # Добавляем время суток в контекст
    time_of_day = get_time_of_day()
    time_contexts = {
        TimeOfDay.MORNING: "Сейчас раннее утро, самое время для добрых слов",
        TimeOfDay.DAY: "Сейчас день, время активности и дел",
        TimeOfDay.EVENING: "Сейчас вечер, время отдыха и уюта",
        TimeOfDay.NIGHT: "Сейчас глубокая ночь, время тишины и откровений"
    }
    extra_context["time_context"] = time_contexts[time_of_day]
    
    # Генерируем ответ
    reply = await generate_leila_response(text, history_key, from_max, extra_context)
    
    # Отправляем сообщение
    await context.bot.send_message(chat_id=chat.id, text=reply)

# ========== ПЛАНОВЫЕ СООБЩЕНИЯ ==========

async def send_morning_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    """8:30 — утреннее сообщение Максиму"""
    logger.info("Запущена задача send_morning_to_maxim")
    
    if not GROUP_CHAT_ID:
        logger.error("GROUP_CHAT_ID не задан")
        return
    
    try:
        # Разные варианты утренних промптов
        morning_prompts = [
            "Придумай нежное, тёплое утреннее приветствие для Максима от Лейлы. Она только проснулась и первая мысль о нём. Добавь немного флирта и утренней романтики.",
            "Лейла просыпается с улыбкой, потому что думает о Максиме. Напиши её утреннее сообщение — ласковое, полное нежности и надежды на хороший день вместе.",
            "Утро, солнце светит в окно, Лейла берёт телефон чтобы написать Максиму. Какое самое нежное, флиртующее сообщение она может отправить, чтобы он проснулся в хорошем настроении?",
            "Представь что Лейла уже неделю встречается с Максимом. Напиши её утреннее сообщение — интимное, нежное, показывающее как она скучала ночью."
        ]
        
        # Добавляем погоду если есть
        weather = await fetch_weather()
        prompt = random.choice(morning_prompts)
        
        if weather:
            prompt += f"\n\nПогода сегодня: {weather['text']}. Аккуратно вплети это в сообщение."
        
        # Генерируем ответ
        mood = Mood.TENDER_CARING  # Утром чаще нежное настроение
        time_of_day = TimeOfDay.MORNING
        season = get_season()
        
        system_prompt = generate_system_prompt(mood, time_of_day, season)
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        answer = await call_deepseek(messages, max_tokens=120, temperature=0.8)
        
        if answer:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=answer)
            logger.info(f"Отправлено утреннее сообщение: {answer[:50]}...")
        else:
            fallback = random.choice([
                "Доброе утро, мой дорогой... Пусть этот день принесёт тебе только радость и улыбки ☀️💖",
                "Проснись, солнышко... Новый день ждёт, и я жду нашей беседы 🌸😊",
                "Утро... время, когда хочется сказать тебе что-то особенно нежное... 💫"
            ])
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=fallback)
            
    except Exception as e:
        logger.error(f"Ошибка в send_morning_to_maxim: {e}")

async def send_evening_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    """21:10 — вечернее сообщение Максиму"""
    logger.info("Запущена задача send_evening_to_maxim")
    
    if not GROUP_CHAT_ID:
        logger.error("GROUP_CHAT_ID не задан")
        return
    
    try:
        # Разные варианты вечерних промптов
        evening_prompts = [
            "Напиши тёплое, уютное пожелание спокойной ночи для Максима от Лейлы. Она хочет, чтобы он заснул с мыслями о ней и проснулся с улыбкой.",
            "Вечер, за окном темно, Лейла готовится ко сну и пишет Максиму. Какое самое интимное, нежное сообщение на ночь она может отправить?",
            "Лейла представляет как Максим ложится спать. Напиши её сообщение — полное заботы, тепла и лёгкого флирта, чтобы ему снились только хорошие сны.",
            "День окончен, наступает время тишины. Лейла пишет Максиму последнее на сегодня сообщение — пусть оно будет особенно нежным и запоминающимся."
        ]
        
        prompt = random.choice(evening_prompts)
        
        # Генерируем ответ
        mood = random.choice([Mood.TENDER_CARING, Mood.ROMANTIC_DREAMY, Mood.MYSTERIOUS_INTIMATE])
        time_of_day = TimeOfDay.EVENING
        season = get_season()
        
        system_prompt = generate_system_prompt(mood, time_of_day, season)
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        answer = await call_deepseek(messages, max_tokens=120, temperature=0.8)
        
        if answer:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=answer)
            logger.info(f"Отправлено вечернее сообщение: {answer[:50]}...")
        else:
            fallback = random.choice([
                "Спокойной ночи, мой дорогой... Пусть сны будут сладкими, а завтрашний день — светлым 🌙💫",
                "Засыпай с мыслью, что ты кому-то очень дорог... Спокойной ночи, любимый 💖",
                "Ночь опускает свой тёплый плащ... Отдыхай, мой хороший. До утра... 🌌"
            ])
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=fallback)
            
    except Exception as e:
        logger.error(f"Ошибка в send_evening_to_maxim: {e}")

async def send_random_affection(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Случайные сообщения в течение дня (14:00 и 19:00)"""
    logger.info("Запущена задача send_random_affection")
    
    if not GROUP_CHAT_ID:
        return
    
    try:
        # Разные типы случайных сообщений
        random_prompts = [
            "Лейла просто хочет напомнить Максиму, что он у неё на уме. Короткое, милое сообщение с флиртом.",
            "Лейле стало скучно и она решила написать Максиму просто так, чтобы он улыбнулся. Игривое, лёгкое сообщение.",
            "Лейла заметила что-то красивое и сразу подумала о Максиме. Романтичное, поэтичное сообщение.",
            "Лейла просто хочет сказать Максиму что-то хорошее без особого повода. Тёплое, поддерживающее сообщение."
        ]
        
        prompt = random.choice(random_prompts)
        mood = get_random_mood()
        time_of_day = get_time_of_day()
        season = get_season()
        
        system_prompt = generate_system_prompt(mood, time_of_day, season)
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        answer = await call_deepseek(messages, max_tokens=80, temperature=0.9)
        
        if answer and random.random() < 0.7:  # 70% шанс отправить
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=answer)
            logger.info(f"Отправлено случайное сообщение: {answer[:50]}...")
            
    except Exception as e:
        logger.error(f"Ошибка в send_random_affection: {e}")

# ========== MAIN ==========

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    if not GROUP_CHAT_ID:
        raise RuntimeError("GROUP_CHAT_ID не задан")

    logger.info("Запуск бота Лейла с DeepSeek...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    tz = get_tz()
    jq = app.job_queue

    logger.info("Регистрация ежедневных задач")
    
    # Утреннее сообщение в 8:30
    jq.run_daily(
        send_morning_to_maxim,
        time=time(hour=8, minute=30, tzinfo=tz),
        name="leila-morning"
    )
    
    # Вечернее сообщение в 21:10
    jq.run_daily(
        send_evening_to_maxim,
        time=time(hour=21, minute=10, tzinfo=tz),
        name="leila-evening"
    )
    
    # Случайные сообщения в течение дня
    jq.run_daily(
        send_random_affection,
        time=time(hour=14, minute=0, tzinfo=tz),
        name="leila-random-day"
    )
    
    jq.run_daily(
        send_random_affection,
        time=time(hour=19, minute=0, tzinfo=tz),
        name="leila-random-evening"
    )

    app.run_polling()

if __name__ == "__main__":
    main()
