import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand
from chvk_city.backend.config import settings
from chvk_city.bot.telegram.handlers import router

logger = logging.getLogger(__name__)

async def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Correct way to set trust_env=False for aiogram
    session = AiohttpSession()
    # If we really need trust_env=False for Telegram API (rare), 
    # we would pass a custom aiohttp.ClientSession(trust_env=False) here.
    # But for now, let's keep it simple and fix the crash.
    
    bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, session=session)
    dp = Dispatcher()
    
    dp.include_router(router)
    
    # Set bot commands for the "Menu" button
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота 🚀"),
        BotCommand(command="driver", description="Стать водителем 🚕")
    ])
    
    # Delete webhook to ensure polling works
    logger.info("Deleting webhook...")
    await bot.delete_webhook(drop_pending_updates=True)
    
    logger.info("Bot started in DEBUG mode...")
    
    # Test connectivity to driver chat
    try:
        await bot.send_message(settings.DRIVER_CHAT_ID, "🚀 Бот запущен и готов к работе! Проверка связи с чатом водителей.")
        logger.info(f"Тестовое сообщение успешно отправлено в {settings.DRIVER_CHAT_ID}")
    except Exception as e:
        logger.error(f"ОШИБКА отправки тестового сообщения: {e}")
        logger.warning("СОВЕТ: Проверьте DRIVER_CHAT_ID в .env и убедитесь, что бот добавлен в группу и является админом.")

    await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
