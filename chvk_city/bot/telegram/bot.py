import asyncio
import logging
import sys
import traceback
from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import BotCommand
from chvk_city.backend.config import settings
from chvk_city.bot.telegram.handlers import router

logger = logging.getLogger(__name__)

async def main():
    try:
        print("Инициализация...", flush=True)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            stream=sys.stdout,
        )
        logging.getLogger("httpx").setLevel(logging.WARNING)

        session = AiohttpSession(timeout=60.0)
        bot = Bot(token=settings.TELEGRAM_BOT_TOKEN, session=session)
        dp = Dispatcher()

        from chvk_city.bot.telegram.handlers import init_http_client
        init_http_client()
        print(f"Backend: {settings.API_BASE_URL}", flush=True)
        print(f"DATABASE_URL: {settings.DATABASE_URL}", flush=True)

        from chvk_city.backend.database.session import async_session
        from chvk_city.backend.services.taxi_service import TaxiService
        try:
            async with async_session() as db:
                _ = await TaxiService.get_driver(db, 0)
            print("DB: async_session OK", flush=True)
        except Exception as e:
            logger.error(f"DB async_session init check failed: {e}", exc_info=True)
            print(f"[WARN] DB check failed (driver registration may not work): {e}", flush=True)

        dp.include_router(router)

        try:
            await bot.set_my_commands([
                BotCommand(command="start", description="Запустить бота 🚀"),
                BotCommand(command="driver", description="Стать водителем 🚕"),
            ])
        except Exception as e:
            print(f"[WARN] set_my_commands: {e}", flush=True)

        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception as e:
            print(f"[WARN] delete_webhook: {e}", flush=True)

        print("Старт поллинга...", flush=True)
        await dp.start_polling(bot, skip_updates=True)
    except Exception as e:
        logger.error(f"Bot failed to start: {e}", exc_info=True)
        print(f"BOT START ERROR: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped.", flush=True)
    except Exception as e:
        print(f"ОШИБКА: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)
