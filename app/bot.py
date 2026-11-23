import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.config import settings
from app.db import connect, close
from app.handlers.main import router
from app.services.schedule import refresh_schedule

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

async def main():
    if not settings.bot_token:
        raise RuntimeError("Не указан BOT_TOKEN. Заполни файл .env.")
    await connect()
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(refresh_schedule, "interval", minutes=max(5, settings.schedule_refresh_minutes),
                      id="schedule_refresh", max_instances=1, coalesce=True)
    scheduler.start()

    # Первый запуск не блокирует polling: обновление расписания идёт отдельной задачей.
    asyncio.create_task(refresh_schedule())
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await close()

if __name__ == "__main__":
    asyncio.run(main())
