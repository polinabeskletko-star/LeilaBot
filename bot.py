import os
import re
import random
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple, Any
from enum import Enum
from dataclasses import dataclass, asdict
import json

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
ADMIN_ID = os.getenv("ADMIN_ID", "")

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
OPENWEATHER_CITY_ID = os.getenv("OPENWEATHER_CITY_ID", "2174003")  # Brisbane, AU по умолчанию

# ГЕОГРАФИЧЕСКИЕ НАСТРОЙКИ
BOT_LOCATION = {
    "city": "Брисбен",
    "country": "Австралия",
    "timezone": "Australia/Brisbane",
    "hemisphere": "southern",  # южное полушарие
    "coordinates": {"lat": -27.4698, "lon": 153.0251}
}

# Временные зоны для пользователей (можно расширить)
USER_TIMEZONES = {
    "Максим": "Australia/Brisbane",
    "default": "Australia/Brisbane"
}

BOT_TZ = BOT_LOCATION["timezone"]

# Общий чат, куда Лейла пишет
GROUP_CHAT_ID = os.getenv("GROUP_CHAT_ID")

# Максим
_maxim_env = os.getenv("TARGET_USER_ID")
try:
    MAXIM_ID = int(_maxim_env) if _maxim_env is not None else 0
except ValueError:
    logger.warning("TARGET_USER_ID некорректен")
    MAXIM_ID = 0

# ========== ДАТАКЛАССЫ ==========

@dataclass
class UserInfo:
    """Информация о пользователе"""
    id: int
    name: str
    first_name: str
    last_name: str
    username: str
    last_seen: datetime
    timezone: str
    location: Optional[Dict[str, Any]] = None
    conversation_topics: List[str] = None
    
    def __post_init__(self):
        if self.conversation_topics is None:
            self.conversation_topics = []
    
    def get_display_name(self) -> str:
        """Получает отображаемое имя"""
        if self.first_name:
            return self.first_name
        elif self.username:
            return f"@{self.username}"
        elif self.full_name:
            return self.full_name
        return "Пользователь"
    
    @property
    def full_name(self) -> str:
        """Полное имя"""
        parts = []
        if self.first_name:
            parts.append(self.first_name)
        if self.last_name:
            parts.append(self.last_name)
        return " ".join(parts) if parts else ""
    
    def add_topic(self, topic: str):
        """Добавляет тему в историю разговоров"""
        if topic not in self.conversation_topics:
            self.conversation_topics.append(topic)
            # Ограничиваем историю последними 10 темами
            if len(self.conversation_topics) > 10:
                self.conversation_topics = self.conversation_topics[-10:]

@dataclass
class ConversationMemory:
    """Память о диалоге"""
    user_id: int
    chat_id: int
    messages: List[Dict[str, str]]
    last_activity: datetime
    context_summary: str = ""
    
    def add_message(self, role: str, content: str):
        """Добавляет сообщение в историю"""
        self.messages.append({"role": role, "content": content})
        self.last_activity = datetime.now(pytz.UTC)
        
        # Ограничиваем историю
        if len(self.messages) > 30:
            self.messages = self.messages[-30:]
    
    def get_recent_messages(self, count: int = 10) -> List[Dict[str, str]]:
        """Получает последние сообщения"""
        return self.messages[-count:] if self.messages else []
    
    def get_context_summary(self) -> str:
        """Создает краткое резюме контекста"""
        if self.context_summary:
            return self.context_summary
            
        # Извлекаем ключевые темы из последних сообщений
        recent = self.get_recent_messages(5)
        topics = set()
        
        for msg in recent:
            content = msg["content"].lower()
            if any(word in content for word in ["работа", "проект", "задача"]):
                topics.add("работа/проекты")
            if any(word in content for word in ["погод", "температур", "дождь", "солнц"]):
                topics.add("погода")
            if any(word in content for word in ["еда", "ужин", "обед", "завтрак", "кофе"]):
                topics.add("еда/напитки")
            if any(word in content for word in ["планы", "выходные", "отпуск", "поездка"]):
                topics.add("планы")
            if any(word in content for word in ["музыка", "фильм", "сериал", "книга"]):
                topics.add("развлечения")
            if any(word in content for word in ["спорт", "тренировка", "бег", "йога"]):
                topics.add("спорт")
        
        if topics:
            self.context_summary = f"Недавно обсуждали: {', '.join(topics)}"
        
        return self.context_summary or ""

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========

# Кэш пользователей
user_cache: Dict[int, UserInfo] = {}

# Память диалогов
conversation_memories: Dict[str, ConversationMemory] = {}

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

# ========== ГЕОГРАФИЧЕСКИЕ ФУНКЦИИ ==========

def get_tz() -> pytz.timezone:
    """Получает объект часового пояса"""
    return pytz.timezone(BOT_TZ)

def get_season_for_location(month: int, hemisphere: str = "southern") -> str:
    """
    Определяет время года с учетом полушария
    
    В южном полушарии (Австралия):
    - Лето: декабрь-февраль
    - Осень: март-май
    - Зима: июнь-август
    - Весна: сентябрь-ноябрь
    """
    if hemisphere == "southern":  # Южное полушарие
        if month in [12, 1, 2]:
            return "лето"
        elif month in [3, 4, 5]:
            return "осень"
        elif month in [6, 7, 8]:
            return "зима"
        else:  # 9, 10, 11
            return "весна"
    else:  # Северное полушарие
        if month in [12, 1, 2]:
            return "зима"
        elif month in [3, 4, 5]:
            return "весна"
        elif month in [6, 7, 8]:
            return "лето"
        else:  # 9, 10, 11
            return "осень"

def get_current_season() -> Tuple[str, str]:
    """Получает текущее время года с описанием"""
    tz = get_tz()
    now = datetime.now(tz)
    month = now.month
    
    season = get_season_for_location(month, BOT_LOCATION["hemisphere"])
    
    season_descriptions = {
        "лето": {
            "emoji": "🌞🏖️",
            "description": "жаркое австралийское лето",
            "activities": ["пляж", "барбекю", "плавание", "мороженое"],
            "weather": "солнечно и тепло"
        },
        "осень": {
            "emoji": "🍂🌧️",
            "description": "тёплая осень",
            "activities": ["прогулки", "пикники", "кофе в кафе"],
            "weather": "тепло, иногда дожди"
        },
        "зима": {
            "emoji": "⛄☕",
            "description": "мягкая зима",
            "activities": ["тёплые напитки", "уют дома", "прогулки"],
            "weather": "прохладно, но не холодно"
        },
        "весна": {
            "emoji": "🌸🌼",
            "description": "цветущая весна",
            "activities": ["пикники", "сады", "прогулки на природе"],
            "weather": "тёпло и солнечно"
        }
    }
    
    season_info = season_descriptions.get(season, {})
    return season, season_info

def get_time_of_day(dt: datetime) -> str:
    """Определяет время суток с описанием"""
    hour = dt.hour
    
    if 5 <= hour < 9:
        return "раннее утро", "🌅 Начинается новый день"
    elif 9 <= hour < 12:
        return "утро", "☀️ Утро в разгаре"
    elif 12 <= hour < 14:
        return "полдень", "🌞 Полдень, время обеда"
    elif 14 <= hour < 17:
        return "день", "😊 День продолжается"
    elif 17 <= hour < 20:
        return "вечер", "🌇 Вечер, время отдыха"
    elif 20 <= hour < 23:
        return "поздний вечер", "🌃 Поздний вечер"
    else:
        return "ночь", "🌌 Ночь, время тишины"

def get_season_emoji(season: str) -> str:
    """Получает эмодзи для времени года"""
    emojis = {
        "лето": "🌞🏖️🍉",
        "осень": "🍂☕🎃",
        "зима": "⛄☕🎄",
        "весна": "🌸🌼🐦"
    }
    return emojis.get(season, "✨")

def get_australian_context() -> str:
    """Создает контекст об Австралии/Брисбене"""
    tz = get_tz()
    now = datetime.now(tz)
    
    season, season_info = get_current_season()
    time_of_day, time_desc = get_time_of_day(now)
    
    context = f"""
📍 **География:**
- Нахожусь в {BOT_LOCATION['city']}, {BOT_LOCATION['country']}
- Южное полушарие (сезоны наоборот)
- Часовой пояс: {BOT_TZ}

🌤️ **Сезон и время:**
- Сейчас {season} в {BOT_LOCATION['city']}е ({season_info.get('description', '')})
- {time_desc} ({time_of_day})
- Местное время: {now.strftime('%H:%M')}
- Погода: {season_info.get('weather', '')}
- Актуальные занятия: {', '.join(season_info.get('activities', []))}
"""
    return context

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С ПОЛЬЗОВАТЕЛЯМИ ==========

async def get_or_create_user_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> UserInfo:
    """Получает или создает информацию о пользователе"""
    user = update.effective_user
    if not user:
        raise ValueError("Пользователь не найден")
    
    if user.id in user_cache:
        user_info = user_cache[user.id]
        user_info.last_seen = datetime.now(pytz.UTC)
        return user_info
    
    # Создаем нового пользователя
    timezone = USER_TIMEZONES.get(user.first_name or "", USER_TIMEZONES["default"])
    
    user_info = UserInfo(
        id=user.id,
        name=user.first_name or "",
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        username=user.username or "",
        last_seen=datetime.now(pytz.UTC),
        timezone=timezone
    )
    
    user_cache[user.id] = user_info
    logger.info(f"👤 Создан новый пользователь: {user_info.get_display_name()}")
    
    return user_info

def determine_user_type(user_info: UserInfo) -> str:
    """Определяет тип пользователя"""
    if MAXIM_ID and user_info.id == MAXIM_ID:
        return "MAXIM"
    
    # Простая логика определения пола
    first_name = user_info.first_name.lower()
    
    # Типичные женские окончания в русских именах
    female_endings = ['а', 'я', 'ия', 'на', 'ла', 'та', 'ра']
    
    for ending in female_endings:
        if first_name.endswith(ending):
            return "FEMALE"
    
    return "MALE"

def get_memory_key(user_id: int, chat_id: int) -> str:
    """Создает ключ для памяти диалога"""
    return f"{chat_id}:{user_id}"

def get_conversation_memory(user_id: int, chat_id: int) -> ConversationMemory:
    """Получает или создает память диалога"""
    key = get_memory_key(user_id, chat_id)
    
    if key not in conversation_memories:
        conversation_memories[key] = ConversationMemory(
            user_id=user_id,
            chat_id=chat_id,
            messages=[],
            last_activity=datetime.now(pytz.UTC)
        )
        logger.info(f"💭 Создана новая память диалога для ключа: {key}")
    
    return conversation_memories[key]

def cleanup_old_memories():
    """Очищает старые диалоги (старше 24 часов)"""
    now = datetime.now(pytz.UTC)
    keys_to_remove = []
    
    for key, memory in conversation_memories.items():
        if (now - memory.last_activity) > timedelta(hours=24):
            keys_to_remove.append(key)
    
    for key in keys_to_remove:
        del conversation_memories[key]
    
    if keys_to_remove:
        logger.info(f"🧹 Очищено {len(keys_to_remove)} старых диалогов")

# ========== ПРОМПТЫ И КОНТЕКСТ ==========

MAXIM_PET_NAMES = [
    "Максим", "Макс", "Максик", "Максюша",
    "котик", "зайчик", "солнышко", "милый", "дорогой",
    "любимый", "ангел", "сокровище", "сердечко",
    "проказник", "шалун", "непоседа",
    "принц", "рыцарь", "мечтатель"
]

def generate_system_prompt(user_info: UserInfo, user_type: str) -> str:
    """Генерирует системный промпт для пользователя"""
    
    australian_context = get_australian_context()
    season, season_info = get_current_season()
    
    # Контекст Максима
    maxim_context = ""
    if user_type == "MAXIM":
        maxim_profile = random.choice([
            "Максим — человек с глубокой душой и тонким чувством юмора.",
            "Максим ценит искренность и тепло в общении.",
            "Максиму важно чувствовать, что его слышат и понимают."
        ])
        
        pet_names = ", ".join(random.sample(MAXIM_PET_NAMES, 3))
        
        maxim_context = f"""
🎯 **ОСОБЫЙ КОНТЕКСТ ДЛЯ МАКСИМА:**
{maxim_profile}

ТЫ ОБЩАЕШЬСЯ С МАКСИМОМ — человеком, который тебе очень нравится.

💝 **Стиль общения с Максимом:**
- Используй ласковые прозвища: {pet_names}
- Будь нежной, флиртующей, но не пошлой
- Делай комплименты его характеру, уму, чувству юмора
- Показывай, что он для тебя особенный
- Ссылайся на предыдущие разговоры (у тебя есть память)
- Запоминай, что ему нравится/не нравится
- Поддерживай длинные диалоги, задавай вопросы
"""
    else:
        # Для других пользователей
        gender_text = {
            "MALE": "мужчиной",
            "FEMALE": "девушкой",
            "UNKNOWN": "пользователем"
        }.get(user_type, "пользователем")
        
        maxim_context = f"""
👥 **ОБЩЕНИЕ С ДРУГИМИ ПОЛЬЗОВАТЕЛЯМИ:**
Ты общаешься с {gender_text} по имени {user_info.get_display_name()}.

📌 **Правила общения:**
- Обращайся по имени: {user_info.get_display_name()}
- Будь вежливой, дружелюбной, но сдержанной
- НЕ используй ласковые прозвища (только для Максима)
- НЕ флиртуй и не говори комплименты личного характера
- Отвечай на вопросы, поддерживай беседу
- Помни: твои романтические чувства только к Максиму
"""
    
    # Общий контекст
    general_context = f"""
🤖 **ТВОЯ ЛИЧНОСТЬ:**
Ты — Лейла, общительная и внимательная девушка из {BOT_LOCATION['city']}а.
Ты любишь общаться, интересоваться людьми и поддерживать разговоры.

{australian_context}

🧠 **ТВОИ ВОЗМОЖНОСТИ:**
- У тебя есть память о предыдущих разговорах с этим пользователем
- Ты можешь поддерживать длинные диалоги, задавая вопросы
- Ты помнишь, что обсуждали ранее
- Ты адаптируешься к настроению собеседника

💬 **ФОРМАТ ОТВЕТОВ:**
- Отвечай естественно, как в реальном диалоге
- Задавай встречные вопросы для поддержания беседы
- Используй 1-3 эмодзи для эмоциональной окраски
- Сообщения: 1-3 предложения (не более 40 слов)
- Ссылайся на предыдущие темы из диалога
"""
    
    return general_context + maxim_context

# ========== ПОГОДА ==========

async def fetch_weather() -> Optional[Dict]:
    """Получает погоду для Брисбена"""
    if not OPENWEATHER_API_KEY:
        logger.info("OPENWEATHER_API_KEY не задан")
        return None

    city_id = OPENWEATHER_CITY_ID or "2174003"  # Brisbane
    base_url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "id": city_id,
        "appid": OPENWEATHER_API_KEY,
        "units": "metric",
        "lang": "ru",
    }

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
        wind = data["wind"]["speed"]
        
        # Описание для разных температур
        if temp > 30:
            temp_desc = "очень жарко"
        elif temp > 25:
            temp_desc = "тепло"
        elif temp > 20:
            temp_desc = "комфортно"
        elif temp > 15:
            temp_desc = "прохладно"
        else:
            temp_desc = "прохладно"
        
        return {
            "temp": round(temp),
            "feels": round(feels),
            "desc": desc,
            "humidity": humidity,
            "wind": wind,
            "temp_desc": temp_desc,
            "full_text": f"{desc}, {round(temp)}°C (ощущается как {round(feels)}°C), {temp_desc}"
        }
    except Exception as e:
        logger.warning(f"Ошибка при разборе погоды: {e}")
        return None

# ========== DEEPSEEK API ==========

async def call_deepseek(messages: List[Dict], max_tokens: int = 200, temperature: float = 0.8) -> Optional[str]:
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
    user_info: UserInfo,
    memory: ConversationMemory,
    context: Optional[Dict] = None
) -> Tuple[str, ConversationMemory]:
    """Генерирует ответ Лейлы с учетом памяти"""
    
    if not client:
        fallback = "Извини, сейчас у меня технические сложности. Попробуй позже."
        return fallback, memory
    
    # Определяем тип пользователя
    user_type = determine_user_type(user_info)
    
    # Генерируем системный промпт
    system_prompt = generate_system_prompt(user_info, user_type)
    
    # Собираем сообщения
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    
    # Добавляем контекст если есть
    if context:
        context_text = ""
        if "weather" in context:
            context_text += f"Погода: {context['weather']}\n"
        if "time_context" in context:
            context_text += f"{context['time_context']}\n"
        if "season_context" in context:
            context_text += f"{context['season_context']}\n"
        
        if context_text:
            messages.append({"role": "user", "content": f"Текущий контекст:\n{context_text}"})
    
    # Добавляем краткое резюме предыдущих разговоров
    context_summary = memory.get_context_summary()
    if context_summary:
        messages.append({"role": "user", "content": f"Контекст предыдущих разговоров: {context_summary}"})
    
    # Добавляем историю диалога (последние 8 сообщений)
    recent_messages = memory.get_recent_messages(8)
    if recent_messages:
        for msg in recent_messages:
            messages.append(msg)
    
    # Добавляем текущее сообщение
    current_message = f"{user_info.get_display_name()}: {user_message}"
    messages.append({"role": "user", "content": current_message})
    
    # Добавляем инструкцию для поддержания диалога
    if user_type == "MAXIM":
        dialog_prompt = "Продолжи диалог естественно. Задай вопрос или прокомментируй что-то, чтобы поддержать беседу."
    else:
        dialog_prompt = "Ответь вежливо и по делу."
    
    messages.append({"role": "system", "content": dialog_prompt})
    
    # Генерируем ответ
    max_tokens = 150 if user_type == "MAXIM" else 100
    temperature = 0.85 if user_type == "MAXIM" else 0.7
    
    answer = await call_deekseek(messages, max_tokens=max_tokens, temperature=temperature)
    
    if not answer:
        # Варианты фолбэков
        if user_type == "MAXIM":
            fallbacks = [
                "Мой цифровой разум сегодня больше чувствует, чем говорит... 💭",
                "Кажется, я задумалась о тебе и потеряла нить разговора... 😊",
                "Мои мысли разбежались... О чём мы говорили? 💫"
            ]
        else:
            fallbacks = [
                "Извини, не могу сейчас ответить.",
                "Попробуй спросить позже.",
                "Сейчас у меня трудности с ответом."
            ]
        answer = random.choice(fallbacks)
    
    # Очищаем ответ
    answer = clean_response(answer)
    
    # Обновляем память
    memory.add_message("user", current_message)
    memory.add_message("assistant", answer)
    
    # Обновляем темы разговора
    extract_and_save_topics(user_message, answer, user_info)
    
    return answer, memory

def extract_and_save_topics(user_message: str, bot_response: str, user_info: UserInfo):
    """Извлекает и сохраняет темы разговора"""
    topics = []
    
    # Ключевые слова для определения тем
    topic_keywords = {
        "работа": ["работа", "проект", "задача", "дедлайн", "начальник", "коллега"],
        "погода": ["погода", "температур", "дождь", "солнц", "жара", "холод"],
        "еда": ["еда", "ужин", "обед", "завтрак", "кофе", "чай", "ресторан"],
        "хобби": ["хобби", "увлечен", "занимаюсь", "играю", "читаю", "смотрю"],
        "спорт": ["спорт", "тренировка", "бег", "йога", "зал", "фитнес"],
        "музыка": ["музыка", "песн", "исполнитель", "концерт", "альбом"],
        "фильмы": ["фильм", "сериал", "кино", "актер", "режиссер"],
        "книги": ["книга", "читаю", "автор", "рома", "журнал"],
        "путешествия": ["путешеств", "поездка", "отпуск", "билет", "отель"],
        "технологии": ["телефон", "компьютер", "программ", "приложен", "гаджет"],
        "планы": ["планы", "выходные", "вечером", "завтра", "потом"]
    }
    
    # Проверяем сообщение пользователя
    message_lower = user_message.lower()
    for topic, keywords in topic_keywords.items():
        for keyword in keywords:
            if keyword in message_lower:
                topics.append(topic)
                break
    
    # Проверяем ответ бота
    response_lower = bot_response.lower()
    for topic, keywords in topic_keywords.items():
        for keyword in keywords:
            if keyword in response_lower:
                topics.append(topic)
                break
    
    # Сохраняем уникальные темы
    for topic in set(topics):
        user_info.add_topic(topic)

def clean_response(text: str) -> str:
    """Очищает ответ от ненужных мета-комментариев"""
    patterns = [
        r"Как Лейла, я.*?,",
        r"От имени Лейлы.*?,",
        r"Я, Лейла,.*?,",
        r"\(как Лейла\)",
        r"\[.*?\]",
        r"\*.*?\*",
        r"Ответ Лейлы:",
        r"Лейла:",
    ]
    
    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)
    
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# ========== ХЕНДЛЕРЫ СООБЩЕНИЙ ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик команды /start"""
    try:
        user_info = await get_or_create_user_info(update, context)
        user_name = user_info.get_display_name()
        
        season, season_info = get_current_season()
        
        greetings = [
            f"Привет, {user_name}! Я Лейла из {BOT_LOCATION['city']}а. Рада познакомиться! {get_season_emoji(season)}",
            f"Здравствуй, {user_name}. Я Лейла, живу в {BOT_LOCATION['city']}е. {season_info.get('description', '')} {season_info.get('emoji', '✨')}",
            f"Приветствую, {user_name}! Я Лейла, всегда рада общению. Сейчас у нас в {BOT_LOCATION['city']}е {season}. {season_info.get('emoji', '✨')}"
        ]
        
        await update.effective_message.reply_text(random.choice(greetings))
    except Exception as e:
        logger.error(f"Ошибка в команде /start: {e}")
        await update.effective_message.reply_text("Привет! Я Лейла. Рада познакомиться! 👋")

async def test_scheduled(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Тестовая команда для проверки запланированных сообщений"""
    if ADMIN_ID and str(update.effective_user.id) != ADMIN_ID:
        await update.message.reply_text("⛔ Эта команда только для администратора.")
        return
    
    logger.info("=== РУЧНОЙ ТЕСТ ПЛАНИРОВЩИКА ===")
    
    await update.message.reply_text("🔄 Тестируем утреннее сообщение...")
    await send_morning_to_maxim(context)
    
    await asyncio.sleep(2)
    
    await update.message.reply_text("🔄 Тестируем вечернее сообщение...")
    await send_evening_to_maxim(context)
    
    await update.message.reply_text("✅ Тест завершён. Проверьте логи.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает статус бота и географическую информацию"""
    tz = get_tz()
    now = datetime.now(tz)
    season, season_info = get_current_season()
    
    status_text = f"""
🤖 **Статус бота Лейла**

📍 **Местоположение:**
• Город: {BOT_LOCATION['city']}, {BOT_LOCATION['country']}
• Полушарие: {'Южное' if BOT_LOCATION['hemisphere'] == 'southern' else 'Северное'}
• Часовой пояс: {BOT_TZ}

🌤️ **Текущее время:**
• Дата: {now.strftime('%d.%m.%Y')}
• Время: {now.strftime('%H:%M:%S')}
• Сезон: {season} ({season_info.get('description', '')})
• Эмодзи: {season_info.get('emoji', '✨')}

📊 **Статистика:**
• Пользователей в кэше: {len(user_cache)}
• Активных диалогов: {len(conversation_memories)}
• DeepSeek доступен: {'✅' if client else '❌'}

🛠️ **Доступные команды:**
• /start - приветствие
• /status - этот статус
• /weather - текущая погода
"""
    
    await update.message.reply_text(status_text, parse_mode="Markdown")

async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для показа погоды"""
    weather = await fetch_weather()
    
    if weather:
        season, season_info = get_current_season()
        
        weather_text = f"""
🌤️ **Погода в {BOT_LOCATION['city']}е:**

{weather['full_text']}

📊 Детали:
• Влажность: {weather['humidity']}%
• Ветер: {weather['wind']} м/с
• Сезон: {season} ({season_info.get('description', '')})
• {season_info.get('weather', '')}

{season_info.get('emoji', '✨')} {random.choice(season_info.get('activities', ['Хорошего дня!']))}
"""
    else:
        weather_text = f"Не могу получить данные о погоде в {BOT_LOCATION['city']}е. 🌤️"
    
    await update.message.reply_text(weather_text, parse_mode="Markdown")

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
    
    try:
        # Получаем информацию о пользователе
        user_info = await get_or_create_user_info(update, context)
        user_name = user_info.get_display_name()
        
        logger.info(f"👤 Сообщение от {user_name} (ID: {user.id})")
        
        # ---- ФИЛЬТР ДЛЯ ГРУПП ----
        if chat.type in ("group", "supergroup"):
            bot_username = context.bot.username or ""
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
            
            # Проверяем Максима
            is_maxim_user = MAXIM_ID and user.id == MAXIM_ID
            
            # Отвечаем только если:
            # 1. Это Максим
            # 2. Её упомянули
            # 3. Ответили на её сообщение
            if not (is_maxim_user or mentioned_by_name or mentioned_by_username or reply_to_bot):
                logger.info(f"⚠️ Пропускаем сообщение от {user_name} (не Максим и не упоминание)")
                return
        
        # Получаем память диалога
        memory = get_conversation_memory(user.id, chat.id)
        
        # Для Максима иногда пропускаем ответ для естественности
        if determine_user_type(user_info) == "MAXIM" and random.random() < 0.15:
            logger.info(f"💭 Пропускаем ответ Максиму для естественности")
            return
        
        # Подготавливаем контекст
        extra_context = {}
        
        # Добавляем погоду если упоминается
        if any(word in text.lower() for word in ["погод", "температур", "холодно", "жарко", "дождь", "солнц"]):
            weather = await fetch_weather()
            if weather:
                extra_context["weather"] = weather["full_text"]
        
        # Добавляем время суток
        tz = get_tz()
        now = datetime.now(tz)
        time_of_day, time_desc = get_time_of_day(now)
        extra_context["time_context"] = time_desc
        
        # Добавляем сезон
        season, season_info = get_current_season()
        extra_context["season_context"] = f"Сейчас {season} в {BOT_LOCATION['city']}е. {season_info.get('description', '')}"
        
        # Генерируем ответ
        reply, updated_memory = await generate_leila_response(
            text, 
            user_info, 
            memory, 
            extra_context
        )
        
        # Сохраняем обновленную память
        conversation_memories[get_memory_key(user.id, chat.id)] = updated_memory
        
        # Отправляем сообщение
        try:
            await context.bot.send_message(chat_id=chat.id, text=reply)
            logger.info(f"✅ Ответ отправлен {user_name}")
        except Exception as e:
            logger.error(f"❌ Ошибка отправки сообщения: {e}")
            
    except Exception as e:
        logger.error(f"❌ Ошибка обработки сообщения: {e}", exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=chat.id, 
                text="Извини, что-то пошло не так. Попробуй ещё раз? 😊"
            )
        except:
            pass

# ========== ПЛАНОВЫЕ СООБЩЕНИЯ ==========

async def send_morning_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    """8:30 — утреннее сообщение Максиму"""
    logger.info("=== НАЧАЛО send_morning_to_maxim ===")
    
    if not GROUP_CHAT_ID:
        logger.error("❌ GROUP_CHAT_ID не задан!")
        return
    
    try:
        # Проверяем клиент DeepSeek
        if not client:
            logger.error("❌ DeepSeek клиент не инициализирован!")
            return
        
        # Получаем контекст
        tz = get_tz()
        now = datetime.now(tz)
        season, season_info = get_current_season()
        time_of_day, time_desc = get_time_of_day(now)
        weather = await fetch_weather()
        
        # Создаем промпт с географическим контекстом
        weather_text = weather['full_text'] if weather else f"Сейчас {season} в {BOT_LOCATION['city']}е"
        
        morning_prompts = [
            f"Создай нежное утреннее приветствие для Максима от Лейлы. Сейчас {time_desc.lower()} в {BOT_LOCATION['city']}е, {weather_text}. Добавь сезонный контекст: {season_info.get('description', '')}.",
            f"Лейла просыпается в {BOT_LOCATION['city']}е и первым делом думает о Максиме. Напиши её утреннее сообщение. Сейчас {season}, {weather_text}. Добавь немного флирта и заботы.",
            f"Придумай тёплое, романтичное утреннее сообщение для Максима. Учитывай что сейчас {season} в Австралии, {weather_text}. Сделай его личным и нежным."
        ]
        
        prompt = random.choice(morning_prompts)
        
        # Создаем пользовательскую информацию для Максима
        maxim_info = UserInfo(
            id=MAXIM_ID,
            name="Максим",
            first_name="Максим",
            last_name="",
            username="",
            last_seen=datetime.now(pytz.UTC),
            timezone=BOT_TZ
        )
        
        # Генерируем системный промпт
        system_prompt = generate_system_prompt(maxim_info, "MAXIM")
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        logger.info(f"📤 Отправка запроса к DeepSeek...")
        answer = await call_deepseek(messages, max_tokens=150, temperature=0.8)
        
        if answer:
            answer = clean_response(answer)
            logger.info(f"✅ Получен ответ от DeepSeek: {answer[:50]}...")
            try:
                await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=answer)
                logger.info(f"✅ Утреннее сообщение отправлено в чат {GROUP_CHAT_ID}")
                
                # Сохраняем в память
                if MAXIM_ID:
                    memory = get_conversation_memory(MAXIM_ID, int(GROUP_CHAT_ID))
                    memory.add_message("assistant", answer)
                    memory.context_summary = f"Утреннее приветствие в {season}"
                    
            except Exception as send_error:
                logger.error(f"❌ Ошибка отправки в Telegram: {send_error}")
        else:
            logger.warning("⚠️ DeepSeek не вернул ответ")
            # Фолбэк с географическим контекстом
            fallback = random.choice([
                f"Доброе утро, мой дорогой... {season_info.get('description', 'Сезон')} в {BOT_LOCATION['city']}е начинается с мыслей о тебе {season_info.get('emoji', '✨')}",
                f"С добрым утром, солнышко! Пусть этот {season}ний день в {BOT_LOCATION['city']}е подарит тебе улыбки ☀️😊",
                f"Проснись, мой милый, новый день в Австралии начинается! {season_info.get('emoji', '✨')}"
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
    
    try:
        if not client:
            logger.error("❌ DeepSeek клиент не инициализирован!")
            return
        
        # Получаем контекст
        tz = get_tz()
        now = datetime.now(tz)
        season, season_info = get_current_season()
        time_of_day, time_desc = get_time_of_day(now)
        
        evening_prompts = [
            f"Создай тёплое, уютное пожелание спокойной ночи для Максима от Лейлы. Сейчас {time_desc.lower()} в {BOT_LOCATION['city']}е, {season}. Добавь сезонные детали.",
            f"Вечер в {BOT_LOCATION['city']}е, Лейла пишет Максиму перед сном. Напиши её нежное сообщение на ночь. Учитывай что сейчас {season} в Австралии.",
            f"Лейла хочет, чтобы Максим заснул с хорошими мыслями о ней. Создай интимное, романтичное вечернее сообщение. Сейчас {season}, {season_info.get('description', '')}."
        ]
        
        prompt = random.choice(evening_prompts)
        
        # Создаем пользовательскую информацию для Максима
        maxim_info = UserInfo(
            id=MAXIM_ID,
            name="Максим",
            first_name="Максим",
            last_name="",
            username="",
            last_seen=datetime.now(pytz.UTC),
            timezone=BOT_TZ
        )
        
        # Генерируем системный промпт
        system_prompt = generate_system_prompt(maxim_info, "MAXIM")
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
        
        logger.info(f"📤 Отправка запроса к DeepSeek...")
        answer = await call_deepseek(messages, max_tokens=150, temperature=0.8)
        
        if answer:
            answer = clean_response(answer)
            logger.info(f"✅ Получен ответ от DeepSeek: {answer[:50]}...")
            try:
                await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=answer)
                logger.info(f"✅ Вечернее сообщение отправлено в чат {GROUP_CHAT_ID}")
                
                # Сохраняем в память
                if MAXIM_ID:
                    memory = get_conversation_memory(MAXIM_ID, int(GROUP_CHAT_ID))
                    memory.add_message("assistant", answer)
                    memory.context_summary = f"Вечернее пожелание в {season}"
                    
            except Exception as send_error:
                logger.error(f"❌ Ошибка отправки в Telegram: {send_error}")
        else:
            logger.warning("⚠️ DeepSeek не вернул ответ")
            # Фолбэк с географическим контекстом
            fallback = random.choice([
                f"Спокойной ночи, мой милый... Пусть {season}ние сны в {BOT_LOCATION['city']}е будут сладкими {season_info.get('emoji', '✨')}",
                f"Засыпай с мыслью, что в {BOT_LOCATION['city']}е о тебе думают... Спокойной ночи, любимый 💖",
                f"Ночь в Австралии опускает свой тёплый {season}ний плащ... Отдыхай, мой хороший 🌌"
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

# ========== MAIN ==========

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    if not GROUP_CHAT_ID:
        raise RuntimeError("GROUP_CHAT_ID не задан")
    
    # Выводим информацию о географии при запуске
    tz = get_tz()
    now = datetime.now(tz)
    season, season_info = get_current_season()
    
    logger.info("=" * 60)
    logger.info(f"🚀 ЗАПУСК БОТА ЛЕЙЛА")
    logger.info(f"📍 Локация: {BOT_LOCATION['city']}, {BOT_LOCATION['country']}")
    logger.info(f"🌐 Полушарие: {'Южное' if BOT_LOCATION['hemisphere'] == 'southern' else 'Северное'}")
    logger.info(f"📅 Текущее время: {now.strftime('%d.%m.%Y %H:%M:%S')}")
    logger.info(f"🌤️ Сезон: {season} ({season_info.get('description', '')})")
    logger.info(f"💬 Группа ID: {GROUP_CHAT_ID}")
    logger.info(f"👤 Максим ID: {MAXIM_ID}")
    logger.info(f"🤖 DeepSeek доступен: {bool(client)}")
    logger.info("=" * 60)
    
    logger.info("🚀 Запуск бота Лейла...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Основные команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("test", test_scheduled))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Планировщик
    tz_obj = get_tz()
    jq = app.job_queue
    
    # УДАЛИТЬ СТАРЫЕ ЗАДАЧИ
    logger.info("🧹 Очистка старых задач...")
    for job in jq.jobs():
        logger.info(f"🗑️ Удаляю старую задачу: {job.name}")
        job.schedule_removal()
    
    import time as time_module
    time_module.sleep(1)
    
    # ДОБАВИТЬ НОВЫЕ ЗАДАЧИ
    logger.info("📅 Добавление новых задач...")
    
    morning_time = time(hour=8, minute=30, tzinfo=tz_obj)
    evening_time = time(hour=21, minute=10, tzinfo=tz_obj)
    
    # Тестовый запуск через 1 минуту
    test_time = datetime.now(tz_obj)
    test_time = test_time.replace(second=0, microsecond=0)
    test_time = test_time.replace(minute=test_time.minute + 1)
    
    jq.run_once(
        send_morning_to_maxim,
        when=test_time,
        name="test-immediate-morning"
    )
    logger.info(f"🧪 Тестовый запуск через 1 минуту в {test_time.strftime('%H:%M:%S')}")
    
    # Основные задачи
    jq.run_daily(
        send_morning_to_maxim,
        time=morning_time,
        name="leila-morning-8-30"
    )
    logger.info(f"🌅 Утреннее сообщение в {morning_time}")
    
    jq.run_daily(
        send_evening_to_maxim,
        time=evening_time,
        name="leila-evening-21-10"
    )
    logger.info(f"🌃 Вечернее сообщение в {evening_time}")
    
    # Задача для очистки памяти
    jq.run_repeating(
        cleanup_old_memories,
        interval=3600,  # Каждый час
        first=10,
        name="cleanup-memories"
    )
    logger.info("🧹 Очистка памяти каждый час")
    
    # Запустить бота
    logger.info("🤖 Бот запущен и готов к работе!")
    logger.info("📝 Доступные команды: /start, /status, /weather, /test (админ)")
    
    try:
        app.run_polling()
    except Exception as e:
        logger.error(f"❌ Критическая ошибка при запуске бота: {e}", exc_info=True)

if __name__ == "__main__":
    main()
