# scheduler.py
import logging
import os
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

import db
from handlers import format_auction_post  # Импортируем нашу функцию форматирования

CHANNEL_ID = os.getenv("CHANNEL_ID")


async def check_auctions(bot: Bot):
    """
    Проверяет и завершает аукционы, время которых истекло.
    Вызывается планировщиком каждую минуту.
    """
    try:
        expired_auctions = await db.get_expired_active_auctions()
        if not expired_auctions:
            return  # Если нет просроченных аукционов, ничего не делаем

        logging.info(f"Найдено {len(expired_auctions)} аукционов для завершения.")

        for auction in expired_auctions:
            auction_id = auction['auction_id']
            last_bid = await db.get_last_bid(auction_id)

            winner_id = last_bid['user_id'] if last_bid else None
            final_price = last_bid['bid_amount'] if last_bid else None

            # 1. Обновляем статус в БД
            await db.finish_auction(auction_id, winner_id, final_price)
            logging.info(f"Аукцион #{auction_id} завершен в базе данных.")

            # 2. Обновляем пост в канале
            finished_post_text = await format_auction_post(auction, bot, finished=True)
            try:
                await bot.edit_message_caption(
                    chat_id=CHANNEL_ID,  # Используем ID из базы
                    message_id=auction['channel_message_id'],
                    caption=finished_post_text,
                    parse_mode="HTML"
                )
            except TelegramAPIError as e:
                logging.error(f"Не удалось обновить пост для аукциона #{auction_id} в канале: {e}")

            # 3. Уведомляем победителя, если он есть
            if winner_id:
                try:
                    await bot.send_message(
                        winner_id,
                        f"🎉 Поздравляем! Вы победили в аукционе «{auction['title']}»!\n\n"
                        f"Ваша выигрышная ставка: {final_price:,.2f} руб.\n\n"
                        f"В ближайшее время с вами свяжется администратор для уточнения деталей."
                    )
                except TelegramAPIError as e:
                    logging.error(f"Не удалось уведомить победителя {winner_id} аукциона #{auction_id}: {e}")

    except Exception as e:
        logging.error(f"Произошла ошибка в задаче check_auctions: {e}")


def setup_scheduler(bot: Bot, timezone: str):
    """Настраивает и возвращает объект планировщика."""
    scheduler = AsyncIOScheduler(timezone=timezone)
    scheduler.add_job(check_auctions, 'interval', minutes=1, args=(bot,))
    return scheduler
