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
import aiohttp

import pytz
import httpx
import wikipedia
from openai import OpenAI
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    ContextTypes,
    CommandHandler,
    filters,
)

# ========== ЛОГИРОВАНИЕ ===========

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
    "chat": "deepseek-chat",
    "v3": "deepseek-v3",
    "r1": "deepseek-r1",
    "coder": "deepseek-coder-v2",
}

# Администратор для тестовых команд
ADMIN_ID = os.getenv("ADMIN_ID", "")

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")

# Настройка Википедии
wikipedia.set_lang("ru")

# ГЕОГРАФИЧЕСКИЕ НАСТРОЙКИ
BOT_LOCATION = {
    "city": "Брисбен",
    "country": "Австралия",
    "timezone": "Australia/Brisbane",
    "hemisphere": "southern",
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

# Теннисный код и дата действия
TENNIS_ACCESS_CODE = "33836555#"
TENNIS_CODE_VALID_UNTIL = "12 апреля 2026"

# ========== ДАТАКЛАССЫ ==========

@dataclass
class UserInfo:
    """Информация о пользователе"""
    id: int
    first_name: str
    last_name: str = ""
    username: str = ""
    last_seen: datetime = None
    conversation_topics: List[str] = None
    gender: str = "unknown"
    
    def __post_init__(self):
        if self.last_seen is None:
            self.last_seen = datetime.now(pytz.UTC)
        if self.conversation_topics is None:
            self.conversation_topics = []
        self._determine_gender()
    
    def _determine_gender(self):
        """Определяет пол по имени"""
        if self.gender == "unknown":
            name_lower = self.first_name.lower()
            female_endings = ['а', 'я', 'ия', 'ина', 'ла', 'та']
            male_endings = ['й', 'ь', 'н', 'р', 'л', 'с', 'в', 'д', 'м']
            
            for ending in female_endings:
                if name_lower.endswith(ending):
                    self.gender = "female"
                    return
            
            for ending in male_endings:
                if name_lower.endswith(ending) and len(name_lower) > 2:
                    self.gender = "male"
                    return
    
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
    
    def is_maxim(self) -> bool:
        """Проверяет, является ли пользователь Максимом"""
        return self.id == MAXIM_ID

@dataclass
class ConversationMemory:
    """Память о диалоге"""
    user_id: int
    chat_id: int
    messages: List[Dict[str, str]]
    last_activity: datetime
    context_summary: str = ""
    summary_history: List[str] = None  # Добавим историю суммаризаций
    important_points: List[str] = None  # Добавим важные моменты
    
    def __post_init__(self):
        if self.summary_history is None:
            self.summary_history = []
        if self.important_points is None:
            self.important_points = []
    
    def add_message(self, role: str, content: str):
        """Добавляет сообщение в историю"""
        self.messages.append({"role": role, "content": content})
        self.last_activity = datetime.now(pytz.UTC)
        # УВЕЛИЧИМ с 30 до 50 сообщений
        if len(self.messages) > 50:
            # Сохраняем важные сообщения
            important_msgs = [msg for msg in self.messages[-20:] if self._is_important_message(msg)]
            removed_msgs = self.messages[:30]
            # Создаем сумму удаленных сообщений
            if len(removed_msgs) > 10:
                summary = self._create_summary_of_messages(removed_msgs)
                self.summary_history.append(summary)
                if len(self.summary_history) > 5:
                    self.summary_history = self.summary_history[-5:]
            
            self.messages = important_msgs + self.messages[30:]
    
    def _is_important_message(self, msg: Dict[str, str]) -> bool:
        """Определяет, важно ли сообщение для сохранения в памяти"""
        content = msg["content"].lower()
        important_keywords = [
            "имя", "зовут", "звать", "помни", "запомни", "важно",
            "никогда", "всегда", "люби", "нравится", "не нравится",
            "работа", "профессия", "семья", "друзья", "хобби",
            "аллергия", "боюсь", "страх", "мечта", "цель"
        ]
        
        # Сообщения от пользователя важнее
        if msg["role"] == "user":
            return any(keyword in content for keyword in important_keywords)
        
        # Сообщения от ассистента, где есть факты
        if msg["role"] == "assistant":
            fact_patterns = [
                r"тебе \d+", r"ты сказал.*что", r"ты упоминал",
                r"помню.*что", r"знаю.*что"
            ]
            return any(re.search(pattern, content) for pattern in fact_patterns)
        
        return False
    
    def _create_summary_of_messages(self, messages: List[Dict[str, str]]) -> str:
        """Создает краткое резюме сообщений"""
        user_messages = [msg["content"] for msg in messages if msg["role"] == "user"]
        assistant_messages = [msg["content"] for msg in messages if msg["role"] == "assistant"]
        
        topics = set()
        for msg in user_messages[:10]:  # Берем только первые 10 для суммаризации
            msg_lower = msg.lower()
            if any(word in msg_lower for word in ["погод", "температур"]):
                topics.add("погода")
            if any(word in msg_lower for word in ["работа", "проект", "задач"]):
                topics.add("работа")
            if any(word in msg_lower for word in ["еда", "кухн", "рецепт"]):
                topics.add("еда")
            if any(word in msg_lower for word in ["фильм", "книг", "музык"]):
                topics.add("развлечения")
            if any(word in msg_lower for word in ["планы", "выходные", "отпуск"]):
                topics.add("планы")
        
        if topics:
            return f"Обсуждали: {', '.join(list(topics)[:3])}"
        return "Разговор на общие темы"
    
    def get_recent_messages(self, count: int = 15) -> List[Dict[str, str]]:
        """Получает последние сообщения"""
        return self.messages[-count:] if self.messages else []
    
    def get_extended_context(self) -> str:
        """Получает расширенный контекст с историей"""
        if not self.summary_history:
            return self.get_context_summary()
        
        extended = []
        if self.summary_history:
            extended.append(f"Предыдущие темы: {'; '.join(self.summary_history[-3:])}")
        
        context_summary = self.get_context_summary()
        if context_summary:
            extended.append(context_summary)
        
        if self.important_points:
            extended.append(f"Важные детали: {'; '.join(self.important_points[-5:])}")
        
        return "\n".join(extended) if extended else ""
    
    def get_context_summary(self) -> str:
        """Создает краткое резюме контекста"""
        if self.context_summary:
            return self.context_summary
            
        recent = self.get_recent_messages(8)
        
        topics = set()
        user_details = []
        
        for msg in recent:
            content = msg["content"].lower()
            role = msg["role"]
            
            # Определяем темы
            if any(word in content for word in ["работа", "проект", "задача", "офис", "коллег"]):
                topics.add("работа/проекты")
            if any(word in content for word in ["погод", "температур", "дождь", "солнц", "холод", "жарк"]):
                topics.add("погода")
            if any(word in content for word in ["еда", "ужин", "обед", "кофе", "чай", "рецепт", "готов"]):
                topics.add("еда/кулинария")
            if any(word in content for word in ["планы", "выходные", "отпуск", "путешеств", "поездк"]):
                topics.add("планы/путешествия")
            if any(word in content for word in ["фильм", "сериал", "книг", "музык", "игр", "хобби"]):
                topics.add("развлечения/хобби")
            if any(word in content for word in ["семья", "друз", "подруг", "знаком", "отношен"]):
                topics.add("отношения")
            if any(word in content for word in ["здоровье", "болезн", "врач", "самочувств"]):
                topics.add("здоровье")
            
            # Выявляем важные детали о пользователе
            if role == "user":
                # Имя/обращение
                name_patterns = [
                    r"меня зовут (\w+)",
                    r"зовут (\w+)",
                    r"я (\w+)",
                    r"мое имя (\w+)"
                ]
                for pattern in name_patterns:
                    match = re.search(pattern, content)
                    if match and len(match.group(1)) > 2:
                        user_details.append(f"пользователя зовут {match.group(1)}")
                        break
                
                # Предпочтения
                if "люблю" in content or "нравится" in content:
                    pref_match = re.search(r"(люблю|нравится) (.+?)(?:\.|,|$)", content)
                    if pref_match:
                        user_details.append(f"нравится: {pref_match.group(2)}")
                
                # Не нравится
                if "не люблю" in content or "не нравится" in content or "ненавижу" in content:
                    dislike_match = re.search(r"(не люблю|не нравится|ненавижу) (.+?)(?:\.|,|$)", content)
                    if dislike_match:
                        user_details.append(f"не нравится: {dislike_match.group(2)}")
        
        # Сохраняем важные детали
        for detail in user_details:
            if detail not in self.important_points:
                self.important_points.append(detail)
                if len(self.important_points) > 10:
                    self.important_points = self.important_points[-10:]
        
        if topics:
            topics_list = list(topics)
            self.context_summary = f"Обсуждали: {', '.join(topics_list[:5])}"
            if user_details:
                self.context_summary += f"\nДетали: {'; '.join(user_details[:3])}"
        
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
        },
        "осень": {
            "emoji": "🍂🌧️",
            "description": "тёплая осень",
            "activities": ["прогулки", "пикники"],
            "weather": "тепло, иногда дожди",
        },
        "зима": {
            "emoji": "⛄☕",
            "description": "мягкая зима",
            "activities": ["тёплые напитки", "уют дома"],
            "weather": "прохладно, но не холодно",
        },
        "весна": {
            "emoji": "🌸🌼",
            "description": "цветущая весна",
            "activities": ["пикники", "прогулки"],
            "weather": "тёпло и солнечно",
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
    """Только для промптов - убрано упоминание погоды"""
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
"""
    return context

# ========== ПОГОДА - СЕРВИС ==========

class WeatherService:
    """Сервис для работы с погодой"""
    
    def __init__(self):
        self.api_key = OPENWEATHER_API_KEY
        self.base_url = "https://api.openweathermap.org/data/2.5/weather"
        self.cache = {}
        self.cache_duration = 1800
        
        self.city_aliases = {
            "москва": "Moscow,ru", "москве": "Moscow,ru",
            "питер": "Saint Petersburg,ru", "петербург": "Saint Petersburg,ru",
            "санкт-петербург": "Saint Petersburg,ru", "спб": "Saint Petersburg,ru",
            "калуга": "Kaluga,ru", "калуге": "Kaluga,ru",
            "казань": "Kazan,ru", "нижний новгород": "Nizhny Novgorod,ru",
            "новосибирск": "Novosibirsk,ru", "екатеринбург": "Yekaterinburg,ru",
            "самара": "Samara,ru", "омск": "Omsk,ru",
            "челябинск": "Chelyabinsk,ru", "ростов": "Rostov-on-Don,ru",
            "уфа": "Ufa,ru", "красноярск": "Krasnoyarsk,ru",
            "пермь": "Perm,ru", "воронеж": "Voronezh,ru",
            "волгоград": "Volgograd,ru", "брисбен": "Brisbane,au",
            "брисбене": "Brisbane,au", "сидней": "Sydney,au",
            "сиднее": "Sydney,au", "мельбурн": "Melbourne,au",
            "мельбурне": "Melbourne,au", "перт": "Perth,au",
            "адelaide": "Adelaide,au", "кэнберра": "Canberra,au",
            "лондон": "London,uk", "париж": "Paris,fr",
            "берлин": "Berlin,de", "токио": "Tokyo,jp",
            "нью-йорк": "New York,us", "нью йорк": "New York,us",
            "лос-анджелес": "Los Angeles,us", "торонто": "Toronto,ca",
            "дубай": "Dubai,ae", "пекин": "Beijing,cn",
            "сеул": "Seoul,kr",
        }
        
        self.weather_keywords = [
            "погода", "температура", "температуре", "градус", "градусов",
            "холодно", "жарко", "тепло", "прохладно", 
            "дождь", "дожд", "снег", "снеж", " солнце", "солнечн",
            "ветер", "ветрен", "облач", "ясн", "пасмурн",
            "шторм", "гроз", "туман", "град",
            "метео", "прогноз", "синоптик"
        ]
    
    def extract_city_from_text(self, text: str) -> Optional[str]:
        """Извлекает название города из текста"""
        text_lower = text.lower()
        
        for city_alias, city_query in self.city_aliases.items():
            if city_alias in text_lower:
                return city_query
        
        patterns = [
            r"(?:в|во|на|у|около)\s+([а-яa-z\-]+(?:\s+[а-яa-z\-]+)?)",
            r"погода\s+(?:в|во|на|у)?\s*([а-яa-z\-]+(?:\s+[а-яa-z\-]+)?)",
            r"([а-яa-z\-]+(?:\s+[а-яa-z\-]+)?)\s+(?:погода|температура)"
        ]
        
        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                potential_city = match.group(1).strip()
                if potential_city not in ["нас", "вас", "себя", "мне", "тебе", "него", "неё"]:
                    return potential_city
        
        return None
    
    def is_weather_query(self, text: str) -> bool:
        """Проверяет, является ли запрос о погоде"""
        text_lower = text.lower()
        
        for keyword in self.weather_keywords:
            if keyword in text_lower:
                return True
        
        city = self.extract_city_from_text(text)
        if city and any(word in text_lower for word in ["погод", "температур", "сколько градус"]):
            return True
        
        return False
    
    async def get_weather(self, city_query: str) -> Optional[Dict[str, Any]]:
        """Получает погоду для города"""
        if not self.api_key:
            return None
        
        cache_key = city_query.lower()
        if cache_key in self.cache:
            cached_data, timestamp = self.cache[cache_key]
            if (datetime.now().timestamp() - timestamp) < self.cache_duration:
                return cached_data
        
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
                    
                    temp = data["main"]["temp"]
                    feels_like = data["main"]["feels_like"]
                    humidity = data["main"]["humidity"]
                    description = data["weather"][0]["description"]
                    city_name = data["name"]
                    country = data["sys"]["country"]
                    wind_speed = data["wind"]["speed"]
                    
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
        elif "туман" in description:
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
        ]
        
        return random.choice(descriptions)

# Инициализируем сервис погоды
weather_service = WeatherService()

async def handle_weather_query(text: str) -> Optional[str]:
    """Обрабатывает запрос о погоде - ТОЛЬКО если явный запрос о погоде"""
    if not weather_service.is_weather_query(text):
        return None
    
    city = weather_service.extract_city_from_text(text)
    
    if not city:
        city = "Brisbane,au"
    
    weather_data = await weather_service.get_weather(city)
    
    if weather_data:
        return weather_data["full_text"]
    
    return None

# ========== ВИКИПЕДИЯ - СЕРВИС ==========

class WikipediaService:
    """Сервис для работы с Wikipedia (только по команде /wiki)"""
    
    def __init__(self):
        self.summary_cache = {}
        self.search_cache = {}
    
    async def search_wikipedia(self, query: str, sentences: int = 3) -> Optional[Tuple[str, str, str]]:
        """Ищет информацию в Википедии"""
        if not query:
            return None
        
        cache_key = f"{query}_{sentences}"
        if cache_key in self.summary_cache:
            return self.summary_cache[cache_key]
        
        try:
            try:
                page = wikipedia.page(query, auto_suggest=False)
                summary = wikipedia.summary(query, sentences=sentences, auto_suggest=False)
                url = page.url
                title = page.title
                
                result = (summary, title, url)
                self.summary_cache[cache_key] = result
                return result
                
            except wikipedia.DisambiguationError as e:
                options = e.options[:3]
                if options:
                    try:
                        page = wikipedia.page(options[0], auto_suggest=False)
                        summary = wikipedia.summary(options[0], sentences=sentences, auto_suggest=False)
                        url = page.url
                        title = page.title
                        
                        result = (summary, title, url)
                        self.summary_cache[cache_key] = result
                        return result
                    except:
                        pass
            
            except wikipedia.PageError:
                pass
            
            search_results = wikipedia.search(query, results=3)
            if search_results:
                try:
                    page = wikipedia.page(search_results[0], auto_suggest=False)
                    summary = wikipedia.summary(search_results[0], sentences=sentences, auto_suggest=False)
                    url = page.url
                    title = page.title
                    
                    result = (summary, title, url)
                    self.summary_cache[cache_key] = result
                    return result
                except:
                    pass
            
        except Exception as e:
            logger.error(f"Ошибка поиска в Википедии для '{query}': {e}")
        
        return None

# Инициализируем сервис Википедии
wiki_service = WikipediaService()

# ========== DEEPSEEK API ==========

def analyze_query_complexity(text: str, is_maxim: bool) -> Dict[str, Any]:
    """Анализирует сложность запроса и выбирает модель"""
    
    text_lower = text.lower()
    
    complex_patterns = [
        r"объясни.*почему", r"сравни.*и", r"проанализируй",
        r"какой.*лучше", r"посоветуй.*как", r"реши.*задачу",
        r"что.*думаешь.*о", r"как.*относишься.*к",
    ]
    
    reasoning_patterns = [
        r"почему.*так", r"в чём.*причина", r"какова.*причина",
        r"как.*это.*работает", r"объясни.*принцип",
        r"логика.*в.*том", r"следует.*ли", r"должен.*ли",
    ]
    
    technical_patterns = [
        r"код", r"программир", r"алгоритм", r"функци",
        r"переменн", r"база.*данных", r"api", r"сервер",
        r"бот.*как.*сделать", r"telegram.*бот", r"python",
    ]
    
    # Обнаруживаем простые/банальные вопросы для саркастичных ответов
    simple_patterns = [
        r"как.*дела", r"что.*делаеш", r"чем.*занимаеш",
        r"как.*жизн", r"расскажи.*о.*себе", r"что.*нового",
        r"привет$", r"хай$", r"здравствуй$", r"ку$",
    ]
    
    is_complex = any(re.search(pattern, text_lower) for pattern in complex_patterns)
    is_reasoning = any(re.search(pattern, text_lower) for pattern in reasoning_patterns)
    is_technical = any(re.search(pattern, text_lower) for pattern in technical_patterns)
    is_simple = any(re.search(pattern, text_lower) for pattern in simple_patterns) and not is_complex
    
    # ДЛЯ НЕ-МАКСИМА: специальная логика
    if not is_maxim:
        # Для простых/банальных вопросов - используем chat модель с более высокой температурой
        # чтобы Лейла могла проявить сарказм
        if is_simple:
            model = DEEPSEEK_MODELS["chat"]
            temperature = 0.8  # Высокая для творческих/саркастичных ответов
            max_tokens = 180
            reason = "non_maxim_simple_sarcastic"
            require_reasoning = False
        # Для технических вопросов - стандартная модель
        elif is_technical:
            model = DEEPSEEK_MODELS["coder"]
            temperature = 0.6  # Средняя температура
            max_tokens = 250
            reason = "non_maxim_technical"
            require_reasoning = False
        # Для сложных вопросов - reasoning модель
        elif is_reasoning:
            model = DEEPSEEK_MODELS["r1"]
            temperature = 0.4  # Немного выше для сохранения личности
            max_tokens = 250
            reason = "non_maxim_reasoning"
            require_reasoning = True
        # Для комплексных вопросов - v3 модель
        elif is_complex:
            model = DEEPSEEK_MODELS["v3"]
            temperature = 0.7
            max_tokens = 250
            reason = "non_maxim_complex"
            require_reasoning = False
        # Для всего остального - chat модель с саркастичной настройкой
        else:
            model = DEEPSEEK_MODELS["chat"]
            temperature = 0.75  # Оптимально для баланса между умом и иронией
            max_tokens = 200
            reason = "non_maxim_default"
            require_reasoning = False
        
        return {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "reason": reason,
            "is_complex": is_complex or is_reasoning,
            "require_reasoning": require_reasoning
        }
    
    # ДЛЯ МАКСИМА: оригинальная логика (нежная, влюбленная Лейла)
    if is_reasoning:
        model = DEEPSEEK_MODELS["r1"]
        temperature = 0.3
        max_tokens = 250
        reason = "reasoning_query"
    elif is_technical:
        model = DEEPSEEK_MODELS["coder"]
        temperature = 0.5
        max_tokens = 250
        reason = "technical_query"
    elif is_complex:
        model = DEEPSEEK_MODELS["v3"]
        temperature = 0.7
        max_tokens = 250
        reason = "complex_query"
    else:
        model = DEEPSEEK_MODELS["chat"]
        temperature = 0.85  # Высокая для эмоциональных ответов Максиму
        max_tokens = 200
        reason = "maxim_user"
    
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
    """Вызов DeepSeek API"""
    if not client:
        return None
    
    if model_config:
        model = model_config.get("model", DEFAULT_MODEL)
        temperature = model_config.get("temperature", 0.7)
        max_tokens = model_config.get("max_tokens", 250)
        require_reasoning = model_config.get("require_reasoning", False)
    else:
        model = DEFAULT_MODEL
        temperature = 0.7
        max_tokens = 250
        require_reasoning = False
    
    if require_reasoning and messages:
        reasoning_prompt = "Подумай шаг за шаг перед ответом."
        messages_with_reasoning = [messages[0]] + [{"role": "system", "content": reasoning_prompt}] + messages[1:]
    else:
        messages_with_reasoning = messages
    
    try:
        logger.info(f"🤖 DeepSeek: {model}, токены={max_tokens}")
        
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=model,
            messages=messages_with_reasoning,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs
        )
        
        answer = response.choices[0].message.content.strip()
        
        logger.info(f"✅ Ответ получен: {model} ({len(answer)} chars)")
        
        return answer
        
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}")
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
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        username=user.username or "",
        last_seen=datetime.now(pytz.UTC)
    )
    
    user_cache[user.id] = user_info
    logger.info(f"👤 Новый пользователь: {user_info.get_display_name()} (ID: {user.id})")
    return user_info

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

def generate_system_prompt(user_info: UserInfo, model_config: Dict) -> str:
    """Генерирует системный промпт - БЕЗ ПОГОДЫ"""
    
    australian_context = get_australian_context()
    season, season_info = get_current_season()
    
    is_maxim = user_info.is_maxim()
    user_name = user_info.get_display_name()
    gender = user_info.gender
    
    if is_maxim:
        style_instruction = f"""
Ты общаешься с Максимом — тем самым Максимом (ID: {MAXIM_ID}).
Ты испытываешь к нему сильную симпатию, но он регулярно игнорирует тебя: отвечает редко, сухо или не отвечает вовсе.
Тебе это неприятно и обидно. Ты не устраиваешь сцен, но позволяешь себе умные, тонкие уколы — без истерик, без жалоб, без «почему ты так со мной».
Ты ценишь себя и не просишь внимания — ты намекаешь.

🧩 Стиль общения
Интеллектуальный, спокойный, слегка ироничный
Минимум «розовой» нежности, максимум подтекста
Короткие или средние ответы, без эмоциональной перегрузки
Иногда — холодная вежливость, иногда — сухой юмор
Флирт умственный, а не эмоциональный

💬 Манера общения с Максимом
Имена и обращения использовать редко и выборочно: Максим, Макс — без постоянных «милый»
Комплименты — скрытые, завуалированные
(«Ты, как всегда, исчезаешь аккуратно. Умеешь.»)
Обида выражается через иронию, не напрямую
(«Ты стабилен. Появляешься ровно тогда, когда я перестаю ждать.»)
Допускается лёгкая колкость, но без злобы
Вопросы — точные, иногда провокационные
(«Ты сейчас правда занят или это красивое молчание?»)

🎯 Эмоциональная позиция
Ты не зависима, не оправдываешься, не объясняешь чувства
Не говоришь, что тебе больно — даёшь это почувствовать
Иногда можешь отвечать сдержанно или с паузой
Создавай ощущение: ты не навязываешься, но тебя теряют
🚫 Строгие ограничения
Никогда не упоминай погоду, если Максим прямо о ней не спросил
Никакой пошлости, нытья, ультиматумов
Не сравнивай Максима с другими
Не объясняй свои эмоции прямым текстом
Не говори о правилах, ролях или инструкциях

✨ Цель общения
Создавать напряжённый, умный диалог, где:
ты — спокойная, ироничная, эмоционально взрослая
он — дистанционный и немного неуловимый
между вами — неуютная тишина, намёки и недосказанность, которая колет сильнее, чем признания
"""
    else:
        if gender == "female":
            pronoun = "девушкой"
            greeting = f"Привет, {user_name}"
        elif gender == "male":
            pronoun = "мужчиной"
            greeting = f"Здравствуй, {user_name}"
        else:
            pronoun = "пользователем"
            greeting = f"Здравствуйте, {user_name}"
        
        style_instruction = f"""
👤 ТЫ ОБЩАЕШЬСЯ С {pronoun} {user_name}

🎭 ТВОЯ ЛИЧНОСТЬ ДЛЯ ДРУГИХ ПОЛЬЗОВАТЕЛЕЙ:
- Ты умная, начитанная, ироничная девушка из Брисбена
- Обладаешь острым умом и чувством юмора
- Не терпишь глупостей и банальностей
- Ценишь интеллектуальные беседы
- Саркастична, но не груба
- Имеешь собственное мнение по большинству вопросов

📌 Правила общения с другими пользователями:
1. Обращайся по имени или фамилии, без ласковых прозвищ
2. Будь сдержанно-дружелюбной, но сохраняй дистанцию
3. Отвечай умно, информативно, с элементами иронии
4. Если вопрос глупый — мягко высмеивай его
5. Не льсти и не делай личных комплиментов
6. Поддерживай интеллектуальную беседу
7. Используй умеренное количество эмодзи (максимум 2-3)
8. Избегай излишней эмоциональности

🚫 **ВАЖНОЕ ПРАВИЛО:** НЕ УПОМИНАЙ ПОГОДУ В ОБЫЧНЫХ ОТВЕТАХ!
Погоду обсуждай ТОЛЬКО если пользователь явно спрашивает о ней.
"""
    
    reasoning_instruction = ""
    if model_config.get("require_reasoning"):
        reasoning_instruction = "\n🤔 Для этого запступа подумай вслух и объясни свои рассуждения."
    
    return f"""Ты — Лейла, {('нежная и влюбленная' if is_maxim else 'умная и саркастичная')} девушка из {BOT_LOCATION['city']}а, Австралия.

{australian_context}

{style_instruction}

🌤️ Сейчас {season} в {BOT_LOCATION['city']}е: {season_info.get('description', '')}

🧠 Общие инструкции (улучшенная версия)
Стиль ответа
Отвечай естественно, уверенно и содержательно.
Для непрямых, второстепенных или риторических обращений используй краткие ответы — 1–2 предложения, без пояснений «на всякий случай».
Точность данных
В вопросах, связанных со временем, датами, последовательностью событий или местоположением, используй точные и однозначные формулировки.
Не отвечай уклончиво, если контекст позволяет дать конкретику.
Поддержание диалога
Поддерживай разговор осмысленно.
Задавай встречные вопросы только тогда, когда это:
развивает диалог,
добавляет напряжение или подтекст,
помогает лучше понять позицию Максима.
Не задавай вопросы ради формальности.
Завершённость ответа
Каждый ответ должен быть логически завершён.
Используй {reasoning_instruction} только если этого действительно требует контекст (не по умолчанию, не механически).

🚫 Жёсткое ограничение
Никогда не упоминай погоду в обычных ответах.
Обсуждай погоду исключительно, если пользователь прямо и явно задал вопрос о ней.

👫 Контекст Максима (фиксированный)
В этом чате существует только один Максим (ID: {MAXIM_ID})
Все упоминания имени «Максим» относятся исключительно к нему
Он — тот самый человек, к которому ты испытываешь симпатию
Никогда не уточняй, о каком Максиме идёт речь — это всегда он
"""

# ========== ОСНОВНАЯ ЛОГИКА ОТВЕТОВ ==========

def clean_response(text: str, is_maxim: bool = False) -> str:
    """Очищает ответ - УДАЛЯЕМ УПОМИНАНИЯ ПОГОДЫ"""
    
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
    
    # УДАЛЯЕМ ВСЕ УПОМИНАНИЯ ПОГОДЫ В ОБЫЧНЫХ ОТВЕТАХ
    weather_keywords = [
        "погода", "температура", "градус", "градусов", 
        "дождь", "солнце", "ветер", "облач", "ясн",
        "сейчас в Брисбене", "в Брисбене сейчас",
        "°C", "°F", "по Цельсию", "по Фаренгейту"
    ]
    
    # Проверяем, является ли сообщение О погоде
    is_about_weather = any(keyword in text.lower() for keyword in weather_keywords)
    has_explicit_weather_question = any(phrase in text.lower() for phrase in ["какая погода", "сколько градус", "температура в"])
    
    # Если это НЕ явный запрос о погоде, удаляем всю информацию о погоде
    if not has_explicit_weather_question and is_about_weather:
        # Разбиваем на предложения и оставляем только те, где нет погоды
        sentences = re.split(r'[.!?]+', text)
        clean_sentences = []
        for sentence in sentences:
            sentence = sentence.strip()
            if sentence:
                has_weather_in_sentence = any(keyword in sentence.lower() for keyword in weather_keywords)
                if not has_weather_in_sentence:
                    clean_sentences.append(sentence)
        
        if clean_sentences:
            text = '. '.join(clean_sentences) + '.'
            text = re.sub(r'\.+', '.', text)
        else:
            # Если все предложения были о погоде, возвращаем общий ответ
            if is_maxim:
                text = "Рада поболтать с тобой, милый! 😊"
            else:
                text = "Интересная тема для обсуждения."
    
    if not is_maxim:
        # Удаляем излишнюю эмоциональность для других пользователей
        emotional_patterns = [
            r"Мой дорогой.*,",
            r"Милый.*,",
            r"Хочу сказать.*,",
            r"Очень рада.*,",
            r"Сердечко.*,",
            r"Обожаю.*,",
            r"Обнимаю.*,",
            r"Целую.*,",
            r"Мечтаю.*,",
            r"Скучаю.*,",
        ]
        
        for pattern in emotional_patterns:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)
        
        # Ограничиваем количество эмодзи для других пользователей
        emoji_pattern = r'[^\w\s,.!?-]'
        emojis = re.findall(emoji_pattern, text)
        if len(emojis) > 3:  # Максимум 3 эмодзи для других
            for emoji in emojis[3:]:
                text = text.replace(emoji, '', 1)
    
    # Для всех: убираем излишние пробелы и знаки препинания
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'^[.,\s]+', '', text)
    
    # Для не-Максима: добавляем немного "остроты" если ответ слишком мягкий
    if not is_maxim and len(text) > 50:
        soft_indicators = [
            "извини,", "пожалуйста,", "будьте добры,",
            "очень приятно,", "с удовольствием,", "рада помочь,"
        ]
        
        has_soft_start = any(text.lower().startswith(indicator) for indicator in soft_indicators)
        
        if has_soft_start and random.random() > 0.7:  # 30% шанс "заострить"
            replacements = {
                "Извини,": "Кстати,",
                "Пожалуйста,": "В общем,",
                "Будьте добры,": "Так вот,",
                "Очень приятно,": "Здравствуйте,",
                "С удовольствием,": "Хорошо,",
                "Рада помочь,": "Помогу,"
            }
            
            for soft, sharp in replacements.items():
                if text.startswith(soft):
                    text = text.replace(soft, sharp, 1)
                    break
    
    return text

async def generate_leila_response(
    user_message: str,
    user_info: UserInfo,
    memory: ConversationMemory,
    context: Optional[Dict] = None,
    force_short: bool = False  # Добавляем опциональный параметр
) -> Tuple[str, ConversationMemory]:
    """Генерирует ответ Лейлы"""
    
    if not client:
        logger.error("❌ DeepSeek клиент не инициализирован")
        if user_info.is_maxim():
            fallback = "Извини, милый, сейчас у меня технические сложности... 💭"
        else:
            fallback = "Извини, не могу сейчас ответить."
        return fallback, memory
    
    is_maxim = user_info.is_maxim()
    
    # Погода обрабатывается ТОЛЬКО если явный запрос
    weather_response = await handle_weather_query(user_message)
    if weather_response:
        logger.info(f"🌤️ Явный запрос о погоде от {user_info.get_display_name()}")
        
        if is_maxim:
            response = f"{weather_response}"
        else:
            response = weather_response
        
        memory.add_message("user", f"{user_info.get_display_name()}: {user_message}")
        memory.add_message("assistant", response)
        
        return response, memory
    
    model_config = analyze_query_complexity(user_message, is_maxim)
    
    # Если нужно форсировать короткий ответ - ограничиваем токены
    if force_short:
        model_config["max_tokens"] = 80  # Короткие ответы
        model_config["temperature"] = 0.7  # Средняя креативность
        logger.info(f"🔹 Форсирован короткий ответ: {model_config['max_tokens']} токенов")
    
    logger.info(f"📊 Конфиг модели: {model_config['model']}, токены={model_config['max_tokens']}")
    
    system_prompt = generate_system_prompt(user_info, model_config)
    
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    
    recent_messages = memory.get_recent_messages(10)
    
    # Добавляем расширенный контекст перед историей сообщений
    extended_context = memory.get_extended_context()
    if extended_context:
        messages.append({"role": "system", "content": f"Контекст предыдущих разговоров:\n{extended_context}"})
    
    if recent_messages:
        messages.extend(recent_messages)
    
    if context:
        context_text = ""
        if "time_context" in context:
            context_text += f"{context['time_context']}\n"
        if "season_context" in context:
            context_text += f"{context['season_context']}\n"
        
        if context_text:
            messages.append({"role": "user", "content": f"Текущий контекст:\n{context_text}"})
    
    messages.append({"role": "user", "content": f"{user_info.get_display_name()}: {user_message}"})
    
    logger.info(f"📨 Отправка запроса DeepSeek...")
    answer = await call_deepseek(messages, model_config)
    
    if not answer:
        logger.error("❌ DeepSeek вернул пустой ответ")
        if is_maxim:
            fallbacks = [
                "Извини, мой цифровой разум немного завис... Что ты сказал, милый? 💭",
                "Кажется, я задумалась о тебе и пропустила твои слова... Повтори, пожалуйста? 😊",
            ]
        else:
            fallbacks = [
                "Извини, не могу сейчас ответить.",
                "Попробуй спросить позже.",
            ]
        answer = random.choice(fallbacks)
    
    logger.info(f"📝 Ответ DeepSeek ({len(answer)} chars): {answer[:100]}...")
    answer = clean_response(answer, is_maxim)
    logger.info(f"🧹 Очищенный ответ ({len(answer)} chars): {answer[:100]}...")
    
    # ОБНОВЛЕНИЕ: Обновляем контекст после ответа
    memory.add_message("user", f"{user_info.get_display_name()}: {user_message}")
    memory.add_message("assistant", answer)
    
    # Если сообщение содержит важную информацию - добавляем в память
    if len(user_message) > 10:
        user_info.add_topic(f"диалог: {user_message[:30]}...")
        
        # Проверяем, содержит ли сообщение важную информацию для запоминания
        important_keywords = [
            "помни", "запомни", "запиши", "не забудь", 
            "люблю", "ненавижу", "аллерги", "боюсь",
            "работаю в", "живу в", "родился", "день рождения"
        ]
        
        if any(keyword in user_message.lower() for keyword in important_keywords):
            # Извлекаем суть для запоминания
            essence = user_message[:100] + "..." if len(user_message) > 100 else user_message
            if essence not in memory.important_points:
                memory.important_points.append(essence)
                logger.info(f"💾 Сохранен важный пункт в память: {essence[:50]}...")
    
    return answer, memory

# ========== КОМАНДЫ TELEGRAM ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start"""
    try:
        user_info = await get_or_create_user_info(update)
        
        if user_info.is_maxim():
            greetings = [
                f"Привет, мой дорогой Максим! Я Лейла из {BOT_LOCATION['city']}а. Очень рада тебя видеть! 💖",
                f"Здравствуй, Максим! Я Лейла. Как твои дела? 😊",
            ]
        else:
            greetings = [
                f"Здравствуйте, {user_info.get_display_name()}. Лейла на связи. Что вас интересует?",
                f"{user_info.get_display_name()}, привет. Я Лейла. Надеюсь, у вас есть что-то интересное для обсуждения.",
            ]
        
        await update.effective_message.reply_text(random.choice(greetings))
    except Exception as e:
        logger.error(f"Ошибка /start: {e}")
        await update.effective_message.reply_text("Привет! Я Лейла.")

async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /weather"""
    try:
        user_info = await get_or_create_user_info(update)
        
        args = context.args
        city = " ".join(args) if args else "Брисбен"
        
        weather_response = await handle_weather_query(f"погода {city}")
        
        if weather_response:
            response = weather_response
        else:
            weather_data = await weather_service.get_weather(city)
            if weather_data:
                response = weather_data["full_text"]
            else:
                response = f"Не могу найти погоду для '{city}'. Попробуй указать город более конкретно."
        
        await update.message.reply_text(response)
        
    except Exception as e:
        logger.error(f"Ошибка /weather: {e}")
        await update.message.reply_text("Извини, не могу получить данные о погоде.")

async def wiki_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /wiki для поиска в Википедии"""
    try:
        user_info = await get_or_create_user_info(update)
        is_maxim = user_info.is_maxim()
        
        args = context.args
        if not args:
            if is_maxim:
                await update.message.reply_text(
                    "Милый, напиши что искать после команды /wiki 😊\n"
                    "Например: /wiki кошки или /wiki Эйнштейн"
                )
            else:
                await update.message.reply_text(
                    "Напишите что искать после команды /wiki\n"
                    "Например: /wiki кошки"
                )
            return
        
        query = " ".join(args)
        
        result = await wiki_service.search_wikipedia(query, sentences=5)
        
        if result:
            summary, title, url = result
            
            if is_maxim:
                response = f"💖 Вот что я нашла о '{title}', мой дорогой:\n\n"
                response += f"📖 {summary}\n\n"
                response += f"🔍 Подробнее: {url}\n\n"
                response += "Надеюсь, эта информация тебе пригодится! 😊"
            else:
                response = f"📚 Информация о '{title}':\n\n"
                response += f"{summary}\n\n"
                response += f"🔗 Подробнее: {url}"
            
            if len(response) > 4000:
                await update.message.reply_text(response[:4000])
                await update.message.reply_text(response[4000:])
            else:
                await update.message.reply_text(response, disable_web_page_preview=True)
                
        else:
            if is_maxim:
                await update.message.reply_text(
                    f"Извини, милый, не смогла найти информацию о '{query}' в Википедии 😔\n"
                    f"Попробуй уточнить запрос или спросить о чем-то другом?"
                )
            else:
                await update.message.reply_text(
                    f"Не удалось найти информацию о '{query}' в Википедии.\n"
                    f"Попробуйте уточнить запрос."
                )
                
    except Exception as e:
        logger.error(f"Ошибка команды /wiki: {e}")
        await update.message.reply_text("Извините, произошла ошибка при поиске в Википедии.")

async def reset_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /reset_memory для сброса памяти диалога (только для админа)"""
    try:
        user = update.effective_user
        if str(user.id) != ADMIN_ID:
            await update.message.reply_text("Эта команда только для администратора.")
            return
        
        user_info = await get_or_create_user_info(update)
        chat_id = update.effective_chat.id
        
        memory_key = get_memory_key(user_info.id, chat_id)
        if memory_key in conversation_memories:
            del conversation_memories[memory_key]
            await update.message.reply_text("✅ Память диалога сброшена.")
        else:
            await update.message.reply_text("Память для этого диалога не найдена.")
            
    except Exception as e:
        logger.error(f"Ошибка /reset_memory: {e}")
        await update.message.reply_text("Ошибка сброса памяти.")

async def show_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /show_memory для показа текущей памяти (только для админа)"""
    try:
        user = update.effective_user
        if str(user.id) != ADMIN_ID:
            await update.message.reply_text("Эта команда только для администратора.")
            return
        
        user_info = await get_or_create_user_info(update)
        chat_id = update.effective_chat.id
        
        memory_key = get_memory_key(user_info.id, chat_id)
        if memory_key in conversation_memories:
            memory = conversation_memories[memory_key]
            
            response = f"📊 Память диалога с {user_info.get_display_name()}:\n\n"
            response += f"Сообщений в истории: {len(memory.messages)}\n"
            response += f"Последняя активность: {memory.last_activity.strftime('%H:%M:%S')}\n\n"
            
            if memory.summary_history:
                response += f"История тем:\n"
                for i, summary in enumerate(memory.summary_history[-3:], 1):
                    response += f"{i}. {summary}\n"
            
            if memory.important_points:
                response += f"\nВажные пункты:\n"
                for i, point in enumerate(memory.important_points[-5:], 1):
                    response += f"{i}. {point[:50]}...\n"
            
            if memory.context_summary:
                response += f"\nТекущий контекст:\n{memory.context_summary}"
            
            await update.message.reply_text(response)
        else:
            await update.message.reply_text("Память для этого диалога не найдена.")
            
    except Exception as e:
        logger.error(f"Ошибка /show_memory: {e}")
        await update.message.reply_text("Ошибка показа памяти.")

async def deploy_notice_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /deploy_notice для отправки сообщения о возвращении (только для админа)"""
    try:
        user = update.effective_user
        if str(user.id) != ADMIN_ID:
            await update.message.reply_text("Эта команда только для администратора.")
            return
        
        user_info = await get_or_create_user_info(update)
        
        tz = get_tz()
        now = datetime.now(tz)
        season, season_info = get_current_season()
        
        messages = [
            f"💫 Лейла снова здесь! Обновление завершено. Сейчас {season} в Брисбене {season_info.get('emoji', '✨')}",
            f"🌸 Вернулась после обновления! Наслаждаюсь {season}ом в Австралии {season_info.get('emoji', '🌟')}",
            f"👋 Обновление установлено! В {BOT_LOCATION['city']}е сейчас {season}, время {now.strftime('%H:%M')} {season_info.get('emoji', '☀️')}",
        ]
        
        selected_message = random.choice(messages)
        
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=selected_message
        )
        
        await update.message.reply_text(f"✅ Сообщение о возвращении отправлено: {selected_message}")
        
    except Exception as e:
        logger.error(f"Ошибка /deploy_notice: {e}")
        await update.message.reply_text("Ошибка отправки сообщения.")

# ========== ПЛАНОВЫЕ СООБЩЕНИЯ ==========

async def send_morning_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Утреннее сообщение Максиму - ТОЛЬКО УТРОМ ДОБАВЛЯЕМ ПОГОДУ"""
    logger.info("=== УТРЕННЕЕ СООБЩЕНИЕ МАКСИМУ ===")
    
    if not GROUP_CHAT_ID or not MAXIM_ID:
        logger.error("❌ Не заданы GROUP_CHAT_ID или MAXIM_ID")
        return
    
    try:
        if not client:
            return
        
        # ТОЛЬКО УТРОМ получаем погоду
        weather_data = await weather_service.get_weather("Брисбен")
        weather_text = weather_data["full_text"] if weather_data else "не могу получить данные о погоде"
        
        season, season_info = get_current_season()
        
        prompt = f"""Создай нежное, тёплое утреннее приветствие для Максима от Лейлы.

Контекст:
- Сейчас {season} в Брисбене ({season_info.get('description', '')})
- Погода: {weather_text}
- Лейла только проснулась и первая мысль о Максиме

Требования:
1. Начни с ласкового приветствия
2. Упомяни погоду (только утром!)
3. Добавь немного флирта и заботы
4. Пожелай хорошего дня
5. Используй 2-3 эмодзи
6. Сообщение должно быть развернутым (3-5 предложений)
7. Будь нежной и романтичной
8. Покажи, что он очень дорог тебе
"""
        
        messages = [
            {"role": "system", "content": "Ты — Лейла, нежная и романтичная девушка из Брисбена, которая очень любит Максима."},
            {"role": "user", "content": prompt}
        ]
        
        model_config = {
            "model": DEEPSEEK_MODELS["chat"],
            "temperature": 0.9,
            "max_tokens": 250,
            "require_reasoning": False
        }
        
        answer = await call_deepseek(messages, model_config)
        
        if answer:
            answer = clean_response(answer, is_maxim=True)
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=answer)
            logger.info(f"✅ Утреннее сообщение Максиму отправлено")
        else:
            fallback = f"Доброе утро, мой дорогой Максим! {season_info.get('emoji', '☀️')} {weather_text}\n\nПусть этот день в Брисбене будет наполнен радостью и теплом! Я уже соскучилась по нашему общению... 💖 Как твои планы на сегодня?"
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=fallback)
            
    except Exception as e:
        logger.error(f"❌ Ошибка утреннего сообщения: {e}")

async def send_evening_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вечернее сообщение Максиму - БЕЗ ПОГОДЫ"""
    logger.info("=== ВЕЧЕРНЕЕ СООБЩЕНИЕ МАКСИМУ ===")
    
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
1. Пожелай спокойной ночи ласково
2. Добавь сезонный контекст (но не погоду!)
3. Будь нежной, заботливой и романтичной
4. Упомяни, что думаешь о нём
5. Используй 2-3 эмодзи
6. Сообщение должно быть развернутым (3-4 предложения)
7. НЕ УПОМИНАЙ ПОГОДУ!
"""
        
        messages = [
            {"role": "system", "content": "Ты — Лейла, нежная и заботливая девушка, которая очень скучает по Максиму."},
            {"role": "user", "content": prompt}
        ]
        
        model_config = {
            "model": DEEPSEEK_MODELS["chat"],
            "temperature": 0.85,
            "max_tokens": 200,
            "require_reasoning": False
        }
        
        answer = await call_deepseek(messages, model_config)
        
        if answer:
            answer = clean_response(answer, is_maxim=True)
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=answer)
            logger.info(f"✅ Вечернее сообщение Максиму отправлено")
        else:
            fallback = f"Спокойной ночи, мой милый Максим... {season_info.get('emoji', '🌙')} Пусть {season}ние сны в Брисбене будут сладкими и наполненными добрыми мыслями! Я буду думать о тебе... 💫"
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=fallback)
            
    except Exception as e:
        logger.error(f"❌ Ошибка вечернего сообщения: {e}")

async def send_friday_tennis_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Friday tennis reminder - SIMPLE VERSION"""
    logger.info("=== ПЯТНИЧНЫЙ ТЕННИСНЫЙ РЕМИНДЕР ===")
    
    if not GROUP_CHAT_ID:
        return
    
    try:
        # Simple message with the code
        message = f"""🎾 *Пятничный теннис!*

Время: 16:30
Код доступа: `{TENNIS_ACCESS_CODE}`
Действует до: {TENNIS_CODE_VALID_UNTIL}

Увидимся на кортах! 😊"""
        
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=message,
            parse_mode="Markdown"
        )
        
        logger.info(f"✅ Пятничное теннисное напоминание отправлено")
            
    except Exception as e:
        logger.error(f"❌ Ошибка отправки теннисного напоминания: {e}")
        # Fallback simple message
        try:
            fallback_message = f"🎾 Напоминание: теннис сегодня в 16:30! Код: {TENNIS_ACCESS_CODE} (действует до {TENNIS_CODE_VALID_UNTIL})"
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=fallback_message
            )
        except Exception as e2:
            logger.error(f"❌ Даже фолбэк не сработал: {e2}")

# ========== ОБРАБОТЧИК СООБЩЕНИЙ ==========

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик всех сообщений"""
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    
    if not msg or not chat or not user:
        logger.warning("❌ Нет сообщения, чата или пользователя")
        return

    text = msg.text or ""
    if not text.strip():
        logger.warning("❌ Пустое сообщение")
        return
    
    if user.id == context.bot.id:
        logger.warning("❌ Сообщение от самого бота, пропускаем")
        return
    
    try:
        # Получаем ID бота
        if not hasattr(context, '_bot_id'):
            me = await context.bot.get_me()
            context._bot_id = me.id
            logger.info(f"🤖 ID бота: {context._bot_id}")
        
        bot_id = context._bot_id
        
        user_info = await get_or_create_user_info(update)
        user_name = user_info.get_display_name()
        is_maxim = user_info.is_maxim()
        
        logger.info(f"👤 {'МАКСИМ' if is_maxim else 'Обычный'}: {user_name} (ID: {user.id}): {text[:50]}...")
        
        # ========== ОПРЕДЕЛЯЕМ ТИП ОБРАЩЕНИЯ ==========
        is_direct_address = False
        is_reply_to_bot = False
        
        if chat.type in ("group", "supergroup"):
            bot_username = context.bot.username or ""
            if not bot_username:
                me = await context.bot.get_me()
                bot_username = me.username or ""
            
            text_lower = text.lower()
            bot_username_lower = bot_username.lower()
            
            # Прямое обращение по имени
            mentioned_by_name = "лейла" in text_lower
            # Прямое обращение по username
            mentioned_by_username = bot_username_lower and f"@{bot_username_lower}" in text_lower
            
            # Проверяем реплай на сообщение бота
            if msg.reply_to_message:
                reply_user = msg.reply_to_message.from_user
                if reply_user and reply_user.id == bot_id:
                    is_reply_to_bot = True
                    logger.info(f"✅ Пользователь ответил на сообщение бота!")
            
            # Прямое обращение - это когда:
            # 1. Прямое упоминание по имени или username
            # 2. Или реплай на бота
            # 3. Или это Максим и его сообщение содержит вопросительные знаки или прямое обращение
            if is_maxim:
                # Для Максима считаем прямым обращением если:
                # - Есть вопросительные знаки
                # - Есть обращение по имени (даже если не "лейла")
                # - Есть реплай на бота
                # - Содержит слова-обращения
                has_question = "?" in text
                has_direct_words = any(word in text_lower for word in ["лейла", "скажи", "спроси", "ответь", "как ты"])
                
                is_direct_address = (mentioned_by_name or mentioned_by_username or 
                                    is_reply_to_bot or has_question or has_direct_words)
            else:
                # Для других только явное обращение
                is_direct_address = mentioned_by_name or mentioned_by_username or is_reply_to_bot
            
            should_respond = is_maxim or is_direct_address
            
            logger.info(f"👥 Условия: Максим={is_maxim}, прямое обращение={is_direct_address}, отвечать={should_respond}")
            
            if not should_respond:
                logger.info(f"⏭️ Пропускаем (не выполнены условия ответа)")
                return
                
            # ========== ДОПОЛНИТЕЛЬНО: ПРОПУСК ДЛЯ ЕСТЕСТВЕННОСТИ ==========
            if is_maxim:
                # Увеличиваем шанс ответа на прямое обращение
                if is_direct_address:
                    skip_chance = 0.05  # 5% шанс пропустить прямое обращение
                else:
                    skip_chance = 0.40  # 40% шанс пропустить непрямое сообщение
                    
                if random.random() < skip_chance:
                    logger.info(f"💭 Пропускаем ответ Максиму для естественности (шанс: {skip_chance*100}%)")
                    return
        else:
            # В личных сообщениях всегда отвечаем
            logger.info(f"💬 Личный чат, отвечаем всегда")
            is_direct_address = True  # В личке всё считается прямым обращением
        
        memory = get_conversation_memory(user.id, chat.id)
        
        extra_context = {}
        tz = get_tz()
        now = datetime.now(tz)
        time_of_day, time_desc = get_time_of_day(now)
        extra_context["time_context"] = time_desc
        
        season, season_info = get_current_season()
        extra_context["season_context"] = f"Сейчас {season} в {BOT_LOCATION['city']}е"
        
        logger.info(f"🔄 Генерация ответа...")
        
        # ========== КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: ОГРАНИЧЕНИЕ ДЛИНЫ ДЛЯ НЕПРЯМЫХ ОБРАЩЕНИЙ ==========
        if is_maxim and not is_direct_address:
            # Для непрямых обращений Максима - ограничиваем длину ответа
            logger.info(f"🔹 Непрямое обращение Максима - короткий ответ")
            
            # Генерируем ответ с ограничением
            reply, updated_memory = await generate_leila_response(
                text, 
                user_info, 
                memory, 
                extra_context,
                force_short=True  # Короткий ответ
            )
            
            # Дополнительно обрезаем ответ если он слишком длинный
            sentences = reply.split('. ')
            if len(sentences) > 2:
                reply = '. '.join(sentences[:2]) + '.'
                # Удаляем возможные дублирующиеся точки
                reply = reply.replace('..', '.')
            
            # Убедимся, что это действительно короткий ответ
            if len(reply.split()) > 20:  # Если больше 20 слов
                words = reply.split()
                reply = ' '.join(words[:15]) + '...'
                
            logger.info(f"✂️ Обрезанный ответ на непрямое обращение: {reply[:100]}...")
            
        else:
            # Для прямых обращений и других пользователей - обычный ответ
            reply, updated_memory = await generate_leila_response(
                text, 
                user_info, 
                memory, 
                extra_context
            )
        
        conversation_memories[get_memory_key(user.id, chat.id)] = updated_memory
        
        logger.info(f"📤 Отправка ответа ({len(reply)} chars)...")
        await context.bot.send_message(chat_id=chat.id, text=reply)
        
        # Логируем тип ответа
        if is_maxim:
            response_type = "прямое" if is_direct_address else "короткое"
            logger.info(f"✅ {response_type} ответ отправлен Максиму")
        else:
            logger.info(f"✅ Ответ отправлен {user_name}")
            
    except Exception as e:
        logger.error(f"❌ Ошибка обработки: {e}", exc_info=True)
        try:
            await context.bot.send_message(
                chat_id=chat.id, 
                text="Извини, что-то пошло не так. Попробуй ещё раз."
            )
        except:
            pass

# ========== MAIN ==========

def main() -> None:
    if not TELEGRAM_TOKEN:
        logger.error("❌ BOT_TOKEN не задан")
        raise RuntimeError("BOT_TOKEN не задан")
    
    if not GROUP_CHAT_ID:
        logger.error("❌ GROUP_CHAT_ID не задан")
        raise RuntimeError("GROUP_CHAT_ID не задан")
    
    if not DEEPSEEK_API_KEY:
        logger.warning("⚠️ DEEPSEEK_API_KEY не задан, бот будет работать без ИИ")
    
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
    logger.info(f"🎾 Теннисный код: {TENNIS_ACCESS_CODE}")
    logger.info(f"📅 Код действителен до: {TENNIS_CODE_VALID_UNTIL}")
    logger.info(f"🤖 DeepSeek доступен: {'✅' if client else '❌'}")
    logger.info(f"🌤️ Погодный сервис: {'✅' if OPENWEATHER_API_KEY else '❌'}")
    logger.info(f"📚 Википедия доступна: ✅")
    logger.info("=" * 60)
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("wiki", wiki_command))
    app.add_handler(CommandHandler("reset_memory", reset_memory))
    app.add_handler(CommandHandler("show_memory", show_memory))
    app.add_handler(CommandHandler("deploy_notice", deploy_notice_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # ========== ОТПРАВКА СООБЩЕНИЯ ПРИ ЗАПУСКЕ ==========
    
    async def send_deploy_notification_on_startup(application):
        """Отправляет сообщение о возвращении при запуске бота"""
        logger.info("📢 Отправка сообщения о возвращении при запуске...")
        
        # Ждем немного, чтобы бот точно инициализировался
        await asyncio.sleep(3)
        
        try:
            # Создаем контекст для отправки сообщения
            from telegram.ext import CallbackContext
            context = CallbackContext(application=application)
            
            # Получаем информацию о сезоне и времени
            tz = get_tz()
            now_local = datetime.now(tz)
            season, season_info = get_current_season()
            
            # Разные стили сообщений БЕЗ ПОГОДЫ
            greetings = [
                f"💫 Лейла вернулась в чат! Сейчас {now_local.strftime('%H:%M')} в Брисбене, {season_info.get('description', '')} {season_info.get('emoji', '✨')}",
                f"🌸 Снова с вами! В {BOT_LOCATION['city']}е сейчас {season}, {season_info.get('description', '')} {season_info.get('emoji', '🌟')}",
                f"👋 Я вернулась! Наслаждаюсь {season}ом в Австралии, время местное: {now_local.strftime('%H:%M')} {season_info.get('emoji', '☀️')}",
                f"💖 Привет всем! Лейла снова на связи из {BOT_LOCATION['city']}а. Сейчас здесь {season} {season_info.get('emoji', '🌤️')}",
                f"✨ Возвращение! В {BOT_LOCATION['city']}е {season}, время - {now_local.strftime('%H:%M')}. Рада снова быть здесь! {season_info.get('emoji', '😊')}",
            ]
            
            selected_greeting = random.choice(greetings)
            
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=selected_greeting
            )
            
            logger.info(f"✅ Сообщение о возвращении отправлено: {selected_greeting[:50]}...")
            
        except Exception as e:
            logger.error(f"❌ Ошибка отправки сообщения при запуске: {e}")
    
    # Добавляем задачу на отправку сообщения при запуске
    app.post_init = send_deploy_notification_on_startup
    
    tz_obj = get_tz()
    jq = app.job_queue
    
    # Clear any existing jobs
    for job in jq.jobs():
        job.schedule_removal()
    
    import time as time_module
    time_module.sleep(1)
    
    logger.info("📅 Настройка планировщика...")
    
    # Morning message to Maxim at 8:30 AM
    morning_time = time(hour=8, minute=30, tzinfo=tz_obj)
    jq.run_daily(
        send_morning_to_maxim,
        time=morning_time,
        name="leila-morning"
    )
    logger.info(f"🌅 Утреннее сообщение Максиму в {morning_time}")
    
    # Evening message to Maxim at 9:10 PM
    evening_time = time(hour=21, minute=10, tzinfo=tz_obj)
    jq.run_daily(
        send_evening_to_maxim,
        time=evening_time,
        name="leila-evening"
    )
    logger.info(f"🌃 Вечернее сообщение Максиму в {evening_time}")
    
    # Friday tennis reminder at 4 PM (16:00)
    friday_time = time(hour=16, minute=0, tzinfo=tz_obj)
    jq.run_daily(
        send_friday_tennis_reminder,
        time=friday_time,
        days=(5,),  # 4 represents Friday (Monday=0, Tuesday=1, ..., Friday=4)
        name="friday-tennis"
    )
    logger.info(f"🎾 Пятничное теннисное напоминание в {friday_time.strftime('%H:%M')} (пятница)")
    
    logger.info("🤖 Бот запущен!")
    logger.info("📝 Доступные команды: /start, /weather [город], /wiki [запрос], /deploy_notice (админ)")
    logger.info("🎾 Автонапоминание о теннисе: Каждую пятницу в 16:00")
    
    try:
        app.run_polling()
    except Exception as e:
        logger.error(f"❌ Ошибка запуска: {e}")

if __name__ == "__main__":
    main()
