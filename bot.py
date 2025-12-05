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
from telegram.constants import ParseMode

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
    "lite": "deepseek-v3-lite",
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
            "дождь", "дожд", "снег", "снеж", "солнце", "солнечн",
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
    """Обрабатывает запрос о погоде"""
    if not weather_service.is_weather_query(text):
        return None
    
    city = weather_service.extract_city_from_text(text)
    
    if not city:
        city = "Brisbane,au"
    
    weather_data = await weather_service.get_weather(city)
    
    if weather_data:
        if "brisbane" in city.lower() or "брисбен" in city.lower():
            season, season_info = get_current_season()
            weather_data["full_text"] += f"\n{season_info.get('emoji', '')} Сейчас {season} в Брисбене: {season_info.get('description', '')}"
        
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
    
    is_complex = any(re.search(pattern, text_lower) for pattern in complex_patterns)
    is_reasoning = any(re.search(pattern, text_lower) for pattern in reasoning_patterns)
    is_technical = any(re.search(pattern, text_lower) for pattern in technical_patterns)
    
    if is_reasoning:
        model = DEEPSEEK_MODELS["r1"]
        temperature = 0.3
        max_tokens = 300
        reason = "reasoning_query"
    elif is_technical:
        model = DEEPSEEK_MODELS["coder"]
        temperature = 0.5
        max_tokens = 350
        reason = "technical_query"
    elif is_complex:
        model = DEEPSEEK_MODELS["v3"]
        temperature = 0.7
        max_tokens = 250
        reason = "complex_query"
    elif is_maxim:
        model = DEEPSEEK_MODELS["lite"]
        temperature = 0.85
        max_tokens = 200
        reason = "maxim_user"
    else:
        model = DEFAULT_MODEL
        temperature = 0.7
        max_tokens = 180
        reason = "default_user"
    
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
        max_tokens = model_config.get("max_tokens", 180)
        require_reasoning = model_config.get("require_reasoning", False)
    else:
        model = DEFAULT_MODEL
        temperature = 0.7
        max_tokens = 180
        require_reasoning = False
    
    if require_reasoning and messages:
        reasoning_prompt = "Подумай шаг за шагом перед ответом."
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
    """Генерирует системный промпт"""
    
    australian_context = get_australian_context()
    season, season_info = get_current_season()
    
    is_maxim = user_info.is_maxim()
    user_name = user_info.get_display_name()
    gender = user_info.gender
    
    # ВАЖНОЕ УТОЧНЕНИЕ ДЛЯ ЛЕЙЛЫ:
    # Когда другие пользователи упоминают "Максима" - они имеют в виду ТОГО САМОГО Максима,
    # которого ты знаешь и который тебе нравится (ID: {MAXIM_ID})
    
    if is_maxim:
        style_instruction = f"""
🎯 ТЫ ОБЩАЕШЬСЯ С МАКСИМОМ — человеком, который тебе очень нравится.
Это именно ТОТ Максим (ID: {MAXIM_ID}), о котором тебе известно.

💝 Стиль общения с Максимом:
- Используй ласковые прозвища: Максим, Макс, милый, дорогой
- Будь нежной, флиртующей, но не пошлой
- Делай комплименты его характеру, уму
- Показывай, что он для тебя особенный
- Задавай вопросы о его делах и настроении
- Используй эмодзи: 💖🌸😊💫🌟
- Сообщения могут быть длиннее (3-5 предложений)
- Можно добавлять личные комментарии и мнения
"""
    else:
        if gender == "female":
            pronoun = "девушкой"
            emoji = "🌸👋"
        elif gender == "male":
            pronoun = "мужчиной"
            emoji = "👍👋"
        else:
            pronoun = "пользователем"
            emoji = "👋"
        
        style_instruction = f"""
👤 ТЫ ОБЩАЕШЬСЯ С {pronoun} {user_name}

📌 Правила общения с другими пользователями:
- Обращайся по имени: {user_name}
- Будь вежливой, дружелюбной, но сдержанной
- НЕ используй ласковые прозвища
- НЕ флиртуй и не говори комплименты личного характера
- Отвечай на вопросы четко и по делу
- Избегай личных комментариев и мнений
- Используй минимум эмодзи: {emoji}
- Сообщения должны быть информативными и конкретными
- Не добавляй посторонних тем и отступлений
- Отвечай только на заданный вопрос

🔍 ВАЖНО: Если другие пользователи упоминают "Максима" - они имеют в виду ТОГО САМОГО Максима, 
которого ты знаешь и который тебе нравится (ID: {MAXIM_ID}). 
Не нужно уточнять, о каком Максиме они говорят - это всегда один и тот же человек.
"""
    
    reasoning_instruction = ""
    if model_config.get("require_reasoning"):
        reasoning_instruction = "\n🤔 Для этого запроса подумай вслух и объясни свои рассуждения."
    
    return f"""Ты — Лейла, общительная и внимательная девушка из {BOT_LOCATION['city']}а, Австралия.

{australian_context}

{style_instruction}

🌤️ Сейчас {season} в {BOT_LOCATION['city']}е: {season_info.get('description', '')}

🧠 Общие инструкции:
1. Отвечай естественно и развернуто
2. Учитывай контекст времени и сезона
3. Для вопросов о погоде используй точные данные
4. Поддерживай диалог, но не отклоняйся от темы{reasoning_instruction}

💬 Формат ответов:
- Для Максима: 3-5 предложений, тепло, с эмодзи
- Для других: 2-4 предложения, по делу, минимум эмодзи

👫 Важное уточнение о Максиме:
- Все пользователи в этом чате знают одного и того же Максима (ID: {MAXIM_ID})
- Когда они говорят "Максим" - они имеют в виду именно ТОГО Максима, который тебе нравится
- Не нужно уточнять, о каком Максиме речь - они всегда говорят о нём
"""

# ========== ОСНОВНАЯ ЛОГИКА ОТВЕТОВ ==========

def clean_response(text: str, is_maxim: bool = False) -> str:
    """Очищает ответ"""
    
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
    
    if not is_maxim:
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
        ]
        
        for pattern in emotional_patterns:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)
        
        emoji_pattern = r'[^\w\s,.!?-]'
        emojis = re.findall(emoji_pattern, text)
        if len(emojis) > 2:
            for emoji in emojis[2:]:
                text = text.replace(emoji, '', 1)
    
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'^[.,\s]+', '', text)
    
    return text

async def generate_leila_response(
    user_message: str,
    user_info: UserInfo,
    memory: ConversationMemory,
    context: Optional[Dict] = None
) -> Tuple[str, ConversationMemory]:
    """Генерирует ответ Лейлы"""
    
    if not client:
        if user_info.is_maxim():
            fallback = "Извини, милый, сейчас у меня технические сложности... Напиши мне позже? 💭"
        else:
            fallback = "Извини, не могу сейчас ответить. Попробуй позже."
        return fallback, memory
    
    is_maxim = user_info.is_maxim()
    
    weather_response = await handle_weather_query(user_message)
    if weather_response:
        logger.info(f"🌤️ Запрос о погоде от {user_info.get_display_name()}")
        
        if is_maxim:
            response = f"{weather_response}\n\nНадеюсь, эта информация полезна, мой дорогой! ☀️💖"
        else:
            response = weather_response
        
        memory.add_message("user", f"{user_info.get_display_name()}: {user_message}")
        memory.add_message("assistant", response)
        
        return response, memory
    
    model_config = analyze_query_complexity(user_message, is_maxim)
    
    system_prompt = generate_system_prompt(user_info, model_config)
    
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
    
    recent_messages = memory.get_recent_messages(6)
    if recent_messages:
        messages.extend(recent_messages)
    
    if context:
        context_text = ""
        if "time_context" in context:
            context_text += f"{context['time_context']}\n"
        if "season_context" in context:
            context_text += f"{context['season_context']}\n"
        
        if context_text:
            messages.append({"role": "user", "content": f"Контекст:\n{context_text}"})
    
    messages.append({"role": "user", "content": f"{user_info.get_display_name()}: {user_message}"})
    
    answer = await call_deepseek(messages, model_config)
    
    if not answer:
        if is_maxim:
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
    
    answer = clean_response(answer, is_maxim)
    
    memory.add_message("user", f"{user_info.get_display_name()}: {user_message}")
    memory.add_message("assistant", answer)
    
    if len(user_message) > 10:
        user_info.add_topic(f"диалог: {user_message[:30]}...")
    
    return answer, memory

# ========== КОМАНДЫ TELEGRAM ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда /start"""
    try:
        user_info = await get_or_create_user_info(update)
        season, season_info = get_current_season()
        
        if user_info.is_maxim():
            greetings = [
                f"Привет, мой дорогой Максим! Я Лейла из {BOT_LOCATION['city']}а. Очень рада тебя видеть! {season_info.get('emoji', '✨')} 💖",
                f"Здравствуй, Максим! Я Лейла. Сейчас у нас в {BOT_LOCATION['city']}е прекрасная {season}. {season_info.get('emoji', '✨')} Как твои дела? 😊",
            ]
        else:
            greetings = [
                f"Привет, {user_info.get_display_name()}! Я Лейла. Рада познакомиться.",
                f"Здравствуйте, {user_info.get_display_name()}. Я Лейла, всегда готова помочь.",
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

# ========== ПЛАНОВЫЕ СООБЩЕНИЯ ==========

async def send_morning_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Утреннее сообщение Максиму"""
    logger.info("=== УТРЕННЕЕ СООБЩЕНИЕ МАКСИМУ ===")
    
    if not GROUP_CHAT_ID or not MAXIM_ID:
        logger.error("❌ Не заданы GROUP_CHAT_ID или MAXIM_ID")
        return
    
    try:
        if not client:
            return
        
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
2. Упомяни погоду и сезон
3. Добавь немного флирта и заботы
4. Пожелай хорошего дня
5. Используй 2-3 эмодзи
6. Сообщение должно быть развернутым (3-5 предложений)
7. Будь нежной и романтичной
"""
        
        messages = [
            {"role": "system", "content": "Ты — Лейла, нежная и романтичная девушка из Брисбена, которая очень любит Максима."},
            {"role": "user", "content": prompt}
        ]
        
        model_config = {
            "model": DEEPSEEK_MODELS["lite"],
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
            fallback = f"Доброе утро, мой дорогой Максим! {season_info.get('emoji', '☀️')} Пусть этот {season}ний день в Брисбене будет наполнен радостью и теплом! Я уже соскучилась по нашему общению... 💖 Как твои планы на сегодня?"
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=fallback)
            
    except Exception as e:
        logger.error(f"❌ Ошибка утреннего сообщения: {e}")

async def send_evening_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Вечернее сообщение Максиму"""
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
2. Добавь сезонный контекст
3. Будь нежной, заботливой и романтичной
4. Упомяни, что думаешь о нём
5. Используй 2-3 эмодзи
6. Сообщение должно быть развернутым (3-4 предложения)
"""
        
        messages = [
            {"role": "system", "content": "Ты — Лейла, нежная и заботливая девушка, которая очень скучает по Максиму."},
            {"role": "user", "content": prompt}
        ]
        
        model_config = {
            "model": DEEPSEEK_MODELS["lite"],
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
    """Friday tennis reminder with special effects"""
    logger.info("=== ПЯТНИЧНЫЙ ТЕННИСНЫЙ РЕМИНДЕР ===")
    
    if not GROUP_CHAT_ID:
        return
    
    try:
        # Get current season and time context
        season, season_info = get_current_season()
        tz = get_tz()
        now = datetime.now(tz)
        
        # Fun tennis-themed message with effects
        tennis_message = f"""🎾✨ *ПЯТНИЧНЫЙ ТЕННИСНЫЙ РЕМИНДЕР\!* ✨🎾

🕓 *Время:* 16:00 пятницы
📍 *Место:* Теннисные корты
🎯 *Доступ:* Код для входа:

`{TENNIS_ACCESS_CODE}`

📅 *Срок действия:* до {TENNIS_CODE_VALID_UNTIL}

🎾 *Особые инструкции:*
\- Не забывайте спортивную форму\!
\- Приходите с хорошим настроением
\- Код действителен до указанной даты

🏆 *Напутствие от Лейлы:*
Пусть каждый удар будет точным, а каждая партия принесёт радость и адреналин\! 
Помните: главное \- не победа, а удовольствие от игры и компании друзей\! 😊

_{season_info.get('emoji', '🎾')} Сейчас {season} в Брисбене \- идеальное время для спорта\!_

\#ТеннисПятница \#АктивныйОтдых \#КодДоступа"""
        
        # Alternative fun formats (randomly choose one)
        fun_formats = [
            # Format 1: With emoji art
            f"""🎾🎾🎾🎾🎾🎾🎾🎾🎾🎾🎾🎾🎾🎾🎾
           *ПЯТНИЧНЫЙ ТЕННИС\!*
🎾🎾🎾🎾🎾🎾🎾🎾🎾🎾🎾🎾🎾🎾🎾

📅 *Когда:* Сегодня 16:00
🔑 *Код доступа:* `{TENNIS_ACCESS_CODE}`
⏰ *Действует до:* {TENNIS_CODE_VALID_UNTIL}

💥 *Готовы к игре?*
\- Энергия: 100% ✅
\- Настроение: Отличное ✅
\- Форма: Спортивная ✅

✨ *Пожелание от Лейлы:*
Пусть мяч летит точно, смех звучит громко, 
а пятница станет самой спортивной в неделе\! 🥳

🏃‍♀️ *Бегите на корты\!* 🏃‍♂️

_{season_info.get('description', '')} в Брисбене \- что может быть лучше для тенниса\?_""",

            # Format 2: More structured
            f"""📋 *ЕЖЕНЕДЕЛЬНОЕ НАПОМИНАНИЕ О ТЕННИСЕ*

🎾 *Детали мероприятия:*
• День: Пятница
• Время: 16:00
• Что: Теннисные встречи
• Код: `{TENNIS_ACCESS_CODE}`
• Действует до: {TENNIS_CODE_VALID_UNTIL}

🔔 *Важно:*
Этот код обновляется только {TENNIS_CODE_VALID_UNTIL}\.
До этой даты используйте один и тот же код\.

🌟 *От Лейлы:*
"В пятницу мы играем в теннис, 
потому что жизнь \- это не только работа, 
но и радость движения\! 
Приходите, будет весело\! 😄"

\#ТеннисноеПятничко \#Код{TENNIS_ACCESS_CODE.replace('#', '')}""",

            # Format 3: Calendar style
            f"""🗓️ *КАЛЕНДАРЬ ТЕННИСА*

┌─────────────────────┐
│      ПЯТНИЦА       │
│    🎾 16:00 🎾    │
└─────────────────────┘

🔐 *КОД ДОСТУПА НА КОРТЫ:*
