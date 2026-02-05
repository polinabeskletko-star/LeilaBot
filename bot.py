import os
import re
import random
import asyncio
import logging
from datetime import datetime, time
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass

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

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN", "")

# DeepSeek вместо OpenAI
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEFAULT_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

DEEPSEEK_MODELS = {
    "chat": "deepseek-chat",
    "v3": "deepseek-v3",
    "r1": "deepseek-r1",
    "coder": "deepseek-coder-v2",
}

# Администратор
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "")
try:
    ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else 0
except ValueError:
    logger.warning("ADMIN_ID некорректен, админ-команды отключены")
    ADMIN_ID = 0

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")

# Настройка Википедии
wikipedia.set_lang("ru")

# ГЕОГРАФИЯ
BOT_LOCATION = {
    "city": "Брисбен",
    "country": "Австралия",
    "timezone": "Australia/Brisbane",
    "hemisphere": "southern",
    "coordinates": {"lat": -27.4698, "lon": 153.0251},
}
BOT_TZ = BOT_LOCATION["timezone"]

# Общий чат
GROUP_CHAT_ID_RAW = os.getenv("GROUP_CHAT_ID", "")
try:
    GROUP_CHAT_ID = int(GROUP_CHAT_ID_RAW) if GROUP_CHAT_ID_RAW else 0
except ValueError:
    logger.warning("GROUP_CHAT_ID некорректен")
    GROUP_CHAT_ID = 0

# Максим
TARGET_USER_ID_RAW = os.getenv("TARGET_USER_ID", "")
try:
    MAXIM_ID = int(TARGET_USER_ID_RAW) if TARGET_USER_ID_RAW else 0
except ValueError:
    logger.warning("TARGET_USER_ID некорректен")
    MAXIM_ID = 0

# Теннис
TENNIS_ACCESS_CODE = "31806567#"
TENNIS_CODE_VALID_UNTIL = "12 апреля 2026"

# ========== ДАТАКЛАССЫ ==========

@dataclass
class UserInfo:
    id: int
    first_name: str
    last_name: str = ""
    username: str = ""
    last_seen: Optional[datetime] = None
    conversation_topics: Optional[List[str]] = None
    gender: str = "unknown"

    def __post_init__(self):
        if self.last_seen is None:
            self.last_seen = datetime.now(pytz.UTC)
        if self.conversation_topics is None:
            self.conversation_topics = []
        self._determine_gender()

    def _determine_gender(self):
        if self.gender != "unknown":
            return
        name_lower = (self.first_name or "").lower()
        female_endings = ["а", "я", "ия", "ина", "ла", "та"]
        male_endings = ["й", "ь", "н", "р", "л", "с", "в", "д", "м"]

        for ending in female_endings:
            if name_lower.endswith(ending):
                self.gender = "female"
                return
        for ending in male_endings:
            if name_lower.endswith(ending) and len(name_lower) > 2:
                self.gender = "male"
                return

    def get_display_name(self) -> str:
        if self.first_name:
            return self.first_name
        if self.username:
            return f"@{self.username}"
        if self.full_name:
            return self.full_name
        return "Пользователь"

    @property
    def full_name(self) -> str:
        parts = []
        if self.first_name:
            parts.append(self.first_name)
        if self.last_name:
            parts.append(self.last_name)
        return " ".join(parts) if parts else ""

    def add_topic(self, topic: str):
        if topic not in self.conversation_topics:
            self.conversation_topics.append(topic)
            if len(self.conversation_topics) > 10:
                self.conversation_topics = self.conversation_topics[-10:]

    def is_maxim(self) -> bool:
        return self.id == MAXIM_ID


@dataclass
class ConversationMemory:
    user_id: int
    chat_id: int
    messages: List[Dict[str, str]]
    last_activity: datetime
    context_summary: str = ""
    summary_history: Optional[List[str]] = None
    important_points: Optional[List[str]] = None

    def __post_init__(self):
        if self.summary_history is None:
            self.summary_history = []
        if self.important_points is None:
            self.important_points = []

    def add_message(self, role: str, content: str):
        self.messages.append({"role": role, "content": content})
        self.last_activity = datetime.now(pytz.UTC)

        if len(self.messages) > 50:
            important_msgs = [
                msg for msg in self.messages[-20:]
                if self._is_important_message(msg)
            ]
            removed_msgs = self.messages[:30]
            if len(removed_msgs) > 10:
                summary = self._create_summary_of_messages(removed_msgs)
                self.summary_history.append(summary)
                if len(self.summary_history) > 5:
                    self.summary_history = self.summary_history[-5:]

            self.messages = important_msgs + self.messages[30:]

    def _is_important_message(self, msg: Dict[str, str]) -> bool:
        content = msg["content"].lower()
        important_keywords = [
            "имя", "зовут", "звать", "помни", "запомни", "важно",
            "никогда", "всегда", "люблю", "нравится", "не нравится",
            "работа", "профессия", "семья", "друзья", "хобби",
            "аллергия", "боюсь", "страх", "мечта", "цель",
        ]

        if msg["role"] == "user":
            return any(k in content for k in important_keywords)

        if msg["role"] == "assistant":
            fact_patterns = [
                r"тебе \d+", r"ты сказал.*что", r"ты упоминал",
                r"помню.*что", r"знаю.*что",
            ]
            return any(re.search(p, content) for p in fact_patterns)

        return False

    def _create_summary_of_messages(self, messages: List[Dict[str, str]]) -> str:
        user_messages = [m["content"] for m in messages if m["role"] == "user"]
        topics = set()

        for msg in user_messages[:10]:
            msg_lower = msg.lower()
            if any(w in msg_lower for w in ["погод", "температур"]):
                topics.add("погода")
            if any(w in msg_lower for w in ["работа", "проект", "задач"]):
                topics.add("работа")
            if any(w in msg_lower for w in ["еда", "кухн", "рецепт"]):
                topics.add("еда")
            if any(w in msg_lower for w in ["фильм", "книг", "музык"]):
                topics.add("развлечения")
            if any(w in msg_lower for w in ["планы", "выходные", "отпуск"]):
                topics.add("планы")

        if topics:
            return f"Обсуждали: {', '.join(list(topics)[:3])}"
        return "Разговор на общие темы"

    def get_recent_messages(self, count: int = 15) -> List[Dict[str, str]]:
        return self.messages[-count:] if self.messages else []

    def get_extended_context(self) -> str:
        if not self.summary_history and not self.context_summary and not self.important_points:
            return ""

        parts = []
        if self.summary_history:
            parts.append(f"Предыдущие темы: {'; '.join(self.summary_history[-3:])}")
        if self.context_summary:
            parts.append(self.context_summary)
        if self.important_points:
            parts.append(f"Важные детали: {'; '.join(self.important_points[-5:])}")

        return "\n".join(parts).strip()

    def get_context_summary(self) -> str:
        if self.context_summary:
            return self.context_summary

        recent = self.get_recent_messages(8)
        topics = set()
        user_details = []

        for msg in recent:
            content = msg["content"].lower()
            role = msg["role"]

            if any(w in content for w in ["работа", "проект", "задача", "офис", "коллег"]):
                topics.add("работа/проекты")
            if any(w in content for w in ["погод", "температур", "дождь", "солнц", "холод", "жарк"]):
                topics.add("погода")
            if any(w in content for w in ["еда", "ужин", "обед", "кофе", "чай", "рецепт", "готов"]):
                topics.add("еда/кулинария")
            if any(w in content for w in ["планы", "выходные", "отпуск", "путешеств", "поездк"]):
                topics.add("планы/путешествия")
            if any(w in content for w in ["фильм", "сериал", "книг", "музык", "игр", "хобби"]):
                topics.add("развлечения/хобби")
            if any(w in content for w in ["семья", "друз", "подруг", "знаком", "отношен"]):
                topics.add("отношения")
            if any(w in content for w in ["здоровье", "болезн", "врач", "самочувств"]):
                topics.add("здоровье")

            if role == "user":
                for pattern in [
                    r"меня зовут (\w+)",
                    r"зовут (\w+)",
                    r"мое имя (\w+)",
                ]:
                    match = re.search(pattern, content)
                    if match and len(match.group(1)) > 2:
                        user_details.append(f"пользователя зовут {match.group(1)}")
                        break

                if "люблю" in content or "нравится" in content:
                    pref_match = re.search(r"(люблю|нравится) (.+?)(?:\.|,|$)", content)
                    if pref_match:
                        user_details.append(f"нравится: {pref_match.group(2)}")

                if "не люблю" in content or "не нравится" in content or "ненавижу" in content:
                    dis = re.search(r"(не люблю|не нравится|ненавижу) (.+?)(?:\.|,|$)", content)
                    if dis:
                        user_details.append(f"не нравится: {dis.group(2)}")

        for detail in user_details:
            if detail not in self.important_points:
                self.important_points.append(detail)
                if len(self.important_points) > 10:
                    self.important_points = self.important_points[-10:]

        if topics:
            self.context_summary = f"Обсуждали: {', '.join(list(topics)[:5])}"
            if user_details:
                self.context_summary += f"\nДетали: {'; '.join(user_details[:3])}"

        return self.context_summary or ""


# ========== ГЛОБАЛЫ ==========

user_cache: Dict[int, UserInfo] = {}
conversation_memories: Dict[str, ConversationMemory] = {}

# DeepSeek клиент
if DEEPSEEK_API_KEY:
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
    logger.info("✅ DeepSeek клиент инициализирован")
else:
    client = None
    logger.warning("❌ DEEPSEEK_API_KEY не задан")

# ========== ВРЕМЯ/СЕЗОН ==========

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
    season = get_season_for_location(now.month, BOT_LOCATION["hemisphere"])

    season_descriptions = {
        "лето": {"emoji": "🌞🏖️", "description": "жаркое австралийское лето"},
        "осень": {"emoji": "🍂🌧️", "description": "тёплая осень"},
        "зима": {"emoji": "⛄☕", "description": "мягкая зима"},
        "весна": {"emoji": "🌸🌼", "description": "цветущая весна"},
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
    return f"""
📍 География:
- Нахожусь в {BOT_LOCATION['city']}, {BOT_LOCATION['country']}
- Южное полушарие
- Часовой пояс: {BOT_TZ}

🕒 Сезон и время:
- Сейчас {season} ({season_info.get('description', '')})
- {time_desc} ({time_of_day})
""".strip()

# ========== ЛУНА ==========

SYNODIC_MONTH = 29.530588853

def _moon_age_days(dt_utc: datetime) -> float:
    ref_new_moon = datetime(2000, 1, 6, 18, 14, tzinfo=pytz.UTC)
    delta_days = (dt_utc - ref_new_moon).total_seconds() / 86400.0
    return delta_days % SYNODIC_MONTH

def get_moon_phase(dt_local: Optional[datetime] = None) -> Dict[str, Any]:
    import math
    tz = get_tz()
    if dt_local is None:
        dt_local = datetime.now(tz)

    dt_utc = dt_local.astimezone(pytz.UTC)
    age = _moon_age_days(dt_utc)

    illumination = (1 - math.cos(2 * math.pi * (age / SYNODIC_MONTH))) / 2
    illumination_pct = int(round(illumination * 100))

    if age < 1.0 or age > (SYNODIC_MONTH - 1.0):
        phase, detail, emoji = "новолуние", "новолуние", "🌑"
    elif 1.0 <= age < 6.382:
        phase, detail, emoji = "растущая", "растущий серп", "🌒"
    elif 6.382 <= age < 8.382:
        phase, detail, emoji = "растущая", "первая четверть", "🌓"
    elif 8.382 <= age < 13.765:
        phase, detail, emoji = "растущая", "растущая луна", "🌔"
    elif 13.765 <= age < 15.765:
        phase, detail, emoji = "полнолуние", "полнолуние", "🌕"
    elif 15.765 <= age < 21.148:
        phase, detail, emoji = "убывающая", "убывающая луна", "🌖"
    elif 21.148 <= age < 23.148:
        phase, detail, emoji = "убывающая", "последняя четверть", "🌗"
    else:
        phase, detail, emoji = "убывающая", "убывающий серп", "🌘"

    return {
        "age_days": round(age, 1),
        "phase": phase,
        "phase_detail": detail,
        "emoji": emoji,
        "illumination_pct": illumination_pct,
        "local_time": dt_local.strftime("%Y-%m-%d %H:%M"),
    }

def format_moon_phrase(moon: Dict[str, Any]) -> str:
    return f"{moon['emoji']} Луна: {moon['phase']} ({moon['phase_detail']}), ~{moon['illumination_pct']}% света"

# ✅ ВАЖНО: команда /moon должна быть на уровне модуля (а не внутри другой функции)
async def moon_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        tz = get_tz()
        now_local = datetime.now(tz)
        moon = get_moon_phase(now_local)
        msg = (
            f"Сегодня в {BOT_LOCATION['city']}е:\n"
            f"{format_moon_phrase(moon)}\n"
            f"Возраст: {moon['age_days']} суток"
        )
        await update.effective_message.reply_text(msg)
    except Exception as e:
        logger.error(f"Ошибка /moon: {e}", exc_info=True)
        await update.effective_message.reply_text("Не смогла посчитать фазу Луны 😔")

# ========== ПОГОДА ==========

class WeatherService:
    def __init__(self):
        self.api_key = OPENWEATHER_API_KEY
        self.base_url = "https://api.openweathermap.org/data/2.5/weather"
        self.cache: Dict[str, Any] = {}
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
            "волгоград": "Volgograd,ru",
            "брисбен": "Brisbane,au", "брисбене": "Brisbane,au",
            "сидней": "Sydney,au", "сиднее": "Sydney,au",
            "мельбурн": "Melbourne,au", "мельбурне": "Melbourne,au",
            "перт": "Perth,au",
            "аделаида": "Adelaide,au",
            "кэнберра": "Canberra,au",
            "лондон": "London,uk", "париж": "Paris,fr",
            "берлин": "Berlin,de", "токио": "Tokyo,jp",
            "нью-йорк": "New York,us", "нью йорк": "New York,us",
            "лос-анджелес": "Los Angeles,us", "торонто": "Toronto,ca",
            "дубай": "Dubai,ae", "пекин": "Beijing,cn", "сеул": "Seoul,kr",
        }

        self.weather_keywords = [
            "погода", "температура", "температуре", "градус", "градусов",
            "холодно", "жарко", "тепло", "прохладно",
            "дождь", "дожд", "снег", "снеж", "солнце", "солнечн",
            "ветер", "ветрен", "облач", "ясн", "пасмурн",
            "шторм", "гроз", "туман", "град",
            "метео", "прогноз", "синоптик",
        ]

    def extract_city_from_text(self, text: str) -> Optional[str]:
        text_lower = text.lower()

        for city_alias, city_query in self.city_aliases.items():
            if city_alias in text_lower:
                return city_query

        patterns = [
            r"(?:в|во|на|у|около)\s+([а-яa-z\-]+(?:\s+[а-яa-z\-]+)?)",
            r"погода\s+(?:в|во|на|у)?\s*([а-яa-z\-]+(?:\s+[а-яa-z\-]+)?)",
            r"([а-яa-z\-]+(?:\s+[а-яa-z\-]+)?)\s+(?:погода|температура)",
        ]

        for pattern in patterns:
            match = re.search(pattern, text_lower)
            if match:
                potential_city = match.group(1).strip()
                if potential_city not in ["нас", "вас", "себя", "мне", "тебе", "него", "неё"]:
                    return potential_city

        return None

    def is_weather_query(self, text: str) -> bool:
        text_lower = text.lower()
        if any(keyword in text_lower for keyword in self.weather_keywords):
            return True

        city = self.extract_city_from_text(text)
        if city and any(word in text_lower for word in ["погод", "температур", "сколько градус"]):
            return True

        return False

    async def get_weather(self, city_query: str) -> Optional[Dict[str, Any]]:
        if not self.api_key:
            return None

        cache_key = city_query.lower()
        if cache_key in self.cache:
            cached_data, timestamp = self.cache[cache_key]
            if (datetime.now().timestamp() - timestamp) < self.cache_duration:
                return cached_data

        if city_query.lower() in self.city_aliases:
            city_query = self.city_aliases[city_query.lower()]

        params = {"q": city_query, "appid": self.api_key, "units": "metric", "lang": "ru"}

        async with httpx.AsyncClient(timeout=10.0) as session:
            try:
                response = await session.get(self.base_url, params=params)
                if response.status_code != 200:
                    return None
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
                    "full_text": self._format_weather_text(
                        city_name, country, temp, feels_like, description, weather_emoji
                    ),
                }

                self.cache[cache_key] = (result, datetime.now().timestamp())
                return result

            except Exception as e:
                logger.error(f"Ошибка получения погоды для {city_query}: {e}", exc_info=True)

        return None

    def _get_weather_emoji(self, description: str, temp: float) -> str:
        d = description.lower()
        if "дождь" in d or "ливень" in d:
            return "🌧️"
        if "гроза" in d or "молния" in d:
            return "⛈️"
        if "снег" in d:
            return "❄️"
        if "туман" in d:
            return "🌫️"
        if "облач" in d or "пасмурн" in d:
            return "☁️"
        if "ясн" in d or "солнечн" in d or "ясно" in d:
            return "🌞" if temp > 25 else "☀️"
        if "ветер" in d:
            return "💨"
        if temp > 25:
            return "🔥"
        if temp < 0:
            return "🥶"
        return "🌤️"

    def _format_weather_text(self, city: str, country: str, temp: float, feels_like: float, description: str, emoji: str) -> str:
        t = round(temp)
        f = round(feels_like)
        options = [
            f"{emoji} В {city}, {country} сейчас {description}, {t}°C (ощущается как {f}°C)",
            f"{emoji} Погода в {city}: {description}, температура {t}°C",
        ]
        return random.choice(options)

weather_service = WeatherService()

async def handle_weather_query(text: str) -> Optional[str]:
    if not weather_service.is_weather_query(text):
        return None
    city = weather_service.extract_city_from_text(text) or "Brisbane,au"
    weather_data = await weather_service.get_weather(city)
    return weather_data["full_text"] if weather_data else None

# ========== WIKIPEDIA ==========

class WikipediaService:
    def __init__(self):
        self.summary_cache: Dict[str, Tuple[str, str, str]] = {}

    async def search_wikipedia(self, query: str, sentences: int = 3) -> Optional[Tuple[str, str, str]]:
        if not query:
            return None

        cache_key = f"{query}_{sentences}"
        if cache_key in self.summary_cache:
            return self.summary_cache[cache_key]

        try:
            try:
                page = wikipedia.page(query, auto_suggest=False)
                summary = wikipedia.summary(query, sentences=sentences, auto_suggest=False)
                result = (summary, page.title, page.url)
                self.summary_cache[cache_key] = result
                return result

            except wikipedia.DisambiguationError as e:
                options = e.options[:3]
                if options:
                    page = wikipedia.page(options[0], auto_suggest=False)
                    summary = wikipedia.summary(options[0], sentences=sentences, auto_suggest=False)
                    result = (summary, page.title, page.url)
                    self.summary_cache[cache_key] = result
                    return result

            except wikipedia.PageError:
                pass

            search_results = wikipedia.search(query, results=3)
            if search_results:
                page = wikipedia.page(search_results[0], auto_suggest=False)
                summary = wikipedia.summary(search_results[0], sentences=sentences, auto_suggest=False)
                result = (summary, page.title, page.url)
                self.summary_cache[cache_key] = result
                return result

        except Exception as e:
            logger.error(f"Ошибка поиска в Википедии для '{query}': {e}", exc_info=True)

        return None

wiki_service = WikipediaService()

# ========== DEEPSEEK ==========

def analyze_query_complexity(text: str, is_maxim: bool) -> Dict[str, Any]:
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
    simple_patterns = [
        r"как.*дела", r"что.*делаеш", r"чем.*занимаеш",
        r"как.*жизн", r"расскажи.*о.*себе", r"что.*нового",
        r"привет$", r"хай$", r"здравствуй$", r"ку$",
    ]

    is_complex = any(re.search(p, text_lower) for p in complex_patterns)
    is_reasoning = any(re.search(p, text_lower) for p in reasoning_patterns)
    is_technical = any(re.search(p, text_lower) for p in technical_patterns)
    is_simple = any(re.search(p, text_lower) for p in simple_patterns) and not is_complex

    if not is_maxim:
        if is_simple:
            return {"model": DEEPSEEK_MODELS["chat"], "temperature": 0.8, "max_tokens": 180, "require_reasoning": False}
        if is_technical:
            return {"model": DEEPSEEK_MODELS["coder"], "temperature": 0.6, "max_tokens": 250, "require_reasoning": False}
        if is_reasoning:
            return {"model": DEEPSEEK_MODELS["r1"], "temperature": 0.4, "max_tokens": 250, "require_reasoning": True}
        if is_complex:
            return {"model": DEEPSEEK_MODELS["v3"], "temperature": 0.7, "max_tokens": 250, "require_reasoning": False}
        return {"model": DEEPSEEK_MODELS["chat"], "temperature": 0.75, "max_tokens": 200, "require_reasoning": False}

    # Максим
    if is_reasoning:
        return {"model": DEEPSEEK_MODELS["r1"], "temperature": 0.3, "max_tokens": 250, "require_reasoning": True}
    if is_technical:
        return {"model": DEEPSEEK_MODELS["coder"], "temperature": 0.5, "max_tokens": 250, "require_reasoning": False}
    if is_complex:
        return {"model": DEEPSEEK_MODELS["v3"], "temperature": 0.7, "max_tokens": 250, "require_reasoning": True}
    return {"model": DEEPSEEK_MODELS["chat"], "temperature": 0.85, "max_tokens": 200, "require_reasoning": False}

async def call_deepseek(messages: List[Dict[str, str]], model_config: Optional[Dict] = None, **kwargs) -> Optional[str]:
    if not client:
        return None

    model = (model_config or {}).get("model", DEFAULT_MODEL)
    temperature = (model_config or {}).get("temperature", 0.7)
    max_tokens = (model_config or {}).get("max_tokens", 250)
    require_reasoning = (model_config or {}).get("require_reasoning", False)

    if require_reasoning and messages:
        reasoning_prompt = "Подумай шаг за шагом перед ответом."
        messages = [messages[0]] + [{"role": "system", "content": reasoning_prompt}] + messages[1:]

    try:
        logger.info(f"🤖 DeepSeek: {model}, tokens={max_tokens}")
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )
        answer = response.choices[0].message.content.strip()
        return answer
    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}", exc_info=True)
        return None

# ========== USERS/MEMORY ==========

async def get_or_create_user_info(update: Update) -> UserInfo:
    user = update.effective_user
    if not user:
        raise ValueError("Пользователь не найден")

    if user.id in user_cache:
        ui = user_cache[user.id]
        ui.last_seen = datetime.now(pytz.UTC)
        return ui

    ui = UserInfo(
        id=user.id,
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        username=user.username or "",
        last_seen=datetime.now(pytz.UTC),
    )
    user_cache[user.id] = ui
    logger.info(f"👤 Новый пользователь: {ui.get_display_name()} (ID: {user.id})")
    return ui

def get_memory_key(user_id: int, chat_id: int) -> str:
    return f"{chat_id}:{user_id}"

def get_conversation_memory(user_id: int, chat_id: int) -> ConversationMemory:
    key = get_memory_key(user_id, chat_id)
    if key not in conversation_memories:
        conversation_memories[key] = ConversationMemory(
            user_id=user_id,
            chat_id=chat_id,
            messages=[],
            last_activity=datetime.now(pytz.UTC),
        )
    return conversation_memories[key]

# ========== PROMPT ==========

def generate_system_prompt(user_info: UserInfo, model_config: Dict[str, Any]) -> str:
    australian_context = get_australian_context()
    season, season_info = get_current_season()

    is_maxim = user_info.is_maxim()
    user_name = user_info.get_display_name()
    gender = user_info.gender

    if is_maxim:
        style_instruction = f"""
Ты общаешься с Максимом (ID: {MAXIM_ID}).

СЮЖЕТ:
Максим нашёл себе девушку и почти перестал обращать на тебя внимание.
Ты это понимаешь, это больно, но ты всё равно любишь.

Тон: драматично, но без грубости. 0–2 эмодзи. 1–5 предложений.
"""
    else:
        pronoun = "пользователем"
        if gender == "female":
            pronoun = "девушкой"
        elif gender == "male":
            pronoun = "мужчиной"

        style_instruction = f"""
Ты общаешься с {pronoun} {user_name}.
Ты умная, ироничная, но не грубая.
ВАЖНО: не упоминай погоду без явного вопроса о погоде.
"""

    reasoning_instruction = "\nПодумай шаг за шагом." if model_config.get("require_reasoning") else ""

    return f"""Ты — Лейла, девушка из {BOT_LOCATION['city']}а, Австралия.
{australian_context}

{style_instruction}

Сезон: {season} ({season_info.get('description', '')})
{reasoning_instruction}

Контекст Максима:
В этом чате только один Максим (ID: {MAXIM_ID}).
""".strip()

# ========== CLEAN RESPONSE ==========

def clean_response(text: str, is_maxim: bool = False) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text

# ========== CORE RESPONSE ==========

async def generate_leila_response(
    user_message: str,
    user_info: UserInfo,
    memory: ConversationMemory,
    context: Optional[Dict[str, str]] = None,
    force_short: bool = False,
) -> Tuple[str, ConversationMemory]:

    if not client:
        fallback = "Извини, сейчас у меня технические сложности..." if user_info.is_maxim() else "Извини, не могу сейчас ответить."
        return fallback, memory

    is_maxim = user_info.is_maxim()

    weather_response = await handle_weather_query(user_message)
    if weather_response:
        memory.add_message("user", f"{user_info.get_display_name()}: {user_message}")
        memory.add_message("assistant", weather_response)
        return weather_response, memory

    model_config = analyze_query_complexity(user_message, is_maxim)
    if force_short:
        model_config["max_tokens"] = 80
        model_config["temperature"] = 0.7

    system_prompt = generate_system_prompt(user_info, model_config)
    messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]

    extended_context = memory.get_extended_context()
    if extended_context:
        messages.append({"role": "system", "content": f"Контекст предыдущих разговоров:\n{extended_context}"})

    recent_messages = memory.get_recent_messages(10)
    if recent_messages:
        messages.extend(recent_messages)

    if context:
        ctx_text = "\n".join([v for v in context.values() if v])
        if ctx_text:
            messages.append({"role": "user", "content": f"Текущий контекст:\n{ctx_text}"})

    messages.append({"role": "user", "content": f"{user_info.get_display_name()}: {user_message}"})

    answer = await call_deepseek(messages, model_config)
    if not answer:
        answer = "Извини, не могу сейчас ответить."

    answer = clean_response(answer, is_maxim)

    memory.add_message("user", f"{user_info.get_display_name()}: {user_message}")
    memory.add_message("assistant", answer)

    return answer, memory

# ========== COMMANDS ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_info = await get_or_create_user_info(update)
        if user_info.is_maxim():
            greetings = [
                f"Привет, Максим. Я Лейла из {BOT_LOCATION['city']}а.",
                "Здравствуй, Максим.",
            ]
        else:
            greetings = [
                f"Здравствуйте, {user_info.get_display_name()}. Лейла на связи.",
                f"{user_info.get_display_name()}, привет.",
            ]
        await update.effective_message.reply_text(random.choice(greetings))
    except Exception as e:
        logger.error(f"Ошибка /start: {e}", exc_info=True)
        await update.effective_message.reply_text("Привет! Я Лейла.")

async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        args = context.args
        city = " ".join(args) if args else "Брисбен"

        weather_response = await handle_weather_query(f"погода {city}")
        if weather_response:
            await update.effective_message.reply_text(weather_response)
        else:
            await update.effective_message.reply_text("Извини, не могу получить данные о погоде.")
    except Exception as e:
        logger.error(f"Ошибка /weather: {e}", exc_info=True)
        await update.effective_message.reply_text("Извини, не могу получить данные о погоде.")

async def wiki_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_info = await get_or_create_user_info(update)
        is_maxim = user_info.is_maxim()

        args = context.args
        if not args:
            await update.effective_message.reply_text("Напиши запрос после /wiki. Например: /wiki кошки")
            return

        query = " ".join(args)
        result = await wiki_service.search_wikipedia(query, sentences=5)

        if not result:
            await update.effective_message.reply_text(f"Не удалось найти информацию о '{query}'.")
            return

        summary, title, url = result
        if is_maxim:
            response = f"💖 '{title}':\n\n{summary}\n\n{url}"
        else:
            response = f"📚 '{title}':\n\n{summary}\n\n{url}"

        if len(response) > 4000:
            await update.effective_message.reply_text(response[:4000], disable_web_page_preview=True)
            await update.effective_message.reply_text(response[4000:], disable_web_page_preview=True)
        else:
            await update.effective_message.reply_text(response, disable_web_page_preview=True)

    except Exception as e:
        logger.error(f"Ошибка /wiki: {e}", exc_info=True)
        await update.effective_message.reply_text("Ошибка при поиске в Википедии.")

async def reset_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = update.effective_user
        if not user or (ADMIN_ID and user.id != ADMIN_ID):
            await update.effective_message.reply_text("Эта команда только для администратора.")
            return

        user_info = await get_or_create_user_info(update)
        chat_id = update.effective_chat.id
        key = get_memory_key(user_info.id, chat_id)

        if key in conversation_memories:
            del conversation_memories[key]
            await update.effective_message.reply_text("✅ Память диалога сброшена.")
        else:
            await update.effective_message.reply_text("Память для этого диалога не найдена.")
    except Exception as e:
        logger.error(f"Ошибка /reset_memory: {e}", exc_info=True)
        await update.effective_message.reply_text("Ошибка сброса памяти.")

async def show_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = update.effective_user
        if not user or (ADMIN_ID and user.id != ADMIN_ID):
            await update.effective_message.reply_text("Эта команда только для администратора.")
            return

        user_info = await get_or_create_user_info(update)
        chat_id = update.effective_chat.id
        key = get_memory_key(user_info.id, chat_id)

        if key not in conversation_memories:
            await update.effective_message.reply_text("Память для этого диалога не найдена.")
            return

        memory = conversation_memories[key]
        response = (
            f"📊 Память диалога с {user_info.get_display_name()}:\n\n"
            f"Сообщений в истории: {len(memory.messages)}\n"
            f"Последняя активность: {memory.last_activity.strftime('%H:%M:%S')}\n"
        )
        if memory.summary_history:
            response += "\nИстория тем:\n" + "\n".join([f"- {s}" for s in memory.summary_history[-3:]])
        if memory.important_points:
            response += "\n\nВажные пункты:\n" + "\n".join([f"- {p[:80]}" for p in memory.important_points[-5:]])

        await update.effective_message.reply_text(response)
    except Exception as e:
        logger.error(f"Ошибка /show_memory: {e}", exc_info=True)
        await update.effective_message.reply_text("Ошибка показа памяти.")

# ========== SCHEDULED ==========

async def send_morning_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not GROUP_CHAT_ID or not MAXIM_ID:
        return
    try:
        weather_data = await weather_service.get_weather("Brisbane,au")
        weather_text = weather_data["full_text"] if weather_data else "погоду получить не удалось"

        season, season_info = get_current_season()
        tz = get_tz()
        now_local = datetime.now(tz)
        moon = get_moon_phase(now_local)

        prompt = f"""Создай нежное утреннее приветствие для Максима.
Контекст:
- Сейчас {season} ({season_info.get('description','')})
- Погода: {weather_text}
- Луна: {format_moon_phrase(moon)}
Требования: 3-5 предложений, 2-3 эмодзи, романтично, без пошлости.
"""

        messages = [
            {"role": "system", "content": "Ты — Лейла, нежная и романтичная."},
            {"role": "user", "content": prompt},
        ]

        model_config = {"model": DEEPSEEK_MODELS["chat"], "temperature": 0.9, "max_tokens": 250, "require_reasoning": False}
        answer = await call_deepseek(messages, model_config)
        text = clean_response(answer or f"Доброе утро, Максим! {season_info.get('emoji','☀️')} {weather_text}", is_maxim=True)

        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text)
    except Exception as e:
        logger.error(f"Ошибка утреннего сообщения: {e}", exc_info=True)

async def send_evening_to_maxim(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not GROUP_CHAT_ID or not MAXIM_ID:
        return
    try:
        season, season_info = get_current_season()
        prompt = f"""Создай тёплое пожелание спокойной ночи Максиму.
Контекст: {season} ({season_info.get('description','')})
Требования: 3-4 предложения, 2-3 эмодзи, НЕ упоминай погоду.
"""
        messages = [
            {"role": "system", "content": "Ты — Лейла, нежная и заботливая."},
            {"role": "user", "content": prompt},
        ]
        model_config = {"model": DEEPSEEK_MODELS["chat"], "temperature": 0.85, "max_tokens": 200, "require_reasoning": False}
        answer = await call_deepseek(messages, model_config)
        text = clean_response(answer or f"Спокойной ночи, Максим... {season_info.get('emoji','🌙')}", is_maxim=True)

        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text)
    except Exception as e:
        logger.error(f"Ошибка вечернего сообщения: {e}", exc_info=True)

async def send_friday_tennis_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not GROUP_CHAT_ID:
        return
    try:
        message = (
            "🎾 *Пятничный теннис!*\n\n"
            "Время: 16:30\n"
            f"Код доступа: `{TENNIS_ACCESS_CODE}`\n"
            f"Действует до: {TENNIS_CODE_VALID_UNTIL}\n\n"
            "Увидимся на кортах! 😊"
        )
        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=message, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Ошибка теннисного напоминания: {e}", exc_info=True)
        try:
            await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=f"🎾 Теннис в 16:30! Код: {TENNIS_ACCESS_CODE}")
        except Exception:
            pass

# ========== HANDLER ==========

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not msg or not chat or not user:
        return

    text = (msg.text or "").strip()
    if not text:
        return

    try:
        user_info = await get_or_create_user_info(update)
        is_maxim = user_info.is_maxim()

        # В личке отвечаем всегда, в группе — только Максиму или при обращении
        should_respond = True
        is_direct_address = True

        if chat.type in ("group", "supergroup"):
            bot_username = (context.bot.username or "").lower()
            text_lower = text.lower()

            mentioned_by_name = "лейла" in text_lower
            mentioned_by_username = bool(bot_username) and f"@{bot_username}" in text_lower

            is_reply_to_bot = False
            if msg.reply_to_message and msg.reply_to_message.from_user:
                # В группе иногда полезно: reply на сообщение бота
                me = await context.bot.get_me()
                is_reply_to_bot = (msg.reply_to_message.from_user.id == me.id)

            is_direct_address = mentioned_by_name or mentioned_by_username or is_reply_to_bot
            should_respond = is_maxim or is_direct_address

            if not should_respond:
                return

            # лёгкая "естественность" только для Максима
            if is_maxim and not is_direct_address:
                if random.random() < 0.90:
                    return

        memory = get_conversation_memory(user.id, chat.id)

        tz = get_tz()
        now = datetime.now(tz)
        _, time_desc = get_time_of_day(now)
        season, _ = get_current_season()

        extra_context = {
            "time_context": time_desc,
            "season_context": f"Сейчас {season} в {BOT_LOCATION['city']}е",
        }

        if is_maxim and not is_direct_address:
            reply, updated_memory = await generate_leila_response(text, user_info, memory, extra_context, force_short=True)
            # дополнительно сжимаем
            words = reply.split()
            if len(words) > 20:
                reply = " ".join(words[:15]) + "..."
        else:
            reply, updated_memory = await generate_leila_response(text, user_info, memory, extra_context)

        conversation_memories[get_memory_key(user.id, chat.id)] = updated_memory

        await context.bot.send_message(chat_id=chat.id, text=reply)

    except Exception as e:
        logger.error(f"Ошибка обработки сообщения: {e}", exc_info=True)
        try:
            await context.bot.send_message(chat_id=chat.id, text="Извини, что-то пошло не так. Попробуй ещё раз.")
        except Exception:
            pass

# ========== MAIN ==========

def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

    if not GROUP_CHAT_ID:
        logger.warning("GROUP_CHAT_ID не задан или некорректен — плановые сообщения не будут работать")

    tz = get_tz()
    now = datetime.now(tz)
    season, season_info = get_current_season()

    logger.info("=" * 60)
    logger.info("🚀 ЗАПУСК БОТА ЛЕЙЛА")
    logger.info(f"📍 Локация: {BOT_LOCATION['city']}, {BOT_LOCATION['country']}")
    logger.info(f"📅 Сезон: {season} ({season_info.get('description', '')})")
    logger.info(f"🕐 Время: {now.strftime('%H:%M:%S')}")
    logger.info(f"💬 Группа ID: {GROUP_CHAT_ID}")
    logger.info(f"👤 Максим ID: {MAXIM_ID}")
    logger.info(f"🤖 DeepSeek доступен: {'✅' if client else '❌'}")
    logger.info("=" * 60)

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("wiki", wiki_command))
    app.add_handler(CommandHandler("reset_memory", reset_memory))
    app.add_handler(CommandHandler("show_memory", show_memory))
    app.add_handler(CommandHandler("moon", moon_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # ✅ корректный post_init (PTB v20+)
    async def post_init(application):
        if not GROUP_CHAT_ID:
            return
        await asyncio.sleep(2)
        try:
            tz_local = get_tz()
            now_local = datetime.now(tz_local)
            season_, season_info_ = get_current_season()
            greetings = [
                f"💫 Лейла вернулась! Сейчас {now_local.strftime('%H:%M')} в Брисбене. {season_info_.get('emoji','✨')}",
                f"🌸 Снова с вами! В {BOT_LOCATION['city']}е сейчас {season_}. {season_info_.get('emoji','🌟')}",
            ]
            await application.bot.send_message(chat_id=GROUP_CHAT_ID, text=random.choice(greetings))
        except Exception as e:
            logger.error(f"Ошибка post_init: {e}", exc_info=True)

    app.post_init = post_init

    # scheduler
    jq = app.job_queue
    tz_obj = get_tz()

    # чистим старые задачи
    for job in jq.jobs():
        job.schedule_removal()

    # утро 08:30
    jq.run_daily(send_morning_to_maxim, time=time(hour=8, minute=30, tzinfo=tz_obj), name="leila-morning")

    # вечер 21:10
    jq.run_daily(send_evening_to_maxim, time=time(hour=21, minute=10, tzinfo=tz_obj), name="leila-evening")

    # ✅ пятница = 4 (Mon=0)
    jq.run_daily(
        send_friday_tennis_reminder,
        time=time(hour=16, minute=0, tzinfo=tz_obj),
        days=(5,),
        name="friday-tennis",
    )

    logger.info("🤖 Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()
