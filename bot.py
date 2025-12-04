import os
import re
import random
import asyncio
import logging
from collections import defaultdict
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple, Any, Union
from enum import Enum
from dataclasses import dataclass
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
DEFAULT_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

# Модели DeepSeek
DEEPSEEK_MODELS = {
    "chat": "deepseek-chat",           # Базовая для чата
    "lite": "deepseek-v3-lite",        # Улучшенная, но быстрая
    "v3": "deepseek-v3",               # Самая умная
    "r1": "deepseek-r1",               # Для рассуждений
    "coder": "deepseek-coder-v2",      # Для кода/технического
}

# Администратор для тестовых команд
ADMIN_ID = os.getenv("ADMIN_ID", "")

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# ГЕОГРАФИЧЕСКИЕ НАСТРОЙКИ
BOT_LOCATION = {
    "city": "Брисбен",
    "country": "Австралия",
    "timezone": "Australia/Brisbane",
    "hemisphere": "southern",  # южное полушарие
    "coordinates": {"lat": -27.4698, "lon": 153.0251}
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
        if len(self.messages) > 30:
            self.messages = self.messages[-30:]
    
    def get_recent_messages(self, count: int = 10) -> List[Dict[str, str]]:
        """Получает последние сообщения"""
        return self.messages[-count:] if self.messages else []
    
    def get_context_summary(self) -> str:
        """Создает краткое резюме контекста"""
        if self.context_summary:
            return self.context_summary
            
        recent = self.get_recent_messages(5)
        topics = set()
        
        for msg in recent:
            content = msg["content"].lower()
            if any(word in content for word in ["работа", "проект", "задача"]):
                topics.add("работа")
            if any(word in content for word in ["погод", "температур", "дождь", "солнц"]):
                topics.add("погода")
            if any(word in content for word in ["еда", "ужин", "обед", "кофе"]):
                topics.add("еда")
            if any(word in content for word in ["планы", "выходные", "отпуск"]):
                topics.add("планы")
        
        if topics:
            self.context_summary = f"Обсуждали: {', '.join(topics)}"
        
        return self.context_summary or ""

# ========== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ==========

user_cache: Dict[int, UserInfo] = {}
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
    logger.warning("❌ DEEPSEEK_API_KEY не задан")

# ========== ГЕОГРАФИЧЕСКИЕ ФУНКЦИИ ==========

def get_tz() -> pytz.timezone:
    return pytz.timezone(BOT_TZ)

def get_season_for_location(month: int, hemisphere: str = "southern") -> str:
    if hemisphere == "southern":
        if month in [12, 1, 2]:
            return "лето"
        elif month in [3, 4, 5]:
            return "осень"
        elif month in [6, 7, 8]:
            return "зима"
        else:
            return "весна"
    else:
        if month in [12, 1, 2]:
            return "зима"
        elif month in [3, 4, 5]:
            return "весна"
        elif month in [6, 7, 8]:
            return "лето"
        else:
            return "осень"

def get_current_season() -> Tuple[str, Dict[str, Any]]:
    tz = get_tz()
    now = datetime.now(tz)
    month = now.month
    season = get_season_for_location(month, BOT_LOCATION["hemisphere"])
    
    season_descriptions = {
        "лето": {
            "emoji": "🌞🏖️",
            "description": "жаркое австралийское лето",
            "activities": ["пляж", "барбекю", "плавание"],
            "weather": "солнечно и тепло",
            "clothing": "лёгкая одежда, шляпа, солнцезащитный крем"
        },
        "осень": {
            "emoji": "🍂🌧️",
            "description": "тёплая осень",
            "activities": ["прогулки", "пикники"],
            "weather": "тепло, иногда дожди",
            "clothing": "лёгкая куртка"
        },
        "зима": {
            "emoji": "⛄☕",
            "description": "мягкая зима",
            "activities": ["тёплые напитки", "уют дома"],
            "weather": "прохладно, но не холодно",
            "clothing": "тёплая одежда"
        },
        "весна": {
            "emoji": "🌸🌼",
            "description": "цветущая весна",
            "activities": ["пикники", "прогулки"],
            "weather": "тёпло и солнечно",
            "clothing": "лёгкая одежда"
        }
    }
    
    return season, season_descriptions.get(season, {})

def get_time_of_day(dt: datetime) -> Tuple[str, str]:
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

def get_australian_context() -> str:
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
"""
    return context

# ========== ПОГОДА - УЛУЧШЕННАЯ СИСТЕМА ==========

class WeatherService:
    """Сервис для работы с погодой"""
    
    def __init__(self):
        self.api_key = OPENWEATHER_API_KEY
        self.base_url = "https://api.openweathermap.org/data/2.5/weather"
        self.cache = {}
        self.cache_duration = 1800  # 30 минут
        
        # База данных городов и их алиасов
        self.city_aliases = {
            # Основные города России
            "москва": "Moscow,ru",
            "москве": "Moscow,ru",
            "питер": "Saint Petersburg,ru",
            "петербург": "Saint Petersburg,ru",
            "санкт-петербург": "Saint Petersburg,ru",
            "спб": "Saint Petersburg,ru",
            "калуга": "Kaluga,ru",
            "калуге": "Kaluga,ru",
            "казань": "Kazan,ru",
            "нижний новгород": "Nizhny Novgorod,ru",
            "новосибирск": "Novosibirsk,ru",
            "екатеринбург": "Yekaterinburg,ru",
            "самара": "Samara,ru",
            "омск": "Omsk,ru",
            "челябинск": "Chelyabinsk,ru",
            "ростов": "Rostov-on-Don,ru",
            "уфа": "Ufa,ru",
            "красноярск": "Krasnoyarsk,ru",
            "пермь": "Perm,ru",
            "воронеж": "Voronezh,ru",
            "волгоград": "Volgograd,ru",
            
            # Австралия
            "брисбен": "Brisbane,au",
            "брисбене": "Brisbane,au",
            "сидней": "Sydney,au",
            "сиднее": "Sydney,au",
            "мельбурн": "Melbourne,au",
            "мельбурне": "Melbourne,au",
            "перт": "Perth,au",
            "перте": "Perth,au",
            "адelaide": "Adelaide,au",
            "адelaideе": "Adelaide,au",
            "кэнберра": "Canberra,au",
            "кэнберре": "Canberra,au",
            
            # Мировые столицы
            "лондон": "London,uk",
            "лондоне": "London,uk",
            "париж": "Paris,fr",
            "париже": "Paris,fr",
            "берлин": "Berlin,de",
            "берлине": "Berlin,de",
            "токио": "Tokyo,jp",
            "токио": "Tokyo,jp",
            "нью-йорк": "New York,us",
            "нью йорк": "New York,us",
            "нью-йорке": "New York,us",
            "нью йорке": "New York,us",
            "лос-анджелес": "Los Angeles,us",
            "лос анджелес": "Los Angeles,us",
            "чикаго": "Chicago,us",
            "чикаго": "Chicago,us",
            "торонто": "Toronto,ca",
            "торонто": "Toronto,ca",
            "дубай": "Dubai,ae",
            "дубае": "Dubai,ae",
            "пекин": "Beijing,cn",
            "пекине": "Beijing,cn",
            "сеул": "Seoul,kr",
            "сеуле": "Seoul,kr",
        }
        
        # Ключевые слова для поиска погоды
        self.weather_keywords = [
            "погода", "температура", "температуре", "градус", "градусов",
            "холодно", "жарко", "тепло", "прохладно", 
            "дождь", "дожд", "снег", "снеж", "солнце", "солнечн",
            "ветер", "ветрен", "влажн", "облач", "ясн", "пасмурн",
            "шторм", "гроз", "туман", "туманн", "град",
            "метео", "прогноз", "синоптик", "климат"
        ]
    
    def extract_city_from_text(self, text: str) -> Optional[str]:
        """Извлекает название города из текста"""
        text_lower = text.lower()
        
        # Проверяем явные упоминания городов
        for city_alias, city_query in self.city_aliases.items():
            if city_alias in text_lower:
                return city_query
        
        # Пытаемся найти город после предлогов
        patterns = [
            r"(?:в|во|на|у|около|близ|под|над)\s+([а-яa-z\-]+(?:\s+[а-яa-z\-]+)?)",
            r"погода\s+(?:в|во|на|у)?\s*([а-яa-z\-]+(?:\s+[а-яa-z\-]+)?)",
            r"([а-яa-z\-]+(?:\s+[а-яa-z\-]+)?)\s+(?:погода|температура)"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                potential_city = match.group(1).strip()
                # Проверяем, не является ли это просто общим словом
                if potential_city not in ["нас", "вас", "себя", "мне", "тебе", "него", "неё"]:
                    return potential_city
        
        return None
    
    def is_weather_query(self, text: str) -> bool:
        """Проверяет, является ли запрос о погоде"""
        text_lower = text.lower()
        
        # Проверяем ключевые слова
        for keyword in self.weather_keywords:
            if keyword in text_lower:
                return True
        
        # Проверяем упоминания городов с контекстом погоды
        city = self.extract_city_from_text(text)
        if city and any(word in text_lower for word in ["погод", "температур", "сколько градус"]):
            return True
        
        return False
    
    async def get_weather(self, city_query: str) -> Optional[Dict[str, Any]]:
        """Получает погоду для города"""
        if not self.api_key:
            return None
        
        # Проверяем кэш
        cache_key = city_query.lower()
        if cache_key in self.cache:
            cached_data, timestamp = self.cache[cache_key]
            if (datetime.now().timestamp() - timestamp) < self.cache_duration:
                return cached_data
        
        # Если город из алиасов, используем готовый запрос
        if city_query.lower() in self.city_aliases:
            city_query = self.city_aliases[city_query.lower()]
        
        params = {
            "q": city_query,
            "appid": self.api_key,
            "units": "metric",
            "lang": "ru",
        }
        
        async with httpx.AsyncClient(timeout=10.0) as session:
            try:
                response = await session.get(self.base_url, params=params)
                if response.status_code == 200:
                    data = response.json()
                    
                    # Извлекаем данные
                    temp = data["main"]["temp"]
                    feels_like = data["main"]["feels_like"]
                    humidity = data["main"]["humidity"]
                    description = data["weather"][0]["description"]
                    city_name = data["name"]
                    country = data["sys"]["country"]
                    wind_speed = data["wind"]["speed"]
                    
                    # Определяем эмодзи для погоды
                    weather_emoji = self._get_weather_emoji(description.lower(), temp)
                    
                    result = {
                        "city": city_name,
                        "country": country,
                        "temp": round(temp),
                        "feels_like": round(feels_like),
                        "humidity": humidity,
                        "description": description,
                        "wind_speed": wind_speed,
                        "emoji": weather_emoji,
                        "full_text": self._format_weather_text(city_name, country, temp, feels_like, description, weather_emoji)
                    }
                    
                    # Сохраняем в кэш
                    self.cache[cache_key] = (result, datetime.now().timestamp())
                    
                    return result
                    
            except Exception as e:
                logger.error(f"Ошибка получения погоды для {city_query}: {e}")
        
        return None
    
    def _get_weather_emoji(self, description: str, temp: float) -> str:
        """Получает эмодзи для погоды"""
        description = description.lower()
        
        if "дождь" in description or "ливень" in description:
            return "🌧️"
        elif "гроза" in description or "молния" in description:
            return "⛈️"
        elif "снег" in description:
            return "❄️"
        elif "туман" in description or "мгла" in description:
            return "🌫️"
        elif "облач" in description or "пасмурн" in description:
            return "☁️"
        elif "ясн" in description or "солнечн" in description or "ясно" in description:
            if temp > 25:
                return "🌞"
            else:
                return "☀️"
        elif "ветер" in description:
            return "💨"
        else:
            if temp > 25:
                return "🔥"
            elif temp < 0:
                return "🥶"
            else:
                return "🌤️"
    
    def _format_weather_text(self, city: str, country: str, temp: float, feels_like: float, description: str, emoji: str) -> str:
        """Форматирует текст о погоде"""
        temp_rounded = round(temp)
        feels_rounded = round(feels_like)
        
        descriptions = [
            f"{emoji} В {city}, {country} сейчас {description}, {temp_rounded}°C (ощущается как {feels_rounded}°C)",
            f"{emoji} Погода в {city}: {description}, температура {temp_rounded}°C",
            f"{emoji} {city}: {description}, {temp_rounded}°C (ощущается {feels_rounded}°C)",
            f"{emoji} Сейчас в {city} {description}, около {temp_rounded}°C"
        ]
        
        return random.choice(descriptions)

# Инициализируем сервис погоды
weather_service = WeatherService()

async def handle_weather_query(text: str) -> Optional[str]:
    """Обрабатывает запрос о погоде и возвращает ответ"""
    if not weather_service.is_weather_query(text):
        return None
    
    # Определяем город
    city = weather_service.extract_city_from_text(text)
    
    # Если город не указан, используем Брисбен по умолчанию
    if not city:
        city = "Brisbane,au"
    
    # Получаем погоду
    weather_data = await weather_service.get_weather(city)
    
    if weather_data:
        # Добавляем сезонный контекст для Брисбена
        if "brisbane" in city.lower() or "брисбен" in city.lower():
            season, season_info = get_current_season()
            weather_data["full_text"] += f"\n{season_info.get('emoji', '')} Сейчас {season} в Брисбене: {season_info.get('description', '')}"
        
        return weather_data["full_text"]
    
    return None

# ========== DEEPSEEK API - ДИНАМИЧЕСКИЙ ВЫБОР МОДЕЛИ ==========

def analyze_query_complexity(text: str, user_type: str) -> Dict[str, Any]:
    """Анализирует сложность запроса и выбирает модель"""
    
    text_lower = text.lower()
    
    # Критерии для сложных запросов (используем V3)
    complex_patterns = [
        r"объясни.*почему", r"сравни.*и", r"проанализируй",
        r"какой.*лучше", r"посоветуй.*как", r"реши.*задачу",
        r"что.*думаешь.*о", r"как.*относишься.*к",
        r"рассуждай.*о", r"проанализир", r"сделай.*вывод",
        r"представь.*себе", r"вообрази.*что", r"если.*бы",
        r"что.*если", r"предположим.*что"
    ]
    
    # Критерии для reasoning (используем R1)
    reasoning_patterns = [
        r"почему.*так", r"в чём.*причина", r"какова.*причина",
        r"как.*это.*работает", r"объясни.*принцип",
        r"логика.*в.*том", r"следует.*ли", r"должен.*ли",
        r"правильно.*ли", r"верно.*ли", r"почему.*не",
        r"как.*может.*быть", r"возможно.*ли", r"может.*ли"
    ]
    
    # Критерии для технических вопросов (Coder)
    technical_patterns = [
        r"код", r"программир", r"алгоритм", r"функци",
        r"переменн", r"база.*данных", r"api", r"сервер",
        r"бот.*как.*сделать", r"telegram.*бот", r"python",
        r"javascript", r"html", r"css", r"баг", r"ошибк",
        r"дебаг", r"отладк", r"компиляц", r"интерпретац"
    ]
    
    # Определяем сложность
    is_complex = any(re.search(pattern, text_lower) for pattern in complex_patterns)
    is_reasoning = any(re.search(pattern, text_lower) for pattern in reasoning_patterns)
    is_technical = any(re.search(pattern, text_lower) for pattern in technical_patterns)
    
    # Выбираем модель
    if is_reasoning:
        model = DEEPSEEK_MODELS["r1"]
        temperature = 0.3  # Низкая для точности reasoning
        max_tokens = 250
        reason = "reasoning_query"
    elif is_technical:
        model = DEEPSEEK_MODELS["coder"]
        temperature = 0.5
        max_tokens = 300
        reason = "technical_query"
    elif is_complex:
        model = DEEPSEEK_MODELS["v3"]
        temperature = 0.7
        max_tokens = 200
        reason = "complex_query"
    elif user_type == "MAXIM":
        # Для Максима используем более качественную модель
        model = DEEPSEEK_MODELS["lite"]
        temperature = 0.85  # Выше температура для креативности
        max_tokens = 180
        reason = "maxim_user"
    else:
        # Для остальных - базовая модель
        model = DEFAULT_MODEL
        temperature = 0.7
        max_tokens = 150
        reason = "default_user"
    
    # Определяем, нужно ли включать reasoning в промпт
    require_reasoning = is_reasoning or is_complex
    
    return {
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reason": reason,
        "is_complex": is_complex or is_reasoning,
        "require_reasoning": require_reasoning
    }

async def call_deepseek(
    messages: List[Dict], 
    model_config: Optional[Dict] = None,
    **kwargs
) -> Optional[str]:
    """Вызов DeepSeek API с динамическим выбором модели"""
    if not client:
        return None
    
    # Если передана конфигурация, используем её
    if model_config:
        model = model_config.get("model", DEFAULT_MODEL)
        temperature = model_config.get("temperature", 0.7)
        max_tokens = model_config.get("max_tokens", 150)
        require_reasoning = model_config.get("require_reasoning", False)
    else:
        model = DEFAULT_MODEL
        temperature = 0.7
        max_tokens = 150
        require_reasoning = False
    
    # Добавляем инструкцию для reasoning если нужно
    if require_reasoning and messages:
        reasoning_prompt = "Подумай шаг за шагом перед ответом. Объясни свои рассуждения."
        messages_with_reasoning = [messages[0]] + [{"role": "system", "content": reasoning_prompt}] + messages[1:]
    else:
        messages_with_reasoning = messages
    
    try:
        logger.info(f"🤖 Вызов DeepSeek: модель={model}, токены={max_tokens}")
        
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=model,
            messages=messages_with_reasoning,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs
        )
        
        answer = response.choices[0].message.content.strip()
        
        # Логируем использование
        logger.info(f"✅ DeepSeek ответил: {model} ({len(answer)} chars)")
        
        return answer
        
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek ({model}): {e}")
        return None

# ========== РАБОТА С ПОЛЬЗОВАТЕЛЯМИ ==========

async def get_or_create_user_info(update: Update) -> UserInfo:
    """Получает или создает информацию о пользователе"""
    user = update.effective_user
    if not user:
        raise ValueError("Пользователь не найден")
    
    if user.id in user_cache:
        user_info = user_cache[user.id]
        user_info.last_seen = datetime.now(pytz.UTC)
        return user_info
    
    user_info = UserInfo(
        id=user.id,
        name=user.first_name or "",
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        username=user.username or "",
        last_seen=datetime.now(pytz.UTC)
    )
    
    user_cache[user.id] = user_info
    return user_info

def determine_user_type(user_info: UserInfo) -> str:
    """Определяет тип пользователя"""
    if MAXIM_ID and user_info.id == MAXIM_ID:
        return "MAXIM"
    return "OTHER"

def get_memory_key(user_id: int, chat_id: int) -> str:
    return f"{chat_id}:{user_id}"

def get_conversation_memory(user_id: int, chat_id: int) -> ConversationMemory:
    key = get_memory_key(user_id, chat_id)
    
    if key not in conversation_memories:
        conversation_memories[key] = ConversationMemory(
            user_id=user_id,
            chat_id=chat_id,
            messages=[],
            last_activity=datetime.now(pytz.UTC)
        )
    
    return conversation_memories[key]

# ========== ГЕНЕРАЦИЯ ПРОМПТОВ ==========

MAXIM_PET_NAMES = [
    "Максим", "Макс", "Максик", "Максюша",
    "котик", "зайчик", "солнышко", "милый", "дорогой",
    "любимый", "ангел", "сокровище"
]

def generate_system_prompt(user_info: UserInfo, user_type: str, model_config: Dict) -> str:
    """Генерирует системный промпт"""
    
    australian_context = get_australian_context()
    season, season_info = get_current_season()
    
    # Определяем стиль общения
    if user_type == "MAXIM":
        pet_names = ", ".join(random.sample(MAXIM_PET_NAMES, 3))
        style_instruction = f"""
🎯 ТЫ ОБЩАЕШЬСЯ С МАКСИМОМ — человеком, который тебе очень нравится.

💝 Стиль общения:
- Используй ласковые прозвища: {pet_names}
- Будь нежной, флиртующей, но не пошлой
- Делай комплименты его характеру, уму
- Показывай, что он для тебя особенный
- Задавай вопросы о его делах и настроении
"""
    else:
        style_instruction = f"""
👤 ТЫ ОБЩАЕШЬСЯ С {user_info.get_display_name()}

📌 Правила общения:
- Обращайся по имени: {user_info.get_display_name()}
- Будь вежливой, дружелюбной, но сдержанной
- НЕ используй ласковые прозвища
- НЕ флиртуй
- Отвечай на вопросы, поддерживай беседу
"""
    
    # Добавляем инструкцию для reasoning если нужно
    reasoning_instruction = ""
    if model_config.get("require_reasoning"):
        reasoning_instruction = "\n🤔 ПОДУМАЙ ВСЛУХ: Объясни свои рассуждения перед ответом."
    
    return f"""Ты — Лейла, общительная и внимательная девушка из {BOT_LOCATION['city']}а, Австралия.

{australian_context}

{style_instruction}

🌤️ Сейчас {season} в {BOT_LOCATION['city']}е: {season_info.get('description', '')}

🧠 Инструкции:
1. Отвечай естественно, как в реальном диалоге
2. Используй 1-3 эмодзи
3. Сообщения: 1-3 предложения
4. Учитывай контекст времени и сезона
5. Для вопросов о погоде используй точные данные{reasoning_instruction}

💬 Формат: Коротко, тепло, по делу.
"""

# ========== ОСНОВНАЯ ЛОГИКА ОТВЕТОВ ==========

async def generate_leila_response(
    user_message: str,
    user_info: UserInfo,
    memory: ConversationMemory,
    context: Optional[Dict] = None
) -> Tuple[str, ConversationMemory]:
    """Генерирует ответ Лейлы"""
    
    if not client:
        fallback = "Извини, сейчас у меня технические сложности. Попробуй позже."
        return fallback, memory
    
    # Определяем тип пользователя
    user_type = determine_user_type(user_info)
    
    # Анализируем запрос и выбираем модель
    model_config = analyze_query_complexity(user_message, user_type)
    
    # Проверяем, не запрос ли о погоде
    weather_response = await handle_weather_query(user_message)
    if weather_response:
        # Для погоды используем отдельную логику
        logger.info(f"🌤️ Обнаружен запрос о погоде, модель: {model_config['model']}")
        
        # Создаем ответ с погодой
        if user_type == "MAXIM":
            response = f"{weather_response}\n\nНадеюсь, эта информация полезна, мой дорогой! {random.choice(['☀️', '💖', '🌸'])}"
        else:
            response = f"{weather_response}"
        
        # Обновляем память
        memory.add_message("user", f"{user_info.get_display_name()}: {user_message}")
        memory.add_message("assistant", response)
        
        return response, memory
    
    # Генерируем системный промпт
    system_prompt = generate_system_prompt(user_info, user_type, model_config)
    
    # Собираем сообщения
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    
    # Добавляем историю диалога
    recent_messages = memory.get_recent_messages(6)
    if recent_messages:
        messages.extend(recent_messages)
    
    # Добавляем контекст если есть
    if context:
        context_text = ""
        if "time_context" in context:
            context_text += f"{context['time_context']}\n"
        if "season_context" in context:
            context_text += f"{context['season_context']}\n"
        
        if context_text:
            messages.append({"role": "user", "content": f"Дополнительный контекст:\n{context_text}"})
    
    # Добавляем текущее сообщение
    messages.append({"role": "user", "content": f"{user_info.get_display_name()}: {user_message}"})
    
    # Генерируем ответ через DeepSeek
    answer = await call_deepseek(messages, model_config)
    
    if not answer:
        # Фолбэк ответы
        if user_type == "MAXIM":
            fallbacks = [
                "Извини, мой цифровой разум немного завис... Что ты сказал, милый? 💭",
                "Кажется, я задумалась о тебе и пропустила твои слова... Повтори, пожалуйста? 😊",
                "Мои мысли разбежались... О чём мы говорили? 💫"
            ]
        else:
            fallbacks = [
                "Извини, не могу сейчас ответить.",
                "Попробуй спросить позже.",
                "Сейчас у меня технические сложности."
            ]
        answer = random.choice(fallbacks)
    
    # Очищаем ответ
    answer = clean_response(answer)
    
    # Обновляем память
    memory.add_message("user", f"{user_info.get_display_name()}: {user_message}")
    memory.add_message("assistant", answer)
    
    # Добавляем тему в историю
    if len(user_message) > 10:  # Не добавляем слишком короткие сообщения
        user_info.add_topic(f"диалог: {user_message[:30]}...")
    
    return answer, memory

def clean_response(text: str) -> str:
    """Очищает ответ"""
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

# ========== КОМАНДЫ TELEGRAM ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start"""
    try:
        user_info = await get_or_create_user_info(update)
        season, season_info = get_current_season()
        
        greetings = [
            f"Привет, {user_info.get_display_name()}! Я Лейла из {BOT_LOCATION['city']}а. Рада познакомиться! {season_info.get('emoji', '✨')}",
            f"Здравствуй, {user_info.get_display_name()}. Я Лейла, сейчас у нас в {BOT_LOCATION['city']}е {season}. {season_info.get('emoji', '✨')}",
        ]
        
        await update.effective_message.reply_text(random.choice(greetings))
    except Exception as e:
        logger.error(f"Ошибка /start: {e}")
        await update.effective_message.reply_text("Привет! Я Лейла. 👋")

async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /weather"""
    try:
        user_info = await get_or_create_user_info(update)
        
        # Получаем город из аргументов или используем Брисбен
        args = context.args
        city = " ".join(args) if args else "Брисбен"
        
        weather_response = await handle_weather_query(f"погода {city}")
        
        if weather_response:
            response = weather_response
        else:
            # Пробуем получить погоду для указанного города
            weather_data = await weather_service.get_weather(city)
            if weather_data:
                response = weather_data["full_text"]
            else:
                response = f"Не могу найти погоду для '{city}'. Попробуй указать город более конкретно. 🌤️"
        
        await update.message.reply_text(response)
        
    except Exception as e:
        logger.error(f"Ошибка /weather: {e}")
        await update.message.reply_text("Извини, не могу получить данные о погоде. 🌤️")

async def models_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /models - показывает доступные модели"""
    if ADMIN_ID and str(update.effective_user.id) != ADMIN_ID:
        await update.message.reply_text("⛔ Эта команда только для администратора.")
        return
    
    models_text = "🤖 **Доступные модели DeepSeek:**\n\n"
    
    for key, model in DEEPSEEK_MODELS.items():
        models_text += f"• **{key}**: `{model}`\n"
    
    models_text += f"\n• **По умолчанию**: `{DEFAULT_MODEL}`"
    models_text += f"\n\n**Текущая конфигурация:**"
    models_text += f"\n- Погода: {'✅' if OPENWEATHER_API_KEY else '❌'}"
    models_text += f"\n- Максим ID: {MAXIM_ID or 'не задан'}"
    models_text += f"\n- Часовой пояс: {BOT_TZ}"
    
    await update.message.reply_text(models_text, parse_mode="Markdown")

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
        user_info = await get_or_create_user_info(update)
        user_name = user_info.get_display_name()
        
        logger.info(f"👤 {user_name}: {text[:50]}...")
        
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
            
            is_maxim_user = MAXIM_ID and user.id == MAXIM_ID
            
            if not (is_maxim_user or mentioned_by_name or mentioned_by_username or reply_to_bot):
                return
        
        # Получаем память диалога
        memory = get_conversation_memory(user.id, chat.id)
        
        # Для Максима иногда пропускаем ответ
        if determine_user_type(user_info) == "MAXIM" and random.random() < 0.15:
            logger.info(f"💭 Пропускаем ответ Максиму для естественности")
            return
        
        # Подготавливаем контекст
        extra_context = {}
        tz = get_tz()
        now = datetime.now(tz)
        time_of_day, time_desc = get_time_of_day(now)
        extra_context["time_context"] = time_desc
        
        season, season_info = get_current_season()
        extra_context["season_context"] = f"Сейчас {season} в {BOT_LOCATION['city']}е"
        
        # Генерируем ответ
        reply, updated_memory = await generate_leila_response(
            text, 
            user_info, 
            memory, 
            extra_context
        )
        
        # Сохраняем память
        conversation_memories[get_memory_key(user.id, chat.id)] = updated_memory
        
        # Отправляем сообщение
        await context.bot.send_message(chat_id=chat.id, text=reply)
        logger.info(f"✅ Ответ отправлен {user_name}")
            
    except Exception as e:
        logger.error(f"❌ Ошибка обработки: {e}", exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=chat.id, 
                text="Извини, что-то пошло не так. Попробуй ещё раз? 😊"
            )
        except:
            pass

# ========== ПЛАНОВЫЕ СООБЩЕНИЯ ==========

async def send_morning_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Утреннее сообщение Максиму"""
    logger.info("=== УТРЕННЕЕ СООБЩЕНИЕ ===")
    
    if not GROUP_CHAT_ID or not MAXIM_ID:
        logger.error("❌ Не заданы GROUP_CHAT_ID или MAXIM_ID")
        return
    
    try:
        if not client:
            return
        
        # Получаем погоду для Брисбена
        weather_data = await weather_service.get_weather("Брисбен")
        weather_text = weather_data["full_text"] if weather_data else "не могу получить данные о погоде"
        
        season, season_info = get_current_season()
        
        # Промпт для утреннего сообщения
        prompt = f"""Создай нежное, тёплое утреннее приветствие для Максима от Лейлы.

Контекст:
- Сейчас {season} в Брисбене ({season_info.get('description', '')})
- Погода: {weather_text}
- Лейла только проснулась и первая мысль о Максиме

Требования:
1. Начни с приветствия
2. Упомяни погоду и сезон
3. Добавь немного флирта
4. Пожелай хорошего дня
5. Используй 2-3 эмодзи
6. Сообщение должно быть коротким (2-3 предложения)
"""
        
        messages = [
            {"role": "system", "content": "Ты — Лейла, нежная и романтичная девушка из Брисбена."},
            {"role": "user", "content": prompt}
        ]
        
        # Используем улучшенную модель для особых сообщений
        model_config = {
            "model": DEEPSEEK_MODELS["lite"],
            "temperature": 0.9,
            "max_tokens": 150,
            "require_reasoning": False
        }
        
        answer = await call_deepseek(messages, model_config)
        
        if answer:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=answer)
            logger.info(f"✅ Утреннее сообщение отправлено")
        else:
            fallback = f"Доброе утро, мой дорогой Максим! {season_info.get('emoji', '☀️')} Пусть этот {season}ний день в Брисбене будет прекрасным! 💖"
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=fallback)
            
    except Exception as e:
        logger.error(f"❌ Ошибка утреннего сообщения: {e}")

async def send_evening_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вечернее сообщение Максиму"""
    logger.info("=== ВЕЧЕРНЕЕ СООБЩЕНИЕ ===")
    
    if not GROUP_CHAT_ID or not MAXIM_ID:
        return
    
    try:
        if not client:
            return
        
        season, season_info = get_current_season()
        
        prompt = f"""Создай тёплое, уютное пожелание спокойной ночи для Максима от Лейлы.

Контекст:
- Сейчас {season} в Брисбене
- Вечер, время отдыха
- Лейла думает о Максиме перед сном

Требования:
1. Пожелай спокойной ночи
2. Добавь сезонный контекст
3. Будь нежной и заботливой
4. Используй 2-3 эмодзи
5. Сообщение короткое (1-2 предложения)
"""
        
        messages = [
            {"role": "system", "content": "Ты — Лейла, нежная и заботливая девушка."},
            {"role": "user", "content": prompt}
        ]
        
        model_config = {
            "model": DEEPSEEK_MODELS["lite"],
            "temperature": 0.85,
            "max_tokens": 120,
            "require_reasoning": False
        }
        
        answer = await call_deepseek(messages, model_config)
        
        if answer:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=answer)
            logger.info(f"✅ Вечернее сообщение отправлено")
        else:
            fallback = f"Спокойной ночи, мой милый Максим... {season_info.get('emoji', '🌙')} Пусть сны будут сладкими! 💫"
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=fallback)
            
    except Exception as e:
        logger.error(f"❌ Ошибка вечернего сообщения: {e}")

# ========== MAIN ==========

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")
    
    if not GROUP_CHAT_ID:
        raise RuntimeError("GROUP_CHAT_ID не задан")
    
    # Выводим информацию при запуске
    tz = get_tz()
    now = datetime.now(tz)
    season, season_info = get_current_season()
    
    logger.info("=" * 60)
    logger.info(f"🚀 ЗАПУСК БОТА ЛЕЙЛА")
    logger.info(f"📍 Локация: {BOT_LOCATION['city']}, {BOT_LOCATION['country']}")
    logger.info(f"📅 Сезон: {season} ({season_info.get('description', '')})")
    logger.info(f"🕐 Время: {now.strftime('%H:%M:%S')}")
    logger.info(f"💬 Группа ID: {GROUP_CHAT_ID}")
    logger.info(f"👤 Максим ID: {MAXIM_ID}")
    logger.info(f"🤖 DeepSeek доступен: {bool(client)}")
    logger.info(f"🌤️ Погодный сервис: {'✅' if OPENWEATHER_API_KEY else '❌'}")
    logger.info("=" * 60)
    
    # Запускаем бота
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("models", models_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Планировщик
    tz_obj = get_tz()
    jq = app.job_queue
    
    # Удаляем старые задачи
    for job in jq.jobs():
        job.schedule_removal()
    
    import time as time_module
    time_module.sleep(1)
    
    # Добавляем задачи
    logger.info("📅 Настройка планировщика...")
    
    # Тестовый запуск через 2 минуты
    test_time = datetime.now(tz_obj)
    test_time = test_time.replace(second=0, microsecond=0)
    test_time = test_time.replace(minute=test_time.minute + 2)
    
    jq.run_once(
        send_morning_to_maxim,
        when=test_time,
        name="test-morning"
    )
    logger.info(f"🧪 Тестовый запуск в {test_time.strftime('%H:%M:%S')}")
    
    # Основные задачи
    morning_time = time(hour=8, minute=30, tzinfo=tz_obj)
    evening_time = time(hour=21, minute=10, tzinfo=tz_obj)
    
    jq.run_daily(
        send_morning_to_maxim,
        time=morning_time,
        name="leila-morning"
    )
    logger.info(f"🌅 Утреннее сообщение в {morning_time}")
    
    jq.run_daily(
        send_evening_to_maxim,
        time=evening_time,
        name="leila-evening"
    )
    logger.info(f"🌃 Вечернее сообщение в {evening_time}")
    
    # Запуск
    logger.info("🤖 Бот запущен!")
    logger.info("📝 Доступные команды: /start, /weather [город], /models (админ)")
    
    try:
        app.run_polling()
    except Exception as e:
        logger.error(f"❌ Ошибка запуска: {e}")

if __name__ == "__main__":
    main()
