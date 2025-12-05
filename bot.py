📅 *Действует до:* {TENNIS_CODE_VALID_UNTIL}

💬 *PS от Лейлы:*
"Пятница \+ теннис \= идеальное завершение недели\! 
Увидимся на корте\! 🎯" 

🏸🎳🏓🎿⛸️🛹🎮♟️🏒🏑🏏🥍🏐🏉🎱

_{season_info.get('emoji', '🎾')} {season_info.get('description', 'Сезон тенниса')} в Брисбене\!_"""
        ]
        
        # Choose random format
        tennis_message = random.choice(fun_formats)
        
        # Send the message with HTML formatting
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            text=tennis_message,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True
        )
        
        # 25% chance for extra visual reminder
        if random.random() < 0.25:
            await asyncio.sleep(3)
            
            code_display = f"""🔔 *Быстрый доступ к коду:*

┌─────────────────┐
│  {TENNIS_ACCESS_CODE:<15} │
└─────────────────┘

📱 *Скопируйте и используйте до:* {TENNIS_CODE_VALID_UNTIL}

💡 *Совет:* Сохраните это сообщение 
для быстрого доступа к коду\!
"""
            
            await context.bot.send_message(
                chat_id=GROUP_CHAT_ID,
                text=code_display,
                parse_mode=ParseMode.MARKDOWN_V2
            )
        
        logger.info(f"✅ Пятничное теннисное напоминание отправлено (действует до {TENNIS_CODE_VALID_UNTIL})")
            
    except Exception as e:
        logger.error(f"❌ Ошибка отправки теннисного напоминания: {e}")
        # Fallback simple message
        try:
            fallback_message = f"""🎾 Напоминание: теннис сегодня в 16:00!

Код доступа: {TENNIS_ACCESS_CODE}
Действует до: {TENNIS_CODE_VALID_UNTIL}

Приходите на корты! 😊"""
            
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
        return

    text = msg.text or ""
    if not text.strip():
        return
    
    if user.id == context.bot.id:
        return
    
    try:
        user_info = await get_or_create_user_info(update)
        user_name = user_info.get_display_name()
        is_maxim = user_info.is_maxim()
        
        logger.info(f"👤 {'МАКСИМ' if is_maxim else user_name}: {text[:50]}...")
        
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
            
            if not (is_maxim or mentioned_by_name or mentioned_by_username or reply_to_bot):
                return
        
        memory = get_conversation_memory(user.id, chat.id)
        
        if is_maxim and random.random() < 0.15:
            logger.info(f"💭 Пропускаем ответ Максиму для естественности")
            return
        
        extra_context = {}
        tz = get_tz()
        now = datetime.now(tz)
        time_of_day, time_desc = get_time_of_day(now)
        extra_context["time_context"] = time_desc
        
        season, season_info = get_current_season()
        extra_context["season_context"] = f"Сейчас {season} в {BOT_LOCATION['city']}е"
        
        reply, updated_memory = await generate_leila_response(
            text, 
            user_info, 
            memory, 
            extra_context
        )
        
        conversation_memories[get_memory_key(user.id, chat.id)] = updated_memory
        
        await context.bot.send_message(chat_id=chat.id, text=reply)
        logger.info(f"✅ Ответ отправлен {'Максиму' if is_maxim else user_name}")
            
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
        raise RuntimeError("BOT_TOKEN не задан")
    
    if not GROUP_CHAT_ID:
        raise RuntimeError("GROUP_CHAT_ID не задан")
    
    tz = get_tz()
    now = datetime.now(tz)
    season, season_info = get_current_season()
    
    logger.info("=" * 60)
    logger.info(f"🚀 ЗАПУСК БОТА ЛЕЙЛА С ВИКИПЕДИЕЙ И ТЕННИСОМ")
    logger.info(f"📍 Локация: {BOT_LOCATION['city']}, {BOT_LOCATION['country']}")
    logger.info(f"📅 Сезон: {season} ({season_info.get('description', '')})")
    logger.info(f"🕐 Время: {now.strftime('%H:%M:%S')}")
    logger.info(f"💬 Группа ID: {GROUP_CHAT_ID}")
    logger.info(f"👤 Максим ID: {MAXIM_ID}")
    logger.info(f"🎾 Теннисный код: {TENNIS_ACCESS_CODE}")
    logger.info(f"📅 Код действителен до: {TENNIS_CODE_VALID_UNTIL}")
    logger.info(f"🤖 DeepSeek доступен: {'✅' if client else '❌'}")
    logger.info(f"🌤️ Погодный сервис: {'✅' if OPENWEATHER_API_KEY else '❌'}")
    logger.info(f"📚 Википедия доступна: ✅ (только по команде /wiki)")
    logger.info("=" * 60)
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("weather", weather_command))
    app.add_handler(CommandHandler("wiki", wiki_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    tz_obj = get_tz()
    jq = app.job_queue
    
    for job in jq.jobs():
        job.schedule_removal()
    
    import time as time_module
    time_module.sleep(1)
    
    logger.info("📅 Настройка планировщика...")
    
    test_time = datetime.now(tz_obj)
    test_time = test_time.replace(second=0, microsecond=0)
    test_time = test_time.replace(minute=test_time.minute + 2)
    
    jq.run_once(
        send_morning_to_maxim,
        when=test_time,
        name="test-morning"
    )
    logger.info(f"🧪 Тестовый запуск в {test_time.strftime('%H:%M:%S')}")
    
    morning_time = time(hour=8, minute=30, tzinfo=tz_obj)
    evening_time = time(hour=21, minute=10, tzinfo=tz_obj)
    
    jq.run_daily(
        send_morning_to_maxim,
        time=morning_time,
        name="leila-morning"
    )
    logger.info(f"🌅 Утреннее сообщение Максиму в {morning_time}")
    
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
        days=(4,),  # 4 represents Friday (Monday=0, Tuesday=1, ..., Friday=4)
        name="friday-tennis"
    )
    logger.info(f"🎾 Пятничное теннисное напоминание в {friday_time.strftime('%H:%M')} (пятница)")
    logger.info(f"   Код: {TENNIS_ACCESS_CODE}, действует до: {TENNIS_CODE_VALID_UNTIL}")
    
    logger.info("🤖 Бот запущен!")
    logger.info("📝 Доступные команды: /start, /weather [город], /wiki [запрос]")
    logger.info("🎾 Автонапоминание о теннисе: Каждую пятницу в 16:00")
    
    try:
        app.run_polling()
    except Exception as e:
        logger.error(f"❌ Ошибка запуска: {e}")

if __name__ == "__main__":
    main()
