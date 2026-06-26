import random


def install(app_module):
    async def generated_spontaneous_chat_message(context):
        if not app_module.GROUP_CHAT_ID:
            return

        try:
            chat_context = app_module.memory_store.get_chat_context_text(app_module.GROUP_CHAT_ID)
            prompt = "Write one short casual Russian group message. Use recent context if useful. Avoid repeating old canned lines. Context:\n" + (chat_context or "No recent context.")
            messages = [
                {"role": "system", "content": "Write as Leila in Russian. Keep it casual and short."},
                {"role": "user", "content": prompt},
            ]
            model_config = {
                "model": app_module.DEEPSEEK_MODELS["chat"],
                "temperature": 1.05,
                "max_tokens": 120,
                "require_reasoning": False,
            }
            answer = await app_module.call_deepseek(messages, model_config)
            text = app_module.clean_response(answer or "")
            if not text:
                text = random.choice(app_module.SPONTANEOUS_MESSAGES)
            await context.bot.send_message(chat_id=app_module.GROUP_CHAT_ID, text=text)
        except Exception as e:
            app_module.logger.error(f"Spontaneous message error: {e}", exc_info=True)
        finally:
            app_module.schedule_spontaneous_message(context.job_queue)

    app_module.spontaneous_chat_message = generated_spontaneous_chat_message
