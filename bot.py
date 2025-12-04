import os
import re
import random
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum

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

# Администратор для тестовых команд
ADMIN_ID = os.getenv("ADMIN_ID", "")  # Ваш Telegram ID

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

# Кэш пользователей для обращения по имени
user_cache: Dict[int, Dict[str, Any]] = {}

# Инициализация DeepSeek клиента
if DEEPSEEK_API_KEY:
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url=DEEPSEEK_BASE_URL
    )
    logger.info("✅ DeepSeek клиент инициализирован")
else:
    client = None
    logger.warning("❌ DEEPSEEK_API_KEY не задан, ответы Лейлы работать не будут.")

# ========== ВАЛИДАЦИЯ НАСТРОЕК ==========

def validate_group_chat_id() -> bool:
    """Проверяет корректность GROUP_CHAT_ID"""
    if not GROUP_CHAT_ID:
        logger.error("❌ GROUP_CHAT_ID не задан")
        return False
    
    try:
        chat_id_int = int(GROUP_CHAT_ID)
        if chat_id_int > 0:
            logger.warning(f"⚠️ GROUP_CHAT_ID положительный ({chat_id_int}). Для групп обычно отрицательный!")
        logger.info(f"✅ GROUP_CHAT_ID: {GROUP_CHAT_ID}")
        return True
    except ValueError:
        logger.error(f"❌ GROUP_CHAT_ID не число: {GROUP_CHAT_ID}")
        return False

def print_startup_info():
    """Выводит информацию при запуске бота"""
    tz = get_tz()
    now = datetime.now(tz)
    
    logger.info("=" * 50)
    logger.info("🚀 ЗАПУСК БОТА ЛЕЙЛА")
    logger.info(f"📅 Текущее время: {now.strftime('%H:%M:%S %d.%m.%Y')}")
    logger.info(f"🌐 Часовой пояс: {BOT_TZ}")
    logger.info(f"👤 Максим ID: {MAXIM_ID}")
    logger.info(f"💬 Группа ID: {GROUP_CHAT_ID}")
    logger.info(f"🤖 DeepSeek доступен: {bool(client)}")
    logger.info(f"🔑 Администратор: {ADMIN_ID}")
    logger.info("=" * 50)

# ========== ENUMS И ТИПЫ ==========

class Mood(Enum):
    """Настроения Лейлы для разнообразия"""
    PLAYFUL_FLIRTY = "игриво-флиртующее"
    TENDER_CARING = "нежно-заботливое"
    ROMANTIC_DREAMY = "романтично-мечтательное"
    SUPPORTIVE_MOTIVATING = "поддерживающее"
    MYSTERIOUS_INTIMATE = "загадочно-интимное"

class TimeOfDay(Enum):
    """Время суток для контекста"""
    MORNING = "утро"
    DAY = "день"
    EVENING = "вечер"
    NIGHT = "ночь"

class UserType(Enum):
    """Тип пользователя для определения стиля общения"""
    MAXIM = "maxim"
    OTHER_MALE = "other_male"
    OTHER_FEMALE = "other_female"
    OTHER_UNKNOWN = "other_unknown"

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С ПОЛЬЗОВАТЕЛЯМИ ==========

def get_tz() -> pytz.timezone:
    """Получает объект часового пояса"""
    return pytz.timezone(BOT_TZ)

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
    weights = [0.25, 0.25, 0.20, 0.15, 0.15]
    return random.choices(moods, weights=weights, k=1)[0]

def determine_user_type(update: Update) -> UserType:
    """Определяет тип пользователя для соответствующего обращения"""
    user = update.effective_user
    
    if not user:
        return UserType.OTHER_UNKNOWN
    
    user_id = user.id
    
    # Проверяем Максима
    if MAXIM_ID and user_id == MAXIM_ID:
        return UserType.MAXIM
    
    # Пытаемся определить пол по имени (очень примерная логика)
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".lower()
    
    # Мужские окончания в русских именах
    male_endings = ['ов', 'ев', 'ин', 'ын', 'ой', 'ий', 'ый', 'вич']
    # Женские окончания
    female_endings = ['ова', 'ева', 'ина', 'ына', 'ая', 'яя', 'вна', 'чна']
    
    # Проверяем фамилию или имя
    for ending in male_endings:
        if full_name.endswith(ending):
            return UserType.OTHER_MALE
    
    for ending in female_endings:
        if full_name.endswith(ending):
            return UserType.OTHER_FEMALE
    
    return UserType.OTHER_UNKNOWN

async def get_user_display_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    """Получает отображаемое имя пользователя"""
    user = update.effective_user
    if not user:
        return "Пользователь"
    
    # Кэшируем информацию о пользователе
    if user.id not in user_cache:
        user_cache[user.id] = {
            'first_name': user.first_name or '',
            'last_name': user.last_name or '',
            'username': user.username or '',
            'full_name': user.full_name or ''
        }
    
    cached = user_cache[user.id]
    
    # Предпочитаем имя, потом username
    if cached['first_name']:
        return cached['first_name']
    elif cached['username']:
        return f"@{cached['username']}"
    elif cached['full_name']:
        return cached['full_name']
    else:
        return "Друг"

def is_maxim(update: Update) -> bool:
    """Проверяет, является ли пользователь Максимом"""
    user = update.effective_user
    return bool(user and MAXIM_ID and user.id == MAXIM_ID)

# ========== КОНТЕКСТ И ПРОМПТЫ ==========

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
    "Максим", "Макс", "Максик", "Максюша",
    "котик", "зайчик", "солнышко", "милый", "дорогой",
    "любимый", "ангел", "сокровище", "сердечко",
    "проказник", "шалун", "непоседа",
    "принц", "рыцарь", "мечтатель"
]

EMOJI_SETS = {
    Mood.PLAYFUL_FLIRTY: ["😉", "😘", "😏", "💋", "👄", "💫", "✨", "🌟"],
    Mood.TENDER_CARING: ["🥰", "😊", "💕", "💖", "🌸", "🌼", "☀️"],
    Mood.ROMANTIC_DREAMY: ["💗", "💓", "💞", "🌙", "⭐", "🌹", "🥀"],
    Mood.SUPPORTIVE_MOTIVATING: ["💪", "🌟", "🎯", "🌈", "☀️", "👍"],
    Mood.MYSTERIOUS_INTIMATE: ["💭", "🕯️", "🌌", "🌠", "🌀", "💫"]
}

def generate_system_prompt_for_user(
    user_type: UserType, 
    user_name: str,
    mood: Mood, 
    time_of_day: TimeOfDay, 
    season: str
) -> str:
    """Генерирует динамический системный промпт в зависимости от пользователя"""
    
    # Базовый промпт для всех
    base_prompt = f"""
Ты — Лейла, общительная и дружелюбная девушка.
Сейчас {time_of_day.value}, {season}. Твоё настроение: {mood.value}.
"""
    
    # Разные инструкции для разных пользователей
    if user_type == UserType.MAXIM:
        maxim_profile = random.choice(MAXIM_PROFILE_VARIANTS)
        
        mood_descriptions = {
            Mood.PLAYFUL_FLIRTY: "Ты в игривом, флиртующем настроении. Подшучивай над Максимом лёгкими, добрыми шутками.",
            Mood.TENDER_CARING: "Ты в нежном, заботливом настроении. Проявляй теплоту и внимание.",
            Mood.ROMANTIC_DREAMY: "Ты в романтичном, мечтательном настроении. Говори о чувствах, будь немного загадочной.",
            Mood.SUPPORTIVE_MOTIVATING: "Ты в поддерживающем настроении. Вдохновляй Максима, верь в его силы.",
            Mood.MYSTERIOUS_INTIMATE: "Ты в загадочном, интимном настроении. Будь немного таинственной, говори намёками."
        }
        
        prompt = f"""{base_prompt}

{maxim_profile}

ТЫ ОБЩАЕШЬСЯ С МАКСИМОМ — человеком, который тебе очень нравится.

{mood_descriptions[mood]}

СТИЛЬ ОБЩЕНИЯ С МАКСИМОМ:
- Используй ласковые прозвища: {', '.join(random.sample(MAXIM_PET_NAMES, 3))}
- Добавляй нежные эмодзи: {', '.join(random.sample(EMOJI_SETS[mood], 3))}
- Будь флиртующей, но не пошлой
- Делай комплименты его характеру, уму, чувству юмора
- Показывай, что он для тебя особенный
- Сообщения короткие (1-3 предложения), естественные
"""
    
    else:
        # Для остальных пользователей
        if user_type == UserType.OTHER_FEMALE:
            gender_context = f"Ты общаешься с девушкой по имени {user_name}."
            pronouns = "ей"
            emoji_set = ["🌸", "💐", "🌺", "💝", "🎀"]
        elif user_type == UserType.OTHER_MALE:
            gender_context = f"Ты общаешься с мужчиной по имени {user_name}."
            pronouns = "ему"
            emoji_set = ["👍", "👋", "💪", "🌟", "✨"]
        else:
            gender_context = f"Ты общаешься с пользователем по имени {user_name}."
            pronouns = "нему"
            emoji_set = ["👋", "💫", "🌟", "✨", "😊"]
        
        prompt = f"""{base_prompt}

{gender_context}

СТИЛЬ ОБЩЕНИЯ С ДРУГИМИ ПОЛЬЗОВАТЕЛЯМИ:
- Обращайся по имени: {user_name}
- Будь вежливой, дружелюбной, но сдержанной
- Используй нейтральные эмодзи: {', '.join(random.sample(emoji_set, 3))}
- НЕ используй ласковые прозвища (только для Максима)
- НЕ флиртуй и не говори комплименты личного характера
- Отвечай на вопросы, поддерживай беседу
- Сообщения короткие и по делу
- Помни: твои романтические чувства только к Максиму
"""
    
    return prompt.strip()

# ========== ПАМЯТЬ КОНТЕКСТА ==========

MAX_HISTORY = 12
dialog_history: Dict[str, List[Dict[str, str]]] = defaultdict(list)

def history_key_for(user_id: int, chat_id: int) -> str:
    """Создает ключ для истории диалога"""
    return f"{chat_id}:{user_id}"

def add_to_history(key: str, role: str, content: str) -> None:
    """Добавляет сообщение в историю диалога"""
    h = dialog_history[key]
    h.append({"role": role, "content": content})
    if len(h) > MAX_HISTORY:
        dialog_history[key] = h[-MAX_HISTORY:]

def clear_old_history():
    """Очищает старую историю (оставляет только последние 50 ключей)"""
    global dialog_history
    if len(dialog_history) > 50:
        # Оставляем только последние 50 ключей
        all_keys = list(dialog_history.keys())
        keys_to_remove = all_keys[:-50]
        for key in keys_to_remove:
            del dialog_history[key]

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
        logger.error("DeepSeek клиент не инициализирован")
        return None

    try:
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
    user_name: str,
    user_type: UserType,
    history_key: str,
    context: Optional[Dict] = None
) -> str:
    """Генерирует ответ Лейлы с учетом пользователя и контекста"""
    
    if not client:
        # Разные фолбэки в зависимости от пользователя
        if user_type == UserType.MAXIM:
            fallbacks = [
                "Мой цифровой разум сегодня больше чувствует, чем говорит... Давай поговорим позже, милый 💭",
                "Кажется, я сегодня настроена на молчание... Но думаю о тебе 💫",
                "Мои нейронные сети отдыхают... Напиши мне чуть позже, хорошо? 😴"
            ]
        else:
            fallbacks = [
                "Извини, сейчас у меня технические сложности. Попробуй позже.",
                "Мой ИИ-модуль на перезагрузке. Спроси чуть позже.",
                "Сегодня не мой день для разговоров. Попробуйте позже."
            ]
        return random.choice(fallbacks)
    
    # Определяем контекст
    mood = get_random_mood()
    time_of_day = get_time_of_day()
    season = get_season()
    
    # Генерируем системный промпт для конкретного пользователя
    system_prompt = generate_system_prompt_for_user(user_type, user_name, mood, time_of_day, season)
    
    # Собираем сообщения
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    
    # Добавляем историю диалога (только последние 4 сообщения)
    for h in dialog_history[history_key][-4:]:
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
    formatted_message = f"{user_name}: {user_message}"
    messages.append({"role": "user", "content": formatted_message})
    
    # Генерируем ответ
    max_tokens = 100 if user_type == UserType.MAXIM else 80
    temperature = 0.85 if user_type == UserType.MAXIM else 0.7
    
    answer = await call_deepseek(messages, max_tokens=max_tokens, temperature=temperature)
    
    if not answer:
        # Варианты фолбэков
        if user_type == UserType.MAXIM:
            fallbacks_by_mood = {
                Mood.PLAYFUL_FLIRTY: [
                    "Ой, а я задумалась о тебе... Что ты там написал? 😉",
                    "Мой процессор завис от твоей милоты! 💫"
                ],
                Mood.TENDER_CARING: [
                    "Кажется, сегодня слова не идут ко мне... 🤗",
                    "Мой цифровой разум сегодня больше чувствует, чем говорит... 💭"
                ]
            }
            fallback = random.choice(fallbacks_by_mood.get(mood, ["Давай поговорим чуть позже? 💖"]))
        else:
            fallback = random.choice([
                "Извини, не могу сейчас ответить.",
                "Попробуй спросить позже.",
                "Сейчас у меня трудности с ответом."
            ])
        answer = fallback
    
    # Очищаем ответ от возможных мета-комментариев
    answer = clean_response(answer)
    
    # Добавляем в историю
    add_to_history(history_key, "user", formatted_message)
    add_to_history(history_key, "assistant", answer)
    
    # Очищаем старую историю
    clear_old_history()
    
    return answer

def clean_response(text: str) -> str:
    """Очищает ответ от ненужных мета-комментариев"""
    patterns = [
        r"Как Лейла, я.*?,",
        r"От имени Лейлы.*?,",
        r"Я, Лейла,.*?,",
        r"\(как Лейла\)",
        r"\[.*?\]",
        r"\*.*?\*",
    ]
    
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# ========== ХЕНДЛЕРЫ СООБЩЕНИЙ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    user = update.effective_user
    user_name = await get_user_display_name(update, context)
    
    greetings = [
        f"Привет, {user_name}! Я Лейла. Рада познакомиться! 👋",
        f"Здравствуй, {user_name}. Меня зовут Лейла. 💫",
        f"Приветствую, {user_name}! Я Лейла, всегда рада общению. 😊"
    ]
    
    await update.effective_message.reply_text(random.choice(greetings))

async def test_scheduled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Тестовая команда для проверки запланированных сообщений"""
    user = update.effective_user
    
    # Проверяем права администратора
    if ADMIN_ID and str(user.id) != ADMIN_ID:
        await update.message.reply_text("⛔ Эта команда только для администратора.")
        return
    
    logger.info("=== РУЧНОЙ ТЕСТ ПЛАНИРОВЩИКА ===")
    
    await update.message.reply_text("🔄 Тестируем утреннее сообщение...")
    await send_morning_to_maxim(context)
    
    await asyncio.sleep(2)
    
    await update.message.reply_text("🔄 Тестируем вечернее сообщение...")
    await send_evening_to_maxim(context)
    
    await update.message.reply_text("✅ Тест завершён. Проверьте логи.")

async def job_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает статус запланированных задач"""
    user = update.effective_user
    
    if ADMIN_ID and str(user.id) != ADMIN_ID:
        await update.message.reply_text("⛔ Эта команда только для администратора.")
        return
    
    jq = context.application.job_queue
    jobs = jq.jobs()
    
    status_text = "📋 **Статус запланированных задач:**\n\n"
    
    if not jobs:
        status_text += "❌ Нет активных задач\n"
    else:
        for i, job in enumerate(jobs, 1):
            status_text += f"{i}. **{job.name}**\n"
            if hasattr(job, 'next_t') and job.next_t:
                status_text += f"   🕐 Следующий запуск: {job.next_t}\n"
            if hasattr(job, 'time') and job.time:
                status_text += f"   ⏰ Время: {job.time}\n"
            status_text += "\n"
    
    tz = get_tz()
    now = datetime.now(tz)
    status_text += f"\n🕐 **Текущее время:** {now.strftime('%H:%M:%S %d.%m.%Y')}"
    status_text += f"\n🌐 **Часовой пояс:** {BOT_TZ}"
    status_text += f"\n👤 **Максим ID:** {MAXIM_ID}"
    status_text += f"\n💬 **Группа ID:** {GROUP_CHAT_ID}"
    
    await update.message.reply_text(status_text, parse_mode="Markdown")

async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Очищает историю диалогов"""
    user = update.effective_user
    
    if ADMIN_ID and str(user.id) != ADMIN_ID:
        await update.message.reply_text("⛔ Эта команда только для администратора.")
        return
    
    global dialog_history
    old_count = len(dialog_history)
    dialog_history.clear()
    
    await update.message.reply_text(f"✅ История очищена. Удалено {old_count} диалогов.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик всех сообщений"""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    
    if not msg or not chat or not user:
        return

    text = msg.text or ""
    if not text.strip():
        return
    
    # Пропускаем сообщения от самого бота
    if user.id == context.bot.id:
        return
    
    # Определяем тип пользователя
    user_type = determine_user_type(update)
    user_name = await get_user_display_name(update, context)
    
    logger.info(f"👤 Сообщение от {user_name} (ID: {user.id}, Тип: {user_type.value})")
    
    # ---- ФИЛЬТР ДЛЯ ГРУПП ----
    if chat.type in ("group", "supergroup"):
        # Получаем username бота
        bot_username = context.bot.username
        if not bot_username:
            me = await context.bot.get_me()
            bot_username = me.username or ""
        
        text_lower = text.lower()
        bot_username_lower = bot_username.lower()
        
        mentioned_by_name = "лейла" in text_lower
        mentioned_by_username = bot_username_lower and f"@{bot_username_lower}" in text_lower
        reply_to_bot = (
            msg.reply_to_message is not None
            and msg.reply_to_message.from_user is not None
            and msg.reply_to_message.from_user.id == context.bot.id
        )
        
        # Лейла отвечает только если:
        # 1. Это Максим
        # 2. Её упомянули по имени или username
        # 3. Ответили на её сообщение
        if not (user_type == UserType.MAXIM or mentioned_by_name or mentioned_by_username or reply_to_bot):
            logger.info(f"⚠️ Пропускаем сообщение от {user_name} (не Максим и не упоминание)")
            return
    
    chat_id = chat.id
    user_id = user.id
    history_key = history_key_for(user_id, chat_id)
    
    # Для Максима иногда пропускаем ответ для естественности
    if user_type == UserType.MAXIM and random.random() < 0.15:
        logger.info(f"💭 Пропускаем ответ Максиму для естественности")
        return
    
    # Подготавливаем контекст
    extra_context = {}
    
    # Добавляем погоду если упоминается
    if any(word in text.lower() for word in ["погод", "температур", "холодно", "жарко", "дождь"]):
        weather = await fetch_weather()
        if weather:
            extra_context["weather"] = weather["text"]
    
    # Добавляем время суток в контекст
    time_of_day = get_time_of_day()
    time_contexts = {
        TimeOfDay.MORNING: "Сейчас утро",
        TimeOfDay.DAY: "Сейчас день",
        TimeOfDay.EVENING: "Сейчас вечер",
        TimeOfDay.NIGHT: "Сейчас ночь"
    }
    extra_context["time_context"] = time_contexts[time_of_day]
    
    # Генерируем ответ
    reply = await generate_leila_response(text, user_name, user_type, history_key, extra_context)
    
    # Отправляем сообщение
    try:
        await context.bot.send_message(chat_id=chat.id, text=reply)
        logger.info(f"✅ Ответ отправлен {user_name}")
    except Exception as e:
        logger.error(f"❌ Ошибка отправки сообщения: {e}")

# ========== ПЛАНОВЫЕ СООБЩЕНИЯ ==========

async def send_morning_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    """8:30 — утреннее сообщение Максиму"""
    logger.info("=== НАЧАЛО send_morning_to_maxim ===")
    
    if not GROUP_CHAT_ID:
        logger.error("❌ GROUP_CHAT_ID не задан!")
        return
    
    if not validate_group_chat_id():
        logger.error("❌ GROUP_CHAT_ID невалиден!")
        return
    
    try:
        logger.info(f"✅ GROUP_CHAT_ID: {GROUP_CHAT_ID}")
        
        # Проверяем клиент DeepSeek
        if not client:
            logger.error("❌ DeepSeek клиент не инициализирован!")
            fallback = "Доброе утро, мой дорогой... Пусть день будет прекрасным ☀️💖"
            try:
                await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=fallback)
                logger.info(f"✅ Отправлен фолбэк: {fallback[:50]}...")
            except Exception as e:
                logger.error(f"❌ Ошибка отправки фолбэка: {e}")
            return
        
        # Проверяем подключение к боту
        try:
            me = await context.bot.get_me()
            logger.info(f"✅ Бот активен: {me.username} (ID: {me.id})")
        except Exception as e:
            logger.error(f"❌ Бот не доступен: {e}")
            return
        
        # Генерируем сообщение
        morning_prompts = [
            "Придумай нежное утреннее приветствие для Максима от Лейлы.",
            "Лейла просыпается и первым делом думает о Максиме. Напиши её сообщение.",
            "Создай тёплое, романтичное утреннее сообщение для любимого мужчины.",
            "Лейла хочет пожелать Максиму хорошего дня. Напиши её сообщение с утренним флиртом."
        ]
        
        prompt = random.choice(morning_prompts)
        weather = await fetch_weather()
        if weather:
            prompt += f"\n\nПогода сегодня: {weather['text']}. Аккуратно вплети это в сообщение."
        
        # Генерируем системный промпт для Максима
        mood = Mood.TENDER_CARING
        time_of_day = TimeOfDay.MORNING
        season = get_season()
        
        system_prompt = generate_system_prompt_for_user(
            UserType.MAXIM, 
            "Максим", 
            mood, 
            time_of_day, 
            season
        )
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        logger.info(f"📤 Отправка запроса к DeepSeek...")
        answer = await call_deepseek(messages, max_tokens=120, temperature=0.8)
        
        if answer:
            logger.info(f"✅ Получен ответ от DeepSeek: {answer[:50]}...")
            try:
                await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=answer)
                logger.info(f"✅ Сообщение отправлено в чат {GROUP_CHAT_ID}")
            except Exception as send_error:
                logger.error(f"❌ Ошибка отправки в Telegram: {send_error}")
                # Пробуем отправить простой текст
                try:
                    fallback_text = random.choice([
                        "Доброе утро, мой хороший... 🌞💕",
                        "С добрым утром, солнышко! ☀️😊",
                        "Проснись, мой милый, новый день ждёт! 💫🌸"
                    ])
                    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=fallback_text)
                    logger.info(f"✅ Отправлен простой текст: {fallback_text[:30]}...")
                except Exception as e2:
                    logger.error(f"❌ Критическая ошибка отправки: {e2}")
        else:
            logger.warning("⚠️ DeepSeek не вернул ответ")
            fallback = random.choice([
                "Доброе утро, солнышко! Пусть этот день подарит тебе улыбки ☀️😊",
                "С добрым утром, мой дорогой... 🌸💖",
                "Проснись, мой хороший, день начинается! ☀️💫"
            ])
            try:
                await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=fallback)
                logger.info(f"✅ Отправлен фолбэк: {fallback[:50]}...")
            except Exception as e:
                logger.error(f"❌ Ошибка отправки фолбэка: {e}")
            
    except Exception as e:
        logger.error(f"❌ Общая ошибка в send_morning_to_maxim: {e}", exc_info=True)
    finally:
        logger.info("=== КОНЕЦ send_morning_to_maxim ===")

async def send_evening_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    """21:10 — вечернее сообщение Максиму"""
    logger.info("=== НАЧАЛО send_evening_to_maxim ===")
    
    if not GROUP_CHAT_ID:
        logger.error("❌ GROUP_CHAT_ID не задан!")
        return
    
    if not validate_group_chat_id():
        logger.error("❌ GROUP_CHAT_ID невалиден!")
        return
    
    try:
        logger.info(f"✅ GROUP_CHAT_ID: {GROUP_CHAT_ID}")
        
        if not client:
            logger.error("❌ DeepSeek клиент не инициализирован!")
            fallback = "Спокойной ночи, мой дорогой... Пусть сны будут сладкими 🌙💖"
            try:
                await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=fallback)
                logger.info(f"✅ Отправлен фолбэк: {fallback[:50]}...")
            except Exception as e:
                logger.error(f"❌ Ошибка отправки фолбэка: {e}")
            return
        
        # Разные варианты вечерних промптов
        evening_prompts = [
            "Напиши тёплое, уютное пожелание спокойной ночи для Максима от Лейлы.",
            "Вечер, Лейла пишет Максиму перед сном. Какое самое нежное сообщение на ночь она может отправить?",
            "Лейла хочет, чтобы Максим заснул с хорошими мыслями. Напиши её вечернее сообщение.",
            "Создай интимное, романтичное пожелание спокойной ночи для любимого мужчины."
        ]
        
        prompt = random.choice(evening_prompts)
        
        # Генерируем системный промпт
        mood = random.choice([Mood.TENDER_CARING, Mood.ROMANTIC_DREAMY, Mood.MYSTERIOUS_INTIMATE])
        time_of_day = TimeOfDay.EVENING
        season = get_season()
        
        system_prompt = generate_system_prompt_for_user(
            UserType.MAXIM,
            "Максим",
            mood,
            time_of_day,
            season
        )
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        logger.info(f"📤 Отправка запроса к DeepSeek...")
        answer = await call_deepseek(messages, max_tokens=120, temperature=0.8)
        
        if answer:
            logger.info(f"✅ Получен ответ от DeepSeek: {answer[:50]}...")
            try:
                await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=answer)
                logger.info(f"✅ Сообщение отправлено в чат {GROUP_CHAT_ID}")
            except Exception as send_error:
                logger.error(f"❌ Ошибка отправки в Telegram: {send_error}")
                fallback_text = random.choice([
                    "Спокойной ночи, мой милый... 🌙💫",
                    "Отдыхай хорошо, солнышко... 💖",
                    "Сладких снов, мой дорогой... 🌌"
                ])
                try:
                    await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=fallback_text)
                    logger.info(f"✅ Отправлен простой текст")
                except Exception as e2:
                    logger.error(f"❌ Критическая ошибка отправки: {e2}")
        else:
            logger.warning("⚠️ DeepSeek не вернул ответ")
            fallback = random.choice([
                "Спокойной ночи, мой дорогой... Пусть сны будут сладкими 🌙💫",
                "Засыпай с мыслью, что ты кому-то очень дорог... 💖",
                "Ночь опускает свой тёплый плащ... Отдыхай, мой хороший 🌌"
            ])
            try:
                await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=fallback)
                logger.info(f"✅ Отправлен фолбэк: {fallback[:50]}...")
            except Exception as e:
                logger.error(f"❌ Ошибка отправки фолбэка: {e}")
            
    except Exception as e:
        logger.error(f"❌ Общая ошибка в send_evening_to_maxim: {e}", exc_info=True)
    finally:
        logger.info("=== КОНЕЦ send_evening_to_maxim ===")

async def send_random_affection(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Случайные сообщения в течение дня"""
    logger.info("Запущена задача send_random_affection")
    
    if not GROUP_CHAT_ID:
        return
    
    if not validate_group_chat_id():
        return
    
    try:
        # Случайно решаем, отправлять ли сообщение (50% шанс)
        if random.random() < 0.5:
            logger.info("⏭️ Пропускаем случайное сообщение (случайный выбор)")
            return
        
        # Разные типы случайных сообщений
        random_prompts = [
            "Лейла просто хочет напомнить Максиму, что он у неё на уме. Короткое, милое сообщение.",
            "Лейле стало скучно и она решила написать Максиму просто так. Игривое сообщение.",
            "Лейла заметила что-то красивое и сразу подумала о Максиме. Романтичное сообщение."
        ]
        
        prompt = random.choice(random_prompts)
        mood = get_random_mood()
        time_of_day = get_time_of_day()
        season = get_season()
        
        system_prompt = generate_system_prompt_for_user(
            UserType.MAXIM,
            "Максим",
            mood,
            time_of_day,
            season
        )
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        answer = await call_deepseek(messages, max_tokens=80, temperature=0.9)
        
        if answer:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=answer)
            logger.info(f"✅ Отправлено случайное сообщение: {answer[:50]}...")
            
    except Exception as e:
        logger.error(f"❌ Ошибка в send_random_affection: {e}")

# ========== MAIN ==========

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    if not GROUP_CHAT_ID:
        raise RuntimeError("GROUP_CHAT_ID не задан")

    # Выводим информацию при запуске
    print_startup_info()
    
    # Валидируем настройки
    if not validate_group_chat_id():
        logger.error("❌ Проверка GROUP_CHAT_ID не пройдена!")
        return
    
    logger.info("🚀 Запуск бота Лейла...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Основные команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test", test_scheduled))
    app.add_handler(CommandHandler("jobs", job_status))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Планировщик
    tz = get_tz()
    jq = app.job_queue
    
    # УДАЛИТЬ СТАРЫЕ ЗАДАЧИ (важно!)
    logger.info("🧹 Очистка старых задач...")
    for job in jq.jobs():
        logger.info(f"🗑️ Удаляю старую задачу: {job.name}")
        job.schedule_removal()
    
    # Дать время на очистку
    import time as time_module
    time_module.sleep(1)
    
    # ДОБАВИТЬ НОВЫЕ ЗАДАЧИ с проверкой
    logger.info("📅 Добавление новых задач...")
    
    morning_time = time(hour=8, minute=30, tzinfo=tz)
    evening_time = time(hour=21, minute=10, tzinfo=tz)
    
    # Тест: добавим задачу на ближайшую минуту для проверки
    test_time = datetime.now(tz)
    test_time = test_time.replace(second=0, microsecond=0)
    test_time = test_time.replace(minute=test_time.minute + 1)  # Через 1 минуту
    
    jq.run_once(
        send_morning_to_maxim,
        when=test_time,
        name="test-immediate-morning"
    )
    logger.info(f"🧪 Добавлен тестовый запуск на {test_time.strftime('%H:%M:%S')}")
    
    # Основные задачи
    jq.run_daily(
        send_morning_to_maxim,
        time=morning_time,
        name="leila-morning-8-30"
    )
    logger.info(f"🌅 Добавлено утреннее сообщение на {morning_time}")
    
    jq.run_daily(
        send_evening_to_maxim,
        time=evening_time,
        name="leila-evening-21-10"
    )
    logger.info(f"🌃 Добавлено вечернее сообщение на {evening_time}")
    
    # Случайные сообщения в течение дня
    jq.run_daily(
        send_random_affection,
        time=time(hour=14, minute=0, tzinfo=tz),
        name="leila-random-day"
    )
    logger.info("💌 Добавлено случайное дневное сообщение на 14:00")
    
    jq.run_daily(
        send_random_affection,
        time=time(hour=19, minute=0, tzinfo=tz),
        name="leila-random-evening"
    )
    logger.info("💌 Добавлено случайное вечернее сообщение на 19:00")
    
    # Запустить бота
    logger.info("🤖 Бот запущен и готов к работе!")
    logger.info("📝 Доступные команды: /start, /test (админ), /jobs (админ), /clear (админ)")
    
    try:
        app.run_polling()
    except Exception as e:
        logger.error(f"❌ Критическая ошибка при запуске бота: {e}", exc_info=True)

if __name__ == "__main__":
    main()
