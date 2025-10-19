# main.py
import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
# --- ИЗМЕНЕНО ---
# aiogram 3.7+ требует DefaultBotProperties
from aiogram.client.default import DefaultBotProperties
# ---
from dotenv import load_dotenv

from handlers import router
from db import init_db
from scheduler import setup_scheduler


async def main():
    # Настройка логирования
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    # Загрузка переменных окружения
    load_dotenv()
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        logging.critical("Токен бота не найден! Укажите его в файле .env")
        return

    # Инициализация бота и диспетчера
    # --- ИЗМЕНЕНО ---
    bot = Bot(token=bot_token, default=DefaultBotProperties(parse_mode="HTML"))
    # ---
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Подключение роутера
    dp.include_router(router)

    # Инициализация базы данных
    await init_db()

    # Настройка и запуск планировщика
    # --- ИЗМЕНЕНО: передаем bot ---
    scheduler = setup_scheduler(bot, timezone="Europe/Moscow")
    scheduler.start()

    logging.info("Бот запускается...")
    # Удаление вебхуков перед запуском
    await bot.delete_webhook(drop_pending_updates=True)
    # Запуск поллинга
    await dp.start_polling(bot)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")