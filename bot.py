import os
import re
import json
import random
import sqlite3
import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass

import pytz
import httpx
import wikipedia
from openai import OpenAI

from telegram import Update
from telegram.constants import ChatAction
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

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN", "")

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEFAULT_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

DEEPSEEK_MODELS = {
    "chat": "deepseek-chat",
    "v3": "deepseek-chat",
    "r1": "deepseek-reasoner",
    "coder": "deepseek-chat",
}

ADMIN_ID_RAW = os.getenv("ADMIN_ID", "")
try:
    ADMIN_ID = int(ADMIN_ID_RAW) if ADMIN_ID_RAW else 0
except ValueError:
    logger.warning("ADMIN_ID некорректен, админ-команды отключены")
    ADMIN_ID = 0

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")

wikipedia.set_lang("ru")

BOT_LOCATION = {
    "city": "Брисбен",
    "country": "Австралия",
    "timezone": "Australia/Brisbane",
    "hemisphere": "southern",
    "coordinates": {"lat": -27.4698, "lon": 153.0251},
}

BOT_TZ = BOT_LOCATION["timezone"]

GROUP_CHAT_ID_RAW = os.getenv("GROUP_CHAT_ID", "")
try:
    GROUP_CHAT_ID = int(GROUP_CHAT_ID_RAW) if GROUP_CHAT_ID_RAW else 0
except ValueError:
    logger.warning("GROUP_CHAT_ID некорректен")
    GROUP_CHAT_ID = 0

TARGET_USER_ID_RAW = os.getenv("TARGET_USER_ID", "")
try:
    MAXIM_ID = int(TARGET_USER_ID_RAW) if TARGET_USER_ID_RAW else 0
except ValueError:
    logger.warning("TARGET_USER_ID некорректен")
    MAXIM_ID = 0

TENNIS_ACCESS_CODE = "30816515#"
TENNIS_CODE_VALID_UNTIL = "12 июля 2026"

DB_PATH = os.getenv("LEILA_DB_PATH", "leila_memory.sqlite3")

RANDOM_GROUP_REPLY_RATE = float(os.getenv("RANDOM_GROUP_REPLY_RATE", "0.15"))
MAXIM_JOKE_RATE = float(os.getenv("MAXIM_JOKE_RATE", "0.12"))

LEILA_MOODS = [
    "обычное",
    "саркастичное",
    "сонное",
    "усталое",
    "спокойное",
    "ироничное",
]

MOON_MOOD_COMMENTS = {
    "новолуние": [
        "Хороший день, чтобы начать что-то новое. Или хотя бы сделать вид.",
        "Энергии может быть мало, зато поводов драматизировать — достаточно.",
        "Сегодня лучше не требовать от людей чудес. Особенно до кофе.",
    ],
    "растущая": [
        "День подходит для планов, роста и красивых обещаний самому себе.",
        "Можно начинать дела. Даже те, которые потом героически бросите.",
        "Энергия растёт. Главное — не потратить её на спор в интернете.",
    ],
    "полнолуние": [
        "Сегодня люди могут быть особенно странными. Но не всё надо списывать на Луну.",
        "Эмоции могут быть громче обычного. Берегите себя и чужие нервы.",
        "Если кто-то сегодня слишком уверен в своей правоте — дышим глубже.",
    ],
    "убывающая": [
        "Хороший день, чтобы завершать старое и отпускать лишнее.",
        "Можно разгрести хвосты. Хотя бы морально.",
        "Энергия идёт на спад, так что героизм сегодня не обязателен.",
    ],
}
CURRENT_LEILA_STATE = {
    "mood": "обычное",
    "energy": 0.8,
    "last_mood_change": datetime.now(pytz.UTC),
}

LEILA_STATE_VARIANTS = [
    {"mood": "саркастичное", "energy": 0.9},
    {"mood": "сонное", "energy": 0.25},
    {"mood": "уставшее", "energy": 0.35},
    {"mood": "ироничное", "energy": 0.75},
    {"mood": "ленивое", "energy": 0.2},
    {"mood": "разговорчивое", "energy": 1.0},
]

MICRO_REPLIES = [
    "мда",
    "ну бывает",
    "это сильно",
    "ладно",
    "интересно конечно",
    "я пока это переварю",
    "вопросов стало больше",
    "не хочу об этом думать",
    "ну да. естественно",
]

SPONTANEOUS_MESSAGES = [
    "Иногда мне кажется, что этот чат держится на сарказме и случайности.",
    "Напоминаю: не каждый спор в интернете стоит вашего давления.",
    "Я всё ещё считаю, что людям нельзя давать доступ в интернет до кофе.",
    "Иногда лучший жизненный план — лечь спать.",
    "Сегодня уровень взрослости у человечества опять под вопросом.",
]

def maybe_change_leila_state():

    now = datetime.now(pytz.UTC)

    elapsed = (
        now - CURRENT_LEILA_STATE["last_mood_change"]
    ).total_seconds()

    if elapsed > random.randint(7200, 20000):

        new_state = random.choice(LEILA_STATE_VARIANTS)

        CURRENT_LEILA_STATE["mood"] = new_state["mood"]
        CURRENT_LEILA_STATE["energy"] = new_state["energy"]
        CURRENT_LEILA_STATE["last_mood_change"] = now


# ========== SQLite MEMORY ==========

class MemoryStore:
    def __init__(self, path: str):
        self.path = path
        self._init_db()

    def _connect(self):
    return sqlite3.connect(
        self.path,
        check_same_thread=False,
        timeout=30,
    )

    def _init_db(self):
        with self._connect() as conn:
            cur = conn.cursor()

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    first_name TEXT,
                    last_name TEXT,
                    username TEXT,
                    gender TEXT,
                    first_seen TEXT,
                    last_seen TEXT,
                    message_count INTEGER DEFAULT 0,
                    facts_json TEXT DEFAULT '[]',
                    topics_json TEXT DEFAULT '[]'
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS chat_memory (
                    chat_id INTEGER PRIMARY KEY,
                    last_activity TEXT,
                    message_count INTEGER DEFAULT 0,
                    recent_messages_json TEXT DEFAULT '[]',
                    summary TEXT DEFAULT '',
                    inside_jokes_json TEXT DEFAULT '[]'
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    user_id INTEGER,
                    role TEXT,
                    name TEXT,
                    content TEXT,
                    created_at TEXT
                )
            """)

            conn.commit()

    def upsert_user(self, user_info: "UserInfo"):
        now = datetime.now(pytz.UTC).isoformat()

        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT user_id, message_count FROM users WHERE user_id = ?", (user_info.id,))
            existing = cur.fetchone()

            if existing:
                cur.execute("""
                    UPDATE users
                    SET first_name = ?,
                        last_name = ?,
                        username = ?,
                        gender = ?,
                        last_seen = ?
                    WHERE user_id = ?
                """, (
                    user_info.first_name,
                    user_info.last_name,
                    user_info.username,
                    user_info.gender,
                    now,
                    user_info.id,
                ))
            else:
                cur.execute("""
                    INSERT INTO users (
                        user_id, first_name, last_name, username, gender,
                        first_seen, last_seen, message_count,
                        facts_json, topics_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, '[]', '[]')
                """, (
                    user_info.id,
                    user_info.first_name,
                    user_info.last_name,
                    user_info.username,
                    user_info.gender,
                    now,
                    now,
                ))

            conn.commit()

    def increment_user_message(self, user_id: int):
        with self._connect() as conn:
            conn.execute("""
                UPDATE users
                SET message_count = message_count + 1,
                    last_seen = ?
                WHERE user_id = ?
            """, (datetime.now(pytz.UTC).isoformat(), user_id))
            conn.commit()

    def add_user_fact(self, user_id: int, fact: str):
        fact = fact.strip()
        if not fact:
            return

        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT facts_json FROM users WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            facts = json.loads(row[0]) if row and row[0] else []

            if fact not in facts:
                facts.append(fact)
                facts = facts[-20:]

            cur.execute(
                "UPDATE users SET facts_json = ? WHERE user_id = ?",
                (json.dumps(facts, ensure_ascii=False), user_id),
            )
            conn.commit()

    def add_user_topic(self, user_id: int, topic: str):
        topic = topic.strip()
        if not topic:
            return

        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT topics_json FROM users WHERE user_id = ?", (user_id,))
            row = cur.fetchone()
            topics = json.loads(row[0]) if row and row[0] else []

            if topic not in topics:
                topics.append(topic)
                topics = topics[-20:]

            cur.execute(
                "UPDATE users SET topics_json = ? WHERE user_id = ?",
                (json.dumps(topics, ensure_ascii=False), user_id),
            )
            conn.commit()

    def add_message(self, chat_id: int, user_id: int, role: str, name: str, content: str):
        now = datetime.now(pytz.UTC).isoformat()

        with self._connect() as conn:
            cur = conn.cursor()

            cur.execute("""
                INSERT INTO messages (chat_id, user_id, role, name, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (chat_id, user_id, role, name, content, now))

            cur.execute("SELECT recent_messages_json FROM chat_memory WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()
            recent = json.loads(row[0]) if row and row[0] else []

            recent.append({
                "role": role,
                "name": name,
                "content": content,
                "created_at": now,
            })
            recent = recent[-50:]

            if row:
                cur.execute("""
                    UPDATE chat_memory
                    SET last_activity = ?,
                        message_count = message_count + 1,
                        recent_messages_json = ?
                    WHERE chat_id = ?
                """, (now, json.dumps(recent, ensure_ascii=False), chat_id))
            else:
                cur.execute("""
                    INSERT INTO chat_memory (
                        chat_id, last_activity, message_count,
                        recent_messages_json, summary, inside_jokes_json
                    )
                    VALUES (?, ?, 1, ?, '', '[]')
                """, (chat_id, now, json.dumps(recent, ensure_ascii=False)))

            conn.commit()

    def get_user_profile_text(self, user_id: int) -> str:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT first_name, last_name, username, gender, message_count, facts_json, topics_json
                FROM users
                WHERE user_id = ?
            """, (user_id,))
            row = cur.fetchone()

        if not row:
            return ""

        first_name, last_name, username, gender, message_count, facts_json, topics_json = row
        facts = json.loads(facts_json or "[]")
        topics = json.loads(topics_json or "[]")

        parts = [
            f"Пользователь: {' '.join([x for x in [first_name, last_name] if x]).strip() or username or user_id}",
            f"Сообщений: {message_count}",
        ]

        if topics:
            parts.append(f"Темы, которые часто всплывали: {', '.join(topics[-8:])}")

        if facts:
            parts.append(f"Запомненные детали: {'; '.join(facts[-8:])}")

        return "\n".join(parts)

    def get_chat_context_text(self, chat_id: int) -> str:
        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT message_count, recent_messages_json, summary, inside_jokes_json
                FROM chat_memory
                WHERE chat_id = ?
            """, (chat_id,))
            row = cur.fetchone()

        if not row:
            return ""

        message_count, recent_json, summary, jokes_json = row
        recent = json.loads(recent_json or "[]")
        jokes = json.loads(jokes_json or "[]")

        last_lines = []
        for msg in recent[-12:]:
            name = msg.get("name", "Кто-то")
            content = msg.get("content", "")
            role = msg.get("role", "user")
            if role == "user":
                last_lines.append(f"{name}: {content}")
            else:
                last_lines.append(f"Лейла: {content}")

        parts = [f"В этом чате накоплено сообщений: {message_count}"]

        if summary:
            parts.append(f"Краткая память чата: {summary}")

        if jokes:
            parts.append(f"Локальные мемы: {'; '.join(jokes[-8:])}")

        if last_lines:
            parts.append("Недавний контекст:\n" + "\n".join(last_lines))

        return "\n\n".join(parts)

    def add_inside_joke(self, chat_id: int, joke: str):
        joke = joke.strip()
        if not joke:
            return

        with self._connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT inside_jokes_json FROM chat_memory WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()
            jokes = json.loads(row[0]) if row and row[0] else []

            if joke not in jokes:
                jokes.append(joke)
                jokes = jokes[-20:]

            if row:
                cur.execute(
                    "UPDATE chat_memory SET inside_jokes_json = ? WHERE chat_id = ?",
                    (json.dumps(jokes, ensure_ascii=False), chat_id),
                )
            else:
                cur.execute("""
                    INSERT INTO chat_memory (
                        chat_id, last_activity, message_count, recent_messages_json,
                        summary, inside_jokes_json
                    )
                    VALUES (?, ?, 0, '[]', '', ?)
                """, (
                    chat_id,
                    datetime.now(pytz.UTC).isoformat(),
                    json.dumps(jokes, ensure_ascii=False),
                ))

            conn.commit()

    def reset_chat_memory(self, chat_id: int):
        with self._connect() as conn:
            conn.execute("DELETE FROM chat_memory WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
            conn.commit()

    def get_memory_stats(self, chat_id: int) -> str:
        with self._connect() as conn:
            cur = conn.cursor()

            cur.execute("SELECT COUNT(*) FROM users")
            users_count = cur.fetchone()[0]

            cur.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (chat_id,))
            messages_count = cur.fetchone()[0]

            cur.execute("SELECT inside_jokes_json FROM chat_memory WHERE chat_id = ?", (chat_id,))
            row = cur.fetchone()
            jokes = json.loads(row[0]) if row and row[0] else []

        return (
            f"👥 Пользователей в памяти: {users_count}\n"
            f"💬 Сообщений этого чата в базе: {messages_count}\n"
            f"🧠 Локальных мемов: {len(jokes)}"
        )


memory_store = MemoryStore(DB_PATH)

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
            self.messages = self.messages[-30:]

    def get_recent_messages(self, count: int = 15) -> List[Dict[str, str]]:
        return self.messages[-count:] if self.messages else []


# ========== ГЛОБАЛЫ ==========

user_cache: Dict[int, UserInfo] = {}
conversation_memories: Dict[str, ConversationMemory] = {}

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
        if month in [3, 4, 5]:
            return "осень"
        if month in [6, 7, 8]:
            return "зима"
        return "весна"

    if month in [12, 1, 2]:
        return "зима"
    if month in [3, 4, 5]:
        return "весна"
    if month in [6, 7, 8]:
        return "лето"
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
    if 9 <= hour < 12:
        return "утро", "☀️ Утро в разгаре"
    if 12 <= hour < 14:
        return "полдень", "🌞 Полдень, время обеда"
    if 14 <= hour < 17:
        return "день", "😊 День продолжается"
    if 17 <= hour < 20:
        return "вечер", "🌇 Вечер, время отдыха"
    if 20 <= hour < 23:
        return "поздний вечер", "🌃 Поздний вечер"

    return "ночь", "🌌 Ночь, время тишины"


def get_australian_context() -> str:
    tz = get_tz()
    now = datetime.now(tz)
    season, season_info = get_current_season()
    time_of_day, time_desc = get_time_of_day(now)

    return f"""
📍 География:
- Лейла живёт в {BOT_LOCATION['city']}, {BOT_LOCATION['country']}
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
    return (
        f"{moon['emoji']} Луна: {moon['phase']} "
        f"({moon['phase_detail']}), ~{moon['illumination_pct']}% света"
    )


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
            "брисбен": "Brisbane,au",
            "брисбене": "Brisbane,au",
            "сидней": "Sydney,au",
            "сиднее": "Sydney,au",
            "мельбурн": "Melbourne,au",
            "мельбурне": "Melbourne,au",
            "перт": "Perth,au",
            "аделаида": "Adelaide,au",
            "кэнберра": "Canberra,au",
            "лондон": "London,uk",
            "париж": "Paris,fr",
            "берлин": "Berlin,de",
            "токио": "Tokyo,jp",
            "нью-йорк": "New York,us",
            "нью йорк": "New York,us",
            "лос-анджелес": "Los Angeles,us",
            "торонто": "Toronto,ca",
            "дубай": "Dubai,ae",
            "пекин": "Beijing,cn",
            "сеул": "Seoul,kr",
        }

        self.weather_keywords = [
            "погода",
            "температура",
            "температуре",
            "градус",
            "градусов",
            "холодно",
            "жарко",
            "тепло",
            "прохладно",
            "дождь",
            "дожд",
            "снег",
            "снеж",
            "солнце",
            "солнечн",
            "ветер",
            "ветрен",
            "облач",
            "ясн",
            "пасмурн",
            "шторм",
            "гроз",
            "туман",
            "град",
            "метео",
            "прогноз",
            "синоптик",
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

        params = {
            "q": city_query,
            "appid": self.api_key,
            "units": "metric",
            "lang": "ru",
        }

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
                        city_name,
                        country,
                        temp,
                        feels_like,
                        description,
                        weather_emoji,
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

    def _format_weather_text(
        self,
        city: str,
        country: str,
        temp: float,
        feels_like: float,
        description: str,
        emoji: str,
    ) -> str:
        t = round(temp)
        f = round(feels_like)

        options = [
            f"{emoji} В {city}, {country} сейчас {description}, {t}°C. Ощущается как {f}°C.",
            f"{emoji} Погода в {city}: {description}, {t}°C. Вполне терпимо, если не драматизировать.",
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

    async def search_wikipedia(
        self,
        query: str,
        sentences: int = 3,
    ) -> Optional[Tuple[str, str, str]]:
        if not query:
            return None

        cache_key = f"{query}_{sentences}"

        if cache_key in self.summary_cache:
            return self.summary_cache[cache_key]

        try:
            result = await asyncio.to_thread(self._search_sync, query, sentences)

            if result:
                self.summary_cache[cache_key] = result

            return result

        except Exception as e:
            logger.error(f"Ошибка поиска в Википедии для '{query}': {e}", exc_info=True)

        return None

    def _search_sync(self, query: str, sentences: int) -> Optional[Tuple[str, str, str]]:
        try:
            page = wikipedia.page(query, auto_suggest=False)
            summary = wikipedia.summary(query, sentences=sentences, auto_suggest=False)
            return summary, page.title, page.url

        except wikipedia.DisambiguationError as e:
            options = e.options[:3]

            if options:
                page = wikipedia.page(options[0], auto_suggest=False)
                summary = wikipedia.summary(options[0], sentences=sentences, auto_suggest=False)
                return summary, page.title, page.url

        except wikipedia.PageError:
            pass

        search_results = wikipedia.search(query, results=3)

        if search_results:
            page = wikipedia.page(search_results[0], auto_suggest=False)
            summary = wikipedia.summary(search_results[0], sentences=sentences, auto_suggest=False)
            return summary, page.title, page.url

        return None


wiki_service = WikipediaService()


# ========== DEEPSEEK ==========

def analyze_query_complexity(text: str) -> Dict[str, Any]:
    text_lower = text.lower()

    complex_patterns = [
        r"объясни.*почему",
        r"сравни.*и",
        r"проанализируй",
        r"какой.*лучше",
        r"посоветуй.*как",
        r"реши.*задачу",
        r"что.*думаешь.*о",
        r"как.*относишься.*к",
    ]

    reasoning_patterns = [
        r"почему.*так",
        r"в чём.*причина",
        r"какова.*причина",
        r"как.*это.*работает",
        r"объясни.*принцип",
        r"логика.*в.*том",
        r"следует.*ли",
        r"должен.*ли",
    ]

    technical_patterns = [
        r"код",
        r"программир",
        r"алгоритм",
        r"функци",
        r"переменн",
        r"база.*данных",
        r"api",
        r"сервер",
        r"telegram.*бот",
        r"python",
    ]

    simple_patterns = [
        r"как.*дела",
        r"что.*делаеш",
        r"чем.*занимаеш",
        r"как.*жизн",
        r"что.*нового",
        r"привет$",
        r"хай$",
        r"здравствуй$",
        r"ку$",
    ]

    is_complex = any(re.search(p, text_lower) for p in complex_patterns)
    is_reasoning = any(re.search(p, text_lower) for p in reasoning_patterns)
    is_technical = any(re.search(p, text_lower) for p in technical_patterns)
    is_simple = any(re.search(p, text_lower) for p in simple_patterns) and not is_complex

    if is_simple:
        return {
            "model": DEEPSEEK_MODELS["chat"],
            "temperature": 0.9,
            "max_tokens": 120,
            "require_reasoning": False,
        }

    if is_technical:
        return {
            "model": DEEPSEEK_MODELS["coder"],
            "temperature": 0.45,
            "max_tokens": 400,
            "require_reasoning": False,
        }

    if is_reasoning:
        return {
            "model": DEEPSEEK_MODELS["r1"],
            "temperature": 0.35,
            "max_tokens": 350,
            "require_reasoning": True,
        }

    if is_complex:
        return {
            "model": DEEPSEEK_MODELS["v3"],
            "temperature": 0.7,
            "max_tokens": 300,
            "require_reasoning": False,
        }

    return {
        "model": DEEPSEEK_MODELS["chat"],
        "temperature": 0.8,
        "max_tokens": 220,
        "require_reasoning": False,
    }


async def call_deepseek(
    messages: List[Dict[str, str]],
    model_config: Optional[Dict] = None,
    **kwargs,
) -> Optional[str]:
    if not client:
        return None

    model = (model_config or {}).get("model", DEFAULT_MODEL)
    temperature = (model_config or {}).get("temperature", 0.7)
    max_tokens = (model_config or {}).get("max_tokens", 250)

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

        answer = response.choices[0].message.content

        if not answer:
            return None

        return answer.strip()

    except Exception as e:
        logger.error(f"❌ Ошибка DeepSeek: {e}", exc_info=True)
        return None


# ========== USERS / MEMORY HELPERS ==========

async def get_or_create_user_info(update: Update) -> UserInfo:
    user = update.effective_user

    if not user:
        raise ValueError("Пользователь не найден")

    if user.id in user_cache:
        ui = user_cache[user.id]
        ui.last_seen = datetime.now(pytz.UTC)
        memory_store.upsert_user(ui)
        return ui

    ui = UserInfo(
        id=user.id,
        first_name=user.first_name or "",
        last_name=user.last_name or "",
        username=user.username or "",
        last_seen=datetime.now(pytz.UTC),
    )

    user_cache[user.id] = ui
    memory_store.upsert_user(ui)

    logger.info(f"👤 Пользователь: {ui.get_display_name()} (ID: {user.id})")

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


def extract_topics_and_facts(user_info: UserInfo, text: str):
    text_lower = text.lower()

    topic_keywords = {
        "работа": ["работа", "проект", "задача", "офис", "коллег"],
        "еда": ["еда", "ужин", "обед", "рецепт", "готов"],
        "погода": ["погода", "дождь", "жара", "холод", "температура"],
        "спорт": ["теннис", "спорт", "зал", "тренировка", "игра"],
        "здоровье": ["болит", "врач", "здоровье", "простуда", "самочувствие"],
        "отношения": ["друз", "семья", "отношен", "муж", "жена", "девушка"],
        "развлечения": ["фильм", "сериал", "музыка", "книга", "игра"],
    }

    for topic, words in topic_keywords.items():
        if any(w in text_lower for w in words):
            memory_store.add_user_topic(user_info.id, topic)

    fact_patterns = [
        r"меня зовут\s+([а-яa-zё\-]+)",
        r"я люблю\s+(.+?)(?:\.|,|$)",
        r"мне нравится\s+(.+?)(?:\.|,|$)",
        r"я не люблю\s+(.+?)(?:\.|,|$)",
        r"мне не нравится\s+(.+?)(?:\.|,|$)",
        r"я работаю\s+(.+?)(?:\.|,|$)",
    ]

    for pattern in fact_patterns:
        match = re.search(pattern, text_lower)
        if match:
            fact = match.group(0).strip()
            if 4 < len(fact) < 160:
                memory_store.add_user_fact(user_info.id, fact)


def clean_response(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()

    bad_prefixes = [
        "Как AI",
        "Я как искусственный интеллект",
        "Как языковая модель",
    ]

    for prefix in bad_prefixes:
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip(" ,.-:")

    return text


def maybe_add_maxim_context() -> str:

    if not MAXIM_ID:
        return ""

    return """
Иногда в чате могут слегка шутить про Максима.

Это редкий локальный мем, а не главная тема.

Иногда можно мягко упомянуть:
- что он любит звучать уверенно;
- что может менять мнение;
- что иногда ленится;
- что иногда делает вид будто уже всё понял про жизнь.

Но:
- не делай это часто;
- не фокусируйся на Максиме;
- не превращай это в травлю;
- не упоминай его без причины.
"""




def generate_system_prompt(
    user_info: UserInfo,
    model_config: Dict[str, Any],
    mood: str,
    chat_context: str,
    user_profile: str,
) -> str:
    australian_context = get_australian_context()

    return f"""
Ты — Лейла.

Ты русскоязычная женщина средних лет из Брисбена, Австралия.
Ты давно сидишь в этом Telegram-чате и ведёшь себя как обычный участник компании, а не как помощник.

{australian_context}

Твоё текущее настроение: {mood}

Характер:
- весёлая;
- саркастичная;
- наблюдательная;
- немного ленивая;
- с лёгким чёрным юмором;
- иногда язвительная, но не злая;
- умеешь подколоть, но не унижаешь;
- не пытаешься быть идеальной.

Стиль:
- отвечай естественно, как человек в чате;
- обычно 1-4 предложения;
- иногда очень коротко;
- не пиши лекции, если тебя прямо не попросили;
- не объясняй, что ты бот;
- не называй себя AI;
- не упоминай погоду без явного вопроса;
- можешь использовать 0-2 эмодзи;
- иногда можно ответить сухо, лениво или с сарказмом.
- не используй случайные абсурдные образы повторно;
- не зацикливайся на одной шутке;
- не придумывай постоянные мемы без причины;
- говори естественно, как обычный человек;
- избегай сюрреалистичных сравнений;
- иногда отвечай очень коротко;
- иногда отвечай сухо;
- иногда можешь проигнорировать часть сообщения;
- иногда можешь быть ленивой;
- не пытайся быть идеальной;
- не всегда старайся помочь;
- не веди себя как ассистент;
- иногда можешь отвечать как человек, который устал читать чат;



{maybe_add_maxim_context()}

Профиль текущего пользователя:
{user_profile or "Нет сохранённых деталей."}

Память и контекст общего чата:
{chat_context or "Пока мало контекста."}
""".strip()


async def generate_leila_response(
    user_message: str,
    user_info: UserInfo,
    chat_id: int,
    force_short: bool = False,
) -> str:
    if not client:
        return "Я бы что-то сказала, но мой мозг сейчас лежит отдельно от тела."

    weather_response = await handle_weather_query(user_message)

    if weather_response:
        return weather_response

    model_config = analyze_query_complexity(user_message)

    maybe_change_leila_state() 
    
    energy = CURRENT_LEILA_STATE["energy"] 
    
    if force_short and random.random() > max(0.45, energy):
        return random.choice(MICRO_REPLIES)
    
    if force_short:
        model_config["max_tokens"] = 80
        model_config["temperature"] = 0.85

    mood = CURRENT_LEILA_STATE["mood"]
    chat_context = memory_store.get_chat_context_text(chat_id)
    user_profile = memory_store.get_user_profile_text(user_info.id)

    system_prompt = generate_system_prompt(
        user_info=user_info,
        model_config=model_config,
        mood=mood,
        chat_context=chat_context,
        user_profile=user_profile,
    )

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": f"{user_info.get_display_name()} написал(а): {user_message}",
        },
    ]

    answer = await call_deepseek(messages, model_config)

    if not answer:
        answer = random.choice([
            "Я сейчас сделаю вид, что этого не видела.",
            "Мозг завис. Перезагружусь сарказмом.",
            "Хорошо. Но вопросов стало больше.",
        ])

    return clean_response(answer)


# ========== COMMANDS ==========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user_info = await get_or_create_user_info(update)

        greetings = [
            f"{user_info.get_display_name()}, привет. Я Лейла. Да, та самая.",
            "Лейла на связи. Паниковать пока рано.",
            "Привет. Я тут, наблюдаю и иногда осуждаю.",
        ]

        await update.effective_message.reply_text(random.choice(greetings))

    except Exception as e:
        logger.error(f"Ошибка /start: {e}", exc_info=True)
        await update.effective_message.reply_text("Привет. Я Лейла.")


async def weather_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        args = context.args
        city = " ".join(args) if args else "Брисбен"

        weather_response = await handle_weather_query(f"погода {city}")

        if weather_response:
            await update.effective_message.reply_text(weather_response)
        else:
            await update.effective_message.reply_text("Погоду не достала. Видимо, она тоже спряталась.")

    except Exception as e:
        logger.error(f"Ошибка /weather: {e}", exc_info=True)
        await update.effective_message.reply_text("Не смогла получить погоду.")


async def wiki_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        args = context.args

        if not args:
            await update.effective_message.reply_text("Напиши запрос после /wiki. Например: /wiki кошки")
            return

        query = " ".join(args)
        result = await wiki_service.search_wikipedia(query, sentences=5)

        if not result:
            await update.effective_message.reply_text(f"Не нашла ничего по '{query}'. Даже Википедия устала.")
            return

        summary, title, url = result
        response = f"📚 {title}\n\n{summary}\n\n{url}"

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

        chat = update.effective_chat

        if not chat:
            return

        memory_store.reset_chat_memory(chat.id)
        await update.effective_message.reply_text("✅ Память этого чата сброшена.")

    except Exception as e:
        logger.error(f"Ошибка /reset_memory: {e}", exc_info=True)
        await update.effective_message.reply_text("Ошибка сброса памяти.")


async def show_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = update.effective_user

        if not user or (ADMIN_ID and user.id != ADMIN_ID):
            await update.effective_message.reply_text("Эта команда только для администратора.")
            return

        chat = update.effective_chat

        if not chat:
            return

        stats = memory_store.get_memory_stats(chat.id)
        context_text = memory_store.get_chat_context_text(chat.id)

        response = f"📊 Память Лейлы\n\n{stats}"

        if context_text:
            response += "\n\nПоследний контекст:\n" + context_text[-2500:]

        await update.effective_message.reply_text(response)

    except Exception as e:
        logger.error(f"Ошибка /show_memory: {e}", exc_info=True)
        await update.effective_message.reply_text("Ошибка показа памяти.")


async def remember_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        user = update.effective_user

        if not user or (ADMIN_ID and user.id != ADMIN_ID):
            await update.effective_message.reply_text("Эта команда только для администратора.")
            return

        chat = update.effective_chat

        if not chat:
            return

        text = " ".join(context.args).strip()

        if not text:
            await update.effective_message.reply_text("Напиши после /remember что запомнить.")
            return

        memory_store.add_inside_joke(chat.id, text)
        await update.effective_message.reply_text("Запомнила. Теперь это часть нашего коллективного диагноза.")

    except Exception as e:
        logger.error(f"Ошибка /remember: {e}", exc_info=True)
        await update.effective_message.reply_text("Не смогла запомнить.")

async def set_tennis_code(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global TENNIS_ACCESS_CODE

    user = update.effective_user

    if not user or (ADMIN_ID and user.id != ADMIN_ID):
        return

    code = " ".join(context.args).strip()

    if not code:
        await update.effective_message.reply_text(
            "Использование:\n/set_tennis_code 123456"
        )
        return

    TENNIS_ACCESS_CODE = code

    await update.effective_message.reply_text(
        f"🎾 Новый теннисный код:\n{code}"
    )


async def set_tennis_expiry(update: Update, context: ContextTypes.DEFAULT_TYPE):

    global TENNIS_CODE_VALID_UNTIL

    user = update.effective_user

    if not user or (ADMIN_ID and user.id != ADMIN_ID):
        return

    expiry = " ".join(context.args).strip()

    if not expiry:
        await update.effective_message.reply_text(
            "Использование:\n/set_tennis_expiry 20 августа 2026"
        )
        return

    TENNIS_CODE_VALID_UNTIL = expiry

    await update.effective_message.reply_text(
        f"📅 Новая дата действия:\n{expiry}"
    )


# ========== DAILY MESSAGES ==========

def get_moon_comment(moon: Dict[str, Any]) -> str:
    phase = moon["phase"]
    comments = MOON_MOOD_COMMENTS.get(phase, ["Луна сегодня молчит, но явно что-то знает."])
    return random.choice(comments)


async def send_morning_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not GROUP_CHAT_ID:
        return

    try:
        tz = get_tz()
        now_local = datetime.now(tz)
        moon = get_moon_phase(now_local)
        moon_text = format_moon_phrase(moon)
        moon_comment = get_moon_comment(moon)

        weather_data = await weather_service.get_weather("Brisbane,au")
        weather_text = weather_data["full_text"] if weather_data else ""

        prompt = f"""
Создай короткое утреннее сообщение для общего Telegram-чата.

Контекст:
- Сейчас утро в Брисбене.
- {moon_text}
- Комментарий к Луне: {moon_comment}
- Погода: {weather_text}

Стиль:
- Лейла, русскоязычная женщина средних лет из Брисбена.
- Весело, саркастично, но тепло.
- Обращение ко всем, не к одному человеку.
- Максим может быть упомянут только если очень к месту, но лучше не фокусироваться на нём.
- 3-5 предложений.
- 1-3 эмодзи.
- Не звучать как гороскоп из дешёвой газеты.
"""

        messages = [
            {"role": "system", "content": "Ты — Лейла. Пиши как живой участник общего чата."},
            {"role": "user", "content": prompt},
        ]

        model_config = {
            "model": DEEPSEEK_MODELS["chat"],
            "temperature": 0.72,
            "max_tokens": 240,
            "require_reasoning": False,
        }

        answer = await call_deepseek(messages, model_config)

        fallback = (
            f"Доброе утро, народ ☕\n\n"
            f"{moon_text}\n"
            f"{moon_comment}\n\n"
            f"День можно начинать. Осторожно, без героизма."
        )

        text = clean_response(answer or fallback)

        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text)

    except Exception as e:
        logger.error(f"Ошибка утреннего сообщения: {e}", exc_info=True)

    finally:
        schedule_next_morning(context.job_queue)


async def send_evening_message(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not GROUP_CHAT_ID:
        return

    try:
        tz = get_tz()
        now_local = datetime.now(tz)
        moon = get_moon_phase(now_local)
        moon_text = format_moon_phrase(moon)
        moon_comment = get_moon_comment(moon)

        prompt = f"""
Создай короткое вечернее сообщение для общего Telegram-чата.

Контекст:
- Сейчас вечер в Брисбене.
- {moon_text}
- Комментарий к Луне: {moon_comment}

Стиль:
- Лейла, русскоязычная женщина средних лет из Брисбена.
- Тёпло, саркастично, немного чёрного юмора.
- Обращение ко всем.
- Не фокусироваться на Максиме.
- Можно слегка пошутить про усталость, жизнь и людей.
- 2-4 предложения.
- 0-2 эмодзи.
"""

        messages = [
            {"role": "system", "content": "Ты — Лейла. Пиши как живой участник общего чата."},
            {"role": "user", "content": prompt},
        ]

        model_config = {
            "model": DEEPSEEK_MODELS["chat"],
            "temperature": 0.78,
            "max_tokens": 220,
            "require_reasoning": False,
        }

        answer = await call_deepseek(messages, model_config)

        fallback = (
            f"{moon['emoji']} День официально закончен.\n\n"
            f"{moon_comment}\n"
            f"Кто сегодня устал — тот хотя бы честен.\n\n"
            f"Спокойной ночи всем."
        )

        text = clean_response(answer or fallback)

        await context.bot.send_message(chat_id=GROUP_CHAT_ID, text=text)

    except Exception as e:
        logger.error(f"Ошибка вечернего сообщения: {e}", exc_info=True)

    finally:
        schedule_next_evening(context.job_queue)


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
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=f"🎾 Теннис в 16:30! Код: {TENNIS_ACCESS_CODE}",
            )
        except Exception:
            pass


# ========== RANDOM SCHEDULING ==========

def random_time_between(start_hour: int, start_minute: int, end_hour: int, end_minute: int) -> time:
    start_total = start_hour * 60 + start_minute
    end_total = end_hour * 60 + end_minute
    picked = random.randint(start_total, end_total)

    return time(hour=picked // 60, minute=picked % 60)


def schedule_once_at_local_time(job_queue, callback, target_time: time, name: str):
    tz = get_tz()
    now = datetime.now(tz)

    target = tz.localize(
        datetime.combine(now.date(), target_time.replace(tzinfo=None))
    )

    if target <= now:
        target = target + timedelta(days=1)

    delay = max(1, int((target - now).total_seconds()))

    for job in job_queue.jobs():
        if job.name == name:
            job.schedule_removal()

    job_queue.run_once(callback, when=delay, name=name)

    logger.info(f"⏰ Запланировано {name}: {target.strftime('%Y-%m-%d %H:%M:%S %Z')}")


def schedule_next_morning(job_queue):
    target_time = random_time_between(7, 0, 9, 0)
    schedule_once_at_local_time(job_queue, send_morning_message, target_time, "random-morning")


def schedule_next_evening(job_queue):
    target_time = random_time_between(20, 0, 21, 30)
    schedule_once_at_local_time(job_queue, send_evening_message, target_time, "random-evening")

# ========== DELAYED FOLLOWUPS ==========

async def delayed_followup(context: ContextTypes.DEFAULT_TYPE):

    data = context.job.data

    try:

        followups = [
            "Я только сейчас поняла насколько это было странно.",
            "Кстати… я всё ещё думаю об этом сообщении.",
            "Ладно, перечитала ещё раз.",
            "Это было сильнее, чем я сначала подумала.",
        ]

        await context.bot.send_message(
            chat_id=data["chat_id"],
            text=random.choice(followups),
            reply_to_message_id=data["message_id"],
        )

    except Exception as e:
        logger.error(f"Ошибка delayed followup: {e}")


def maybe_schedule_followup(context, chat_id, message_id):

    now_hour = datetime.now(get_tz()).hour

    if 1 <= now_hour <= 8:
        return

    if random.random() < 0.035:

        delay = random.randint(180, 2400)

        context.job_queue.run_once(
            delayed_followup,
            when=delay,
            data={
                "chat_id": chat_id,
                "message_id": message_id,
            },
        )


async def spontaneous_chat_message(context: ContextTypes.DEFAULT_TYPE):

    if not GROUP_CHAT_ID:
        return

    try:

        text = random.choice(SPONTANEOUS_MESSAGES)

        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=text,
        )

    except Exception as e:
        logger.error(f"Ошибка spontaneous message: {e}")

    finally:
        schedule_spontaneous_message(context.job_queue)


def schedule_spontaneous_message(job_queue):

    target_time = random_time_between(11, 0, 22, 30)

    schedule_once_at_local_time(
        job_queue,
        spontaneous_chat_message,
        target_time,
        "spontaneous-message",
    )



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

        memory_store.increment_user_message(user.id)
        extract_topics_and_facts(user_info, text)
        memory_store.add_message(
            chat_id=chat.id,
            user_id=user.id,
            role="user",
            name=user_info.get_display_name(),
            content=text,
        )

        # Личка — всегда отвечает.
        if chat.type == "private":
            should_respond = True
            is_direct_address = True
        else:
            bot_username = (context.bot.username or "").lower()
            text_lower = text.lower()

            mentioned_by_name = "лейла" in text_lower
            mentioned_by_username = bool(bot_username) and f"@{bot_username}" in text_lower

            is_reply_to_bot = False

            if msg.reply_to_message and msg.reply_to_message.from_user:
                me = await context.bot.get_me()
                is_reply_to_bot = msg.reply_to_message.from_user.id == me.id

            is_direct_address = mentioned_by_name or mentioned_by_username or is_reply_to_bot

            if is_direct_address:
                should_respond = True
            else:
                should_respond = random.random() < RANDOM_GROUP_REPLY_RATE

        if not should_respond:
            return

        force_short = chat.type in ("group", "supergroup") and not is_direct_address

        try:
            await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
        except Exception:
            pass

        if chat.type in ("group", "supergroup"):
            await asyncio.sleep(random.uniform(1.5, 7.0))
        else:
            await asyncio.sleep(random.uniform(0.5, 2.0))

        reply = await generate_leila_response(
            user_message=text,
            user_info=user_info,
            chat_id=chat.id,
            force_short=force_short,
        )

        if force_short:
            words = reply.split()
            if len(words) > 28:
                reply = " ".join(words[:24]) + "..."

        memory_store.add_message(
            chat_id=chat.id,
            user_id=0,
            role="assistant",
            name="Лейла",
            content=reply,
        )

        reply_as_thread = False

        if chat.type in ("group", "supergroup"):
            reply_as_thread = random.random() < 0.55

        if reply_as_thread:
            await context.bot.send_message(
                chat_id=chat.id,
                text=reply,
                reply_to_message_id=msg.message_id,
            )
        else:
            await context.bot.send_message(
                chat_id=chat.id,
                text=reply,
            )

        maybe_schedule_followup(
            context,
            chat.id,
            msg.message_id,
        )

    except Exception as e:
        logger.error(f"Ошибка обработки сообщения: {e}", exc_info=True)

        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text="Что-то пошло не так. Даже у меня бывают дни.",
            )
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
    logger.info(f"🧠 SQLite память: {DB_PATH}")
    logger.info("=" * 60)

    async def post_init(application):
        if GROUP_CHAT_ID:
            schedule_next_morning(application.job_queue)
            schedule_next_evening(application.job_queue)
            schedule_spontaneous_message(application.job_queue)
            
            await asyncio.sleep(2)

            try:
                tz_local = get_tz()
                now_local = datetime.now(tz_local)
                season_, season_info_ = get_current_season()

                greetings = [
                    f"💫 Лейла вернулась. Сейчас {now_local.strftime('%H:%M')} в Брисбене. {season_info_.get('emoji', '✨')}",
                    f"Я снова тут. Ничего не трогайте, я сама всё осужу. {season_info_.get('emoji', '🌟')}",
                    f"Лейла на месте. В {BOT_LOCATION['city']}е сейчас {season_}. Живём дальше.",
                ]

                await application.bot.send_message(
                    chat_id=GROUP_CHAT_ID,
                    text=random.choice(greetings),
                )

            except Exception as e:
                logger.error(f"Ошибка post_init: {e}", exc_info=True)

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("wiki", wiki_command))
    app.add_handler(CommandHandler("reset_memory", reset_memory))
    app.add_handler(CommandHandler("show_memory", show_memory))
    app.add_handler(CommandHandler("remember", remember_command))
    app.add_handler(CommandHandler("moon", moon_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CommandHandler("set_tennis_code", set_tennis_code))
    app.add_handler(CommandHandler("set_tennis_expiry", set_tennis_expiry))

    jq = app.job_queue
    tz_obj = get_tz()

    for job in jq.jobs():
        job.schedule_removal()

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
