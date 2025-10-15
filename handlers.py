import os
import re
import pytz
from datetime import datetime, timedelta
import logging

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove, User
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.markdown import hbold, html_decoration

import db as db
import kb
from states import Registration, AuctionCreation, Bidding

# Загружаем переменные окружения
ADMIN_ID = os.getenv("ADMIN_ID")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")

MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Создаем роутер
router = Router()


# --- Middleware для предварительной обработки ---

@router.message.middleware()
@router.callback_query.middleware()
async def user_status_middleware(handler, event, data):
    """
    Middleware для проверки статуса пользователя и обновления его username.
    Срабатывает на каждое сообщение и callback.
    """
    user: User = data.get('event_from_user')
    if not user:
        return await handler(event, data)

    if str(user.id) == ADMIN_ID:
        return await handler(event, data)

    # Обновляем username при каждом взаимодействии
    await db.update_user_username(user.id, user.username)

    # Пропускаем команды start, admin и процесс регистрации
    if isinstance(event, Message) and event.text in ["/start", "/admin"]:
        return await handler(event, data)
    if await data['state'].get_state() is not None:
        return await handler(event, data)

    status = await db.get_user_status(user.id)
    if status == 'banned':
        if isinstance(event, Message):
            await event.answer("Ваш доступ к боту заблокирован.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Ваш доступ к боту заблокирован.", show_alert=True)
        return  # Прерываем дальнейшую обработку

    if status == 'pending':
        if isinstance(event, Message):
            await event.answer("Ваша заявка на регистрацию находится на рассмотрении.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Ваша заявка на рассмотрении.", show_alert=True)
        return  # Прерываем дальнейшую обработку

    return await handler(event, data)


# --- Вспомогательные функции ---
def normalize_phone(phone: str) -> str:
    """Приводит номер телефона к формату +7XXXXXXXXXX."""
    cleaned_phone = re.sub(r'\D', '', phone)
    if len(cleaned_phone) == 10 and cleaned_phone.startswith('9'):
        return '+7' + cleaned_phone
    if len(cleaned_phone) == 11 and (cleaned_phone.startswith('7') or cleaned_phone.startswith('8')):
        return '+7' + cleaned_phone[1:]
    return phone


async def format_auction_post(auction_data: dict, bot: Bot, finished: bool = False) -> str:
    """Форматирует текст поста для канала (ФИНАЛЬНАЯ ВЕРСИЯ С ПРОВЕРКОЙ BLITZ)."""
    last_bid = await db.get_last_bid(auction_data['auction_id'])
    bot_info = await bot.get_me()
    safe_title = (auction_data['title'])
    safe_description = (auction_data['description'])

    if finished:
        if last_bid:
            winner_text = f"🎉 Поздравляем победителя @{(last_bid['username'])} с выигрышем лота за {last_bid['bid_amount']:,.2f} руб.!"
            return (
                f"<b>🔴 АУКЦИОН ЗАВЕРШЕН</b>\n\n"
                f"💎 <b>{safe_title}</b>\n\n"
                f"{winner_text}"
            )
        else:
            return (
                f"<b>🔴 АУКЦИОН ЗАВЕРШЕН</b>\n\n"
                f"💎 <b>{safe_title}</b>\n\n"
                f"Аукцион завершился без победителя."
            )

    current_price = last_bid['bid_amount'] if last_bid else auction_data['start_price']
    leader_text = f"@{(last_bid['username'])}" if last_bid else "Ставок еще нет"
    end_time_from_db = auction_data['end_time']
    end_time_dt = end_time_from_db.astimezone(MOSCOW_TZ)

    blitz_price_text = ""
    if auction_data.get('blitz_price'):
        blitz_price_text = f"⚡️ <b>Блиц-цена:</b> {auction_data['blitz_price']:,.2f} руб.\n\n"

    text = (
        f"💎 <b>{safe_title}</b>\n\n"
        f"{safe_description}\n\n"
        f"💰 <b>Текущая ставка:</b> {current_price:,.2f} руб.\n"
        f"👑 <b>Лидер:</b> {leader_text}\n"
        f"{blitz_price_text}"
        f"⏳ <b>Окончание:</b> {end_time_dt.strftime('%d.%m.%Y в %H:%M')} (МСК)\n\n"
        f"Для участия и ставок перейдите в нашего бота: @{bot_info.username}"
    )
    return text

# --- 1. РЕГИСТРАЦИЯ ПОЛЬЗОВАТЕЛЕЙ ---

@router.message(CommandStart(), StateFilter(default_state))
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start."""
    user_status = await db.get_user_status(message.from_user.id)
    if user_status == 'approved':
        await message.answer("Добро пожаловать в аукцион!", reply_markup=kb.get_main_menu())
    else:
        await state.set_state(Registration.waiting_for_full_name)
        await message.answer(
            "Здравствуйте! Для участия в аукционе, пожалуйста, зарегистрируйтесь.\n\n"
            "Введите ваше ФИО:"
        )


@router.message(StateFilter(Registration.waiting_for_full_name))
async def process_full_name(message: Message, state: FSMContext):
    """Ловит ФИО и запрашивает номер телефона."""
    await state.update_data(full_name=message.text)
    await state.set_state(Registration.waiting_for_phone)
    await message.answer("Отлично! Теперь, пожалуйста, отправьте ваш номер телефона.",
                         reply_markup=kb.get_phone_keyboard())


@router.message(StateFilter(Registration.waiting_for_phone), F.contact)
async def process_phone(message: Message, state: FSMContext, bot: Bot):
    """Ловит номер, сохраняет заявку и отправляет админу на модерацию."""
    phone_number = normalize_phone(message.contact.phone_number)
    user_data = await state.get_data()

    await db.add_user_request(
        user_id=message.from_user.id,
        username=message.from_user.username,
        full_name=user_data['full_name'],
        phone_number=phone_number
    )

    await message.answer("Спасибо! Ваша заявка отправлена на модерацию. Ожидайте подтверждения.",
                         reply_markup=ReplyKeyboardRemove())

    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"❗️ Новая заявка на регистрацию:\n\n"
            f"ID: `{message.from_user.id}`\n"
            f"Username: @{message.from_user.username}\n"
            f"ФИО: {user_data['full_name']}\n"
            f"Телефон: `{phone_number}`",
            parse_mode="HTML",
            reply_markup=kb.admin_approval_keyboard(message.from_user.id)
        )
    except TelegramAPIError as e:
        logging.error(f"Не удалось отправить заявку в админ-чат: {e}")
        await message.bot.send_message(ADMIN_ID,
                                       "Не удалось отправить заявку в админ-чат. Проверьте ID чата и права бота.")

    await state.clear()


# --- 2. МОДЕРАЦИЯ ЗАЯВОК (АДМИН) ---

@router.callback_query(F.data.startswith("approve_user_"))
async def approve_user(callback: CallbackQuery, bot: Bot):
    """Одобрение заявки пользователя."""
    user_id = int(callback.data.split("_")[2])
    await db.update_user_status(user_id, 'approved')
    await callback.message.edit_text(f"✅ Пользователь {user_id} одобрен.")
    try:
        await bot.send_message(user_id, "Ваша заявка одобрена! Теперь вы можете участвовать в аукционах.",
                               reply_markup=kb.get_main_menu())
    except TelegramAPIError as e:
        logging.error(f"Не удалось уведомить пользователя {user_id} об одобрении: {e}")
    await callback.answer()


@router.callback_query(F.data.startswith("decline_user_"))
async def decline_user(callback: CallbackQuery, bot: Bot):
    """Отклонение заявки пользователя."""
    # TODO: Добавить FSM для ввода причины отклонения
    user_id = int(callback.data.split("_")[2])
    await db.update_user_status(user_id, 'banned')
    await callback.message.edit_text(f"❌ Пользователь {user_id} отклонен/забанен.")
    try:
        await bot.send_message(user_id, "К сожалению, ваша заявка на регистрацию была отклонена.")
    except TelegramAPIError as e:
        logging.error(f"Не удалось уведомить пользователя {user_id} об отклонении: {e}")
    await callback.answer()


# --- 3. ГЛАВНОЕ МЕНЮ И ПРОСМОТР АУКЦИОНА ---

@router.message(F.text == "💎 Актуальный аукцион")
async def show_current_auction(message: Message, bot: Bot):
    """Показывает карточку активного аукциона в ЛС."""
    auction = await db.get_active_auction()
    if not auction:
        await message.answer("На данный момент активных аукционов нет.")
        return

    text = await format_auction_post(auction, bot)
    await message.answer_photo(
        photo=auction['photo_id'],
        caption=text,
        parse_mode="HTML",
        reply_markup=kb.get_auction_keyboard(auction['auction_id'], auction['blitz_price'])
    )

@router.message(F.text == "📚 Все аукционы")
async def show_all_auctions(message: Message):
    await message.answer("Этот раздел находится в разработке. Здесь будет история всех завершенных аукционов.")

@router.message(F.text == "📞 Связь с администратором")
async def contact_admin(message: Message):
    admin_username = "CoId_Siemens"
    await message.answer(f"По всем вопросам вы можете написать нашему администратору: @{admin_username}")


# --- 4. ЛОГИКА СТАВОК ---

@router.callback_query(F.data.startswith("bid_auction_"))
async def make_bid_start(callback: CallbackQuery, state: FSMContext):
    """Начало процесса ставки."""
    auction_id = int(callback.data.split("_")[2])
    auction = await db.get_active_auction()

    if not auction or auction['auction_id'] != auction_id:
        await callback.answer("Аукцион уже завершен или неактивен.", show_alert=True)
        await callback.message.delete()
        return

    # Проверка интервала между ставками
    end_time_dt = auction['end_time']
    time_to_end = end_time_dt - datetime.now(end_time_dt.tzinfo)

    if time_to_end > timedelta(minutes=30):
        last_bid_time = await db.get_user_last_bid_time(callback.from_user.id, auction_id)
        if last_bid_time and (datetime.now(last_bid_time.tzinfo) - last_bid_time) < timedelta(minutes=10):
            remaining_time = timedelta(minutes=10) - (datetime.now(last_bid_time.tzinfo) - last_bid_time)
            await callback.answer(f"Следующую ставку можно сделать через {remaining_time.seconds // 60 + 1} мин.",
                                  show_alert=True)
            return

    await state.set_state(Bidding.waiting_for_bid_amount)
    await state.update_data(auction_id=auction_id, private_message_id=callback.message.message_id)

    last_bid = await db.get_last_bid(auction_id)
    current_price = last_bid['bid_amount'] if last_bid else auction['start_price']

    await callback.message.answer(
        f"Текущая ставка: {current_price:,.0f} руб.\n"
        f"Минимальный шаг: {auction['min_step']:,.0f} руб.\n\n"
        f"{hbold('Введите вашу ставку:')}", parse_mode="HTML",
    )
    await callback.answer()


@router.message(StateFilter(Bidding.waiting_for_bid_amount))
async def process_bid_amount(message: Message, state: FSMContext, bot: Bot):
    """Обработка введенной суммы ставки."""
    try:
        bid_amount = float(message.text.replace(',', '.'))
    except ValueError:
        await message.answer("Пожалуйста, введите числовое значение.")
        return

    data = await state.get_data()
    auction = await db.get_active_auction()

    if not auction or auction['auction_id'] != data['auction_id']:
        await message.answer("Аукцион завершился, пока вы делали ставку.")
        await state.clear()
        return

    last_bid = await db.get_last_bid(auction['auction_id'])
    current_price = last_bid['bid_amount'] if last_bid else auction['start_price']

    if bid_amount < current_price + auction['min_step']:
        await message.answer(f"Ваша ставка должна быть как минимум {current_price + auction['min_step']:,.0f} руб.")
        return

    previous_leader = last_bid['user_id'] if last_bid else None

    await db.add_bid(auction['auction_id'], message.from_user.id, bid_amount)
    await message.answer(f"✅ Ваша ставка в размере {bid_amount:,.0f} руб. принята!")
    await state.clear()

    # Уведомляем предыдущего лидера
    if previous_leader and previous_leader != message.from_user.id:
        try:
            await bot.send_message(previous_leader,
                                   f"❗️ Вашу ставку на аукционе '{auction['title']}' перебили! Новая ставка: {bid_amount:,.0f} руб.")
        except TelegramAPIError as e:
            logging.warning(f"Не удалось уведомить пользователя {previous_leader}: {e}")

    # Обновляем главный пост в канале
    new_text = await format_auction_post(auction, bot)
    try:
        await bot.edit_message_caption(
            chat_id=CHANNEL_ID,
            message_id=auction['channel_message_id'],
            caption=new_text,
            parse_mode="HTML"
        )
    except TelegramAPIError as e:
        logging.error(f"Не удалось обновить пост в канале {CHANNEL_ID}: {e}")

    # Обновляем приватную карточку аукциона
    try:
        await bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=data['private_message_id'],
            caption=new_text,
            parse_mode="HTML",
            reply_markup=kb.get_auction_keyboard(auction['auction_id'], auction['blitz_price'])
        )
    except TelegramAPIError as e:
        logging.warning(f"Не удалось обновить приватную карточку для {message.chat.id}: {e}")


# --- 5. АДМИН-ПАНЕЛЬ ---

@router.message(Command("admin"), F.from_user.id == int(ADMIN_ID))
async def admin_panel(message: Message):
    """Команды для администратора."""
    await message.answer("Админ-панель:\n"
                         "/create_auction - Создать новый аукцион\n"
                         "/finish_auction - Завершить аукцион досрочно\n"
                         "/ban [id] - Забанить пользователя\n"
                         "/unban [id] - Разбанить пользователя")


# --- Создание аукциона (FSM) ---
@router.message(Command("create_auction"), F.from_user.id == int(ADMIN_ID))
async def create_auction_start(message: Message, state: FSMContext):
    active_auction = await db.get_active_auction()
    if active_auction:
        await message.answer("Нельзя создать новый аукцион, пока не завершен предыдущий.")
        return
    await state.set_state(AuctionCreation.waiting_for_title)
    await message.answer("Шаг 1/6: Введите название лота:")


@router.message(StateFilter(AuctionCreation.waiting_for_title))
async def process_auction_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(AuctionCreation.waiting_for_description)
    await message.answer("Шаг 2/6: Введите описание лота")


@router.message(StateFilter(AuctionCreation.waiting_for_description))
async def process_auction_desc(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(AuctionCreation.waiting_for_photo)
    await message.answer("Шаг 3/6: Отправьте фотографию лота:")


@router.message(StateFilter(AuctionCreation.waiting_for_photo), F.photo)
async def process_auction_photo(message: Message, state: FSMContext):
    await state.update_data(photo=message.photo[-1].file_id)
    await state.set_state(AuctionCreation.waiting_for_start_price)
    await message.answer("Шаг 4/6: Введите начальную цену (число, например: 150000):")


@router.message(StateFilter(AuctionCreation.waiting_for_start_price))
async def process_auction_start_price(message: Message, state: FSMContext):
    try:
        await state.update_data(start_price=float(message.text))
        await state.set_state(AuctionCreation.waiting_for_blitz_price)
        await message.answer("Шаг 5/6: Введите блиц-цену (число, если не нужна - введите 0):")
    except ValueError:
        await message.answer("Неверный формат. Введите число (например, 150000).")


@router.message(StateFilter(AuctionCreation.waiting_for_blitz_price))
async def process_auction_blitz_price(message: Message, state: FSMContext):
    try:
        blitz_price = float(message.text)
        await state.update_data(blitz_price=blitz_price if blitz_price > 0 else None)
        await state.set_state(AuctionCreation.waiting_for_end_time)
        await message.answer(
            "Шаг 6/6: Введите дату и время окончания аукциона в формате: ДД.ММ.ГГГГ ЧЧ:ММ\n\nНапример: 25.10.2025 21:00")
    except ValueError:
        await message.answer("Неверный формат. Введите число (например, 300000).")


@router.message(StateFilter(AuctionCreation.waiting_for_end_time))
async def process_auction_end_time(message: Message, state: FSMContext, bot: Bot):
    try:
        naive_end_time = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
        # end_time = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
        end_time = MOSCOW_TZ.localize(naive_end_time)
        await state.update_data(end_time=end_time)
        await state.update_data(min_step=1000) # Шаг ставки по умолчанию
        data = await state.get_data()

        auction_id = await db.create_auction(data)
        auction_data_full = await db.get_active_auction()
        text = await format_auction_post(auction_data_full, bot)

        sent_message = await bot.send_photo(
            chat_id=CHANNEL_ID,
            photo=data['photo'],
            caption=text,
            parse_mode="HTML"
        )

        await db.set_auction_message_id(auction_id, sent_message.message_id)
        await message.answer(f"✅ Аукцион «{data['title']}» успешно создан и опубликован в канале.")

    except ValueError:
        await message.answer("Неверный формат даты. Пожалуйста, введите дату в формате: ДД.ММ.ГГГГ ЧЧ:ММ")
    except Exception as e:
        await message.answer(f"❌ Произошла ошибка при создании аукциона: {e}")
        logging.error(f"Ошибка создания аукциона: {e}")
    finally:
        await state.clear()


@router.message(Command("finish_auction"), F.from_user.id == int(ADMIN_ID))
async def finish_auction_command(message: Message, bot: Bot):
    active_auction = await db.get_active_auction()
    if not active_auction:
        await message.answer("Нет активных аукционов для завершения.")
        return

    auction_id = active_auction['auction_id']
    last_bid = await db.get_last_bid(auction_id)

    winner_id = last_bid['user_id'] if last_bid else None
    final_price = last_bid['bid_amount'] if last_bid else None

    # 1. Обновляем статус в БД
    await db.finish_auction(auction_id, winner_id, final_price)
    await message.answer(f"✅ Аукцион «{active_auction['title']}» принудительно завершен.")

    # 2. Обновляем пост в канале
    finished_post_text = await format_auction_post(active_auction, bot, finished=True)
    try:
        await bot.edit_message_caption(
            chat_id=CHANNEL_ID,
            message_id=active_auction['channel_message_id'],
            caption=finished_post_text,
            parse_mode="HTML"
        )
    except TelegramAPIError as e:
        logging.error(f"Не удалось обновить пост в канале после завершения: {e}")

    # 3. Уведомляем победителя, если он есть
    if winner_id:
        try:
            await bot.send_message(
                winner_id,
                f"🎉 Поздравляем! Вы победили в аукционе «{(active_auction['title'])}»!\n\n"
                f"Ваша выигрышная ставка: {final_price:,.2f} руб.\n\n"
                f"В ближайшее время с вами свяжется администратор для уточнения деталей оплаты и доставки."
            )
        except TelegramAPIError as e:
            logging.error(f"Не удалось уведомить победителя {winner_id}: {e}")
            await message.answer(
                f"❗️ Не удалось отправить уведомление победителю {winner_id}. Свяжитесь с ним вручную.")
