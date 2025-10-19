import os
import re
import pytz
from datetime import datetime, timedelta
import logging
import io
import csv
from html import escape

from aiogram import Router, F, Bot, types
from aiogram.types import Message, CallbackQuery, User, InputMediaPhoto, BufferedInputFile
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.markdown import hbold

import db as db
import kb
from states import Registration, AuctionCreation, Bidding, AdminActions

# Загружаем переменные окружения
ADMIN_ID = os.getenv("ADMIN_ID")
ADMIN_IDS = os.getenv("ADMIN_IDS").split(",")
ADMIN_IDS = list(map(int, ADMIN_IDS))
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")


async def is_user_subscribed(bot: Bot, user_id: int) -> bool:
    """Проверяет подписку пользователя на канал."""
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        status = getattr(member, "status", None)
        return status in ("member", "administrator", "creator")
    except Exception as e:
        logging.warning(f"Не удалось проверить подписку пользователя {user_id}: {e}")
        return False


MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# Создаем роутер
router = Router()


# --- Middleware для предварительной обработки ---

# handlers.py

@router.message.middleware()
@router.callback_query.middleware()
async def user_status_middleware(handler, event, data):
    """
    Middleware для проверки статуса пользователя, подписки
    и обновления его username.
    """
    user: User = data.get('event_from_user')
    if not user:
        return await handler(event, data)

    # Админы имеют полный доступ всегда
    if int(user.id) in ADMIN_IDS:
        return await handler(event, data)

    # Обновляем username при каждом взаимодействии
    await db.update_user_username(user.id, user.username)

    # Пропускаем /start всегда (он сбрасывает FSM)
    if isinstance(event, Message) and event.text == "/start":
        return await handler(event, data)

    # Разрешаем любые активные FSM шаги (для регистрации)
    state: FSMContext = data.get('state')
    current_state = await state.get_state()
    if current_state and current_state.startswith("Registration:"):
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

    # --- ИСПРАВЛЕННЫЙ БЛОК ---
    if status is None:
        # Пользователь не зарегистрирован. Блокируем всё, кроме /start.
        if isinstance(event, Message):
            await event.answer("Вы не зарегистрированы. Пожалуйста, нажмите /start для начала работы.")
        elif isinstance(event, CallbackQuery):
            await event.answer("Вы не зарегистрированы. Пожалуйста, нажмите /start", show_alert=True)
        return  # Прерываем дальнейшую обработку
    # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    # Глобальная проверка подписки (кроме админа и кнопки проверки подписки)
    try:
        bot_inst: Bot = data.get("bot")
    except Exception:
        bot_inst = None

    allow_check = isinstance(event, CallbackQuery) and getattr(event, "data", None) and str(event.data).startswith(
        "check_sub")

    if bot_inst and not allow_check:
        try:
            subscribed = await is_user_subscribed(bot_inst, user.id)
        except Exception:
            subscribed = False

        if not subscribed:
            channel_url = f"httpsMusic://t.me/{CHANNEL_USERNAME}" if CHANNEL_USERNAME else None
            text = (
                "Для пользования ботом необходимо быть подписанным на наш канал.\n"
                "Подпишитесь и нажмите ‘Проверить подписку’."
            )
            try:
                # Пытаемся отредактировать текущее "меню"
                if isinstance(event, CallbackQuery) and event.message:
                    msg = event.message
                    kb_markup = kb.subscribe_keyboard(channel_url, 0)

                    if getattr(msg, "photo", None) or msg.caption is not None:
                        await bot_inst.edit_message_caption(
                            chat_id=msg.chat.id,
                            message_id=msg.message_id,
                            caption=text,
                            reply_markup=kb_markup
                        )
                    else:
                        await bot_inst.edit_message_text(
                            chat_id=msg.chat.id,
                            message_id=msg.message_id,
                            text=text,
                            reply_markup=kb_markup
                        )
                    try:
                        await event.answer("Подписка обязательна", show_alert=True)
                    except Exception:
                        pass
                else:
                    # Если это было сообщение (например, мусорный текст), просто отвечаем
                    await event.answer(text, reply_markup=kb.subscribe_keyboard(channel_url, 0))
            except Exception:
                # Фолбек: просто пытаемся отправить новое сообщение
                try:
                    chat_id = event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id
                    await bot_inst.send_message(chat_id, text, reply_markup=kb.subscribe_keyboard(channel_url, 0))
                except Exception:
                    pass
            return  # блокируем дальнейшую обработку

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


async def safe_delete_message(message: Message):
    """Безопасное удаление сообщения (игнорирует ошибки)."""
    try:
        await message.delete()
    except TelegramAPIError as e:
        # Игнорируем ошибки, если сообщение уже удалено или у бота нет прав
        logging.warning(f"Failed to delete message {message.message_id}: {e}")
        pass

async def send_temp_warning(bot: Bot, chat_id: int, text: str, duration_sec: int = 4):
    """
    Отправляет временное сообщение об ошибке.
    ПРИМЕЧАНИЕ: Автоудаление не реализовано,
    так как требует asyncio.sleep, что может блокировать обработку.
    Для инлайн-FSM лучше показывать ошибку в render_auction_creation_card.
    """
    try:
        # В идеале, нужно было бы редактировать основное меню FSM
        # Но для быстрой ошибки пойдет и это:
        await bot.send_message(chat_id, text, disable_notification=True)
        # В реальном проекте, мы бы не использовали эту функцию,
        # а передавали 'error_text' в render_auction_creation_card.
    except TelegramAPIError as e:
        logging.warning(f"Failed to send temp warning: {e}")
        pass



NAME_ALLOWED_RE = re.compile(r"^[A-Za-zА-Яа-яЁё\-\s]{2,100}$")


def clean_full_name(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def is_valid_full_name(s: str) -> bool:
    return bool(NAME_ALLOWED_RE.match(s))


def parse_amount(s: str) -> float:
    s = (s or "").strip().replace(" ", "").replace(",", ".")
    return float(s)


def csv_safe(s: str) -> str:
    s = s or ""
    return ("'" + s) if s[:1] in ("=", "+", "-", "@", "\t") else s


async def format_auction_post(auction_data: dict, bot: Bot, finished: bool = False) -> str:
    """Форматирует текст поста для канала (с историей ставок для азарта)."""
    last_bid = await db.get_last_bid(auction_data['auction_id'])
    safe_title = escape(auction_data.get('title') or "")
    safe_description = escape(auction_data.get('description') or "")
    bot_info = await bot.get_me()

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

    # Активный аукцион
    current_price = last_bid['bid_amount'] if last_bid else auction_data['start_price']
    leader_text = f"@{(last_bid['username'])}" if last_bid else "Ставок еще нет"
    end_time_dt = auction_data['end_time'].astimezone(MOSCOW_TZ)

    top_bids = await db.get_top_bids(auction_data['auction_id'], limit=5)
    history = ""
    if top_bids:
        lines = ["\n<b>🔥 Топ-5 ставок:</b>"]
        for i, b in enumerate(top_bids, start=1):
            user_disp = f"@{b['username']}" if b.get('username') else (b.get('full_name') or str(b['user_id']))
            lines.append(f"{i}) {b['bid_amount']:,.0f} ₽ — {user_disp}")
        history = "\n".join(lines)

    blitz_price_text = ""
    if auction_data.get('blitz_price'):
        blitz_price_text = f"⚡️ <b>Блиц-цена:</b> {auction_data['blitz_price']:,.2f} руб.\n\n"

    text = (
        f"💎 <b>{safe_title}</b>\n\n"
        f"{safe_description}\n\n"
        f"💰 <b>Текущая ставка:</b> {current_price:,.2f} руб.\n"
        f"👑 <b>Лидер:</b> {leader_text}\n"
        f"{blitz_price_text}"
        f"⏳ <b>Окончание:</b> {end_time_dt.strftime('%d.%m.%Y в %H:%M')} (МСК)\n"
        f"{history}\n\n"
        f"Для участия и ставок перейдите в нашего бота: @{bot_info.username}"
    )
    return text


async def find_user_by_text(text: str) -> int | None:
    """Вспомогательная функция для бана/разбана."""
    target_user_id = None
    text = text.strip()
    if text.startswith('@'):
        user = await db.get_user_by_username(text)
        if user:
            target_user_id = user['user_id']
    else:
        normalized = normalize_phone(text)
        if normalized != text or text.startswith('+'):
            user = await db.get_user_by_phone(normalized)
            if user:
                target_user_id = user['user_id']
        if target_user_id is None:
            try:
                target_user_id = int(text)
            except ValueError:
                pass
    return target_user_id


# --- 1. РЕГИСТРАЦИЯ ПОЛЬЗОВАТЕЛЕЙ (ИНЛАЙН FSM) ---

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    """
    Обработчик команды /start.
    Сбрасывает ЛЮБОЕ состояние и показывает главное меню или FSM регистрации.
    """

    # 1. Сбрасываем состояние
    current_state = await state.get_state()
    if current_state is not None:
        logging.info(f"User {message.from_user.id} used /start, clearing state {current_state}")
        await state.clear()

    # 2. Проверяем статус пользователя
    user_status = await db.get_user_status(message.from_user.id)

    # 3. Определяем, что показать
    if int(message.from_user.id) in ADMIN_IDS:
        await message.answer("Добро пожаловать в аукцион! (Админ)", reply_markup=kb.get_main_menu_admin())
    elif user_status == 'banned':
        await message.answer("Ваш доступ к боту заблокирован.")
    elif user_status == 'pending':
        await message.answer("Ваша заявка на регистрацию находится на рассмотрении.")
    elif user_status == 'approved':
        await message.answer("Добро пожаловать в аукцион!", reply_markup=kb.get_main_menu())
    else:
        # 4. НАЧАЛО FSM РЕГИСТРАЦИИ (Новый флоу)
        await state.set_state(Registration.waiting_for_full_name)

        # Отправляем первое сообщение, ID которого будем хранить
        menu_msg = await message.answer(
            "Здравствуйте! Для участия в аукционе, пожалуйста, зарегистрируйтесь.\n\n"
            f"{hbold('Введите ваше ФИО:')}",
            parse_mode="HTML",
            reply_markup=kb.cancel_fsm_keyboard("back_to_menu")  # Кнопка "Отмена"
        )
        # Сохраняем ID сообщения для FSM
        await state.update_data(menu_message_id=menu_msg.message_id)


@router.message(StateFilter(Registration.waiting_for_full_name), F.text)
async def process_full_name(message: Message, state: FSMContext, bot: Bot):
    """Ловит ФИО, удаляет сообщение, редактирует меню."""
    try:
        await message.delete()  # Удаляем сообщение пользователя
    except TelegramAPIError:
        pass

    name = clean_full_name(message.text)
    if not is_valid_full_name(name):
        # Временно уведомим об ошибке
        try:
            temp_msg = await message.answer("Ошибка: Введите корректное ФИО (2–100 символов, только буквы и пробелы).")
            # TODO: Добавить логику автоудаления этого сообщения
        except Exception:
            pass
        return

    await state.update_data(full_name=name)
    await state.set_state(Registration.waiting_for_phone)

    data = await state.get_data()
    menu_message_id = data.get('menu_message_id')

    try:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=menu_message_id,
            text=(
                f"✅ ФИО: {escape(name)}\n\n"
                f"{hbold('Отлично! Теперь, пожалуйста, введите ваш номер телефона в формате +7XXXXXXXXXX')}\n\n"
                "(Вы также можете прикрепить свой контакт, нажав 'скрепку' 📎 и выбрав 'Контакт')"
            ),
            parse_mode="HTML",
            reply_markup=kb.cancel_fsm_keyboard("back_to_menu")  # Кнопка "Отмена"
        )
    except TelegramAPIError:
        logging.warning("Не удалось отредактировать сообщение FSM регистрации (step 1)")


async def complete_registration(message: Message, state: FSMContext, bot: Bot, phone_number: str):
    """Общая функция для завершения регистрации (т.к. 2 хэндлера)."""
    try:
        await message.delete()  # Удаляем сообщение пользователя (текст или контакт)
    except TelegramAPIError:
        pass

    data = await state.get_data()
    menu_message_id = data.get('menu_message_id')
    full_name = data.get('full_name')

    # Проверка на дубликат
    existing = await db.get_user_by_phone(phone_number)
    if existing and existing.get('user_id') != message.from_user.id:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=menu_message_id,
                text=(
                    f"✅ ФИО: {escape(full_name)}\n"
                    f"❌ Телефон: {escape(phone_number)}\n\n"
                    f"Этот номер уже используется другим пользователем. {hbold('Введите другой номер')}.",
                ),
                parse_mode="HTML",
                reply_markup=kb.cancel_fsm_keyboard("back_to_menu")
            )
        except TelegramAPIError:
            pass
        return

    # Сохраняем заявку
    await db.add_user_request(
        user_id=message.from_user.id,
        username=message.from_user.username,
        full_name=full_name,
        phone_number=phone_number
    )

    # Редактируем меню FSM в последний раз
    try:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=menu_message_id,
            text="✅ Спасибо! Ваша заявка отправлена на модерацию. Ожидайте подтверждения.",
            reply_markup=None  # Убираем кнопки
        )
    except TelegramAPIError:
        pass  # Сообщение могло быть удалено

    await state.clear()

    # Уведомляем админа
    try:
        await bot.send_message(
            int(ADMIN_CHAT_ID),
            f"Новая заявка на регистрацию:\n\n"
            f"ID: <code>{message.from_user.id}</code>\n"
            f"Username: @{escape(message.from_user.username or '')}\n"
            f"ФИО: {escape(full_name or '')}\n"
            f"Телефон: <code>{escape(phone_number)}</code>",
            parse_mode="HTML",
            reply_markup=kb.admin_approval_keyboard(message.from_user.id)
        )
    except Exception as e:
        logging.error(f"Не удалось отправить заявку админу: {e}")


@router.message(StateFilter(Registration.waiting_for_phone), F.contact)
async def process_phone_contact(message: Message, state: FSMContext, bot: Bot):
    """Обработка контакта (прикрепленного)."""
    phone_number = normalize_phone(message.contact.phone_number)
    if not re.fullmatch(r"\+7\d{10}", phone_number or ""):
        data = await state.get_data()
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=data.get('menu_message_id'),
                text="Некорректный номер. Укажите номер в формате +7XXXXXXXXXX.",
                reply_markup=kb.cancel_fsm_keyboard("back_to_menu")
            )
        except TelegramAPIError:
            pass
        return

    await complete_registration(message, state, bot, phone_number)


@router.message(StateFilter(Registration.waiting_for_phone), F.text)
async def process_phone_text(message: Message, state: FSMContext, bot: Bot):
    """Обработка номера (текстом)."""
    phone_number = normalize_phone(message.text)
    if not re.fullmatch(r"\+7\d{10}", phone_number or ""):
        data = await state.get_data()
        try:
            # Редактируем меню FSM, чтобы показать ошибку
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=data.get('menu_message_id'),
                text=(
                    f"✅ ФИО: {escape(data.get('full_name'))}\n\n"
                    f"Некорректный номер. {hbold('Укажите номер в формате +7XXXXXXXXXX.')}"
                ),
                parse_mode="HTML",
                reply_markup=kb.cancel_fsm_keyboard("back_to_menu")
            )
            await message.delete()  # Удаляем неверный ввод
        except TelegramAPIError:
            pass
        return

    await complete_registration(message, state, bot, phone_number)


# --- 2. МОДЕРАЦИЯ ЗАЯВОК (АДМИН) (ИНЛАЙН FSM) ---

@router.callback_query(F.data.startswith("approve_user_"))
async def approve_user(callback: CallbackQuery, bot: Bot):
    """Одобрение заявки пользователя."""
    if int(callback.from_user.id) not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)

    try:
        user_id = int(callback.data.split("_")[2])
    except Exception:
        return await callback.answer("Некорректный идентификатор", show_alert=True)

    await db.update_user_status(user_id, 'approved')
    await callback.message.edit_text(f"✅ Пользователь {user_id} одобрен.")

    try:
        # Отправляем НОВОЕ сообщение-меню пользователю
        await bot.send_message(user_id, "Ваша заявка одобрена! Теперь вы можете участвовать в аукционах.",
                               reply_markup=kb.get_main_menu())
    except TelegramAPIError as e:
        logging.error(f"Не удалось уведомить пользователя {user_id} об одобрении: {e}")
    await callback.answer()


@router.callback_query(F.data.startswith("decline_user_"))
async def decline_user(callback: CallbackQuery, state: FSMContext):
    """Отклонение заявки: FSM для причины (НОВЫЙ ФЛОУ)"""
    if int(callback.from_user.id) not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)

    try:
        target_user_id = int(callback.data.split("_")[2])
    except Exception:
        return await callback.answer("Некорректный идентификатор", show_alert=True)

    await state.set_state(AdminActions.waiting_for_decline_reason)
    await state.update_data(
        target_user_id=target_user_id,
        menu_message_id=callback.message.message_id  # Запоминаем ID админ-сообщения
    )

    await callback.bot.edit_message_text(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        text=(
            f"Отклонение пользователя <code>{target_user_id}</code>.\n"
            f"{hbold('Введите причину отклонения (опционально).')}\n"
            "Отправьте ‘-’ или ‘0’, чтобы отклонить без причины."
        ),
        parse_mode="HTML",
        reply_markup=kb.cancel_fsm_keyboard("admin_menu")  # Кнопка Назад -> в админ меню
    )
    await callback.answer()


@router.message(StateFilter(AdminActions.waiting_for_decline_reason))
async def decline_reason_process(message: Message, state: FSMContext, bot: Bot):
    """Ловит причину, удаляет, редактирует админ-меню."""
    try:
        await message.delete()
    except TelegramAPIError:
        pass

    data = await state.get_data()
    target_user_id = data.get('target_user_id')
    menu_message_id = data.get('menu_message_id')
    reason = (message.text or '').strip()
    no_reason = (reason in ('-', '0', ''))

    await db.update_user_status(target_user_id, 'banned')

    notify_text = "К сожалению, ваша заявка на регистрацию была отклонена."
    if not no_reason:
        notify_text += f"\nПричина: {reason}"

    try:
        await bot.send_message(target_user_id, notify_text)
    except TelegramAPIError as e:
        logging.error(f"Не удалось уведомить пользователя {target_user_id} об отклонении: {e}")

    # Возвращаем админа в админ-меню
    await state.clear()
    try:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=menu_message_id,
            text=f"❌ Заявка пользователя {target_user_id} отклонена.",
            reply_markup=kb.admin_menu_keyboard()
        )
    except TelegramAPIError:
        await message.answer("❌ Заявка отклонена.", reply_markup=kb.admin_menu_keyboard())


# --- 3. ГЛАВНОЕ МЕНЮ И ПРОСМОТР АУКЦИОНА ---

@router.callback_query(F.data == "menu_current")
async def menu_current(callback: CallbackQuery, bot: Bot):
    auction = await db.get_active_auction()
    if not auction:
        await bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            text="На данный момент активных аукционов нет.",
            reply_markup=kb.back_to_menu_keyboard()
        )
        await callback.answer()
        return

    text = await format_auction_post(auction, bot)

    # Пытаемся отредактировать. Если было текст-меню, упадет.
    try:
        await bot.edit_message_media(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            media=InputMediaPhoto(media=auction['photo_id'], caption=text, parse_mode="HTML"),
            reply_markup=kb.get_auction_keyboard(auction['auction_id'], auction['blitz_price'])
        )
    except TelegramAPIError as e:
        # Ошибка (например, editing text to media). Удаляем старое, шлем новое.
        logging.warning(f"Failed to edit to media: {e}. Re-sending message.")
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass

        await callback.message.answer_photo(
            photo=auction['photo_id'],
            caption=text,
            parse_mode="HTML",
            reply_markup=kb.get_auction_keyboard(auction['auction_id'], auction['blitz_price'])
        )
    await callback.answer()


@router.callback_query(F.data == "menu_all")
async def menu_all(callback: CallbackQuery, bot: Bot):
    await render_all_auctions_page(callback, bot, page=1)


@router.callback_query(F.data.startswith("all_page_"))
async def menu_all_page(callback: CallbackQuery, bot: Bot):
    try:
        page = int(callback.data.split("_")[-1])
        if page < 1:
            page = 1
    except Exception:
        page = 1
    await render_all_auctions_page(callback, bot, page=page)


async def render_all_auctions_page(callback: CallbackQuery, bot: Bot, page: int, page_size: int = 5):
    total = await db.count_auctions()
    if total == 0:
        text = "Пока аукционов нет."
        kb_markup = kb.back_to_menu_keyboard()
    else:
        offset = (page - 1) * page_size
        auctions = await db.get_auctions_page(limit=page_size, offset=offset)
        lines = []
        for a in auctions:
            status = a['status']
            prefix = "🟢 Активен" if status == 'active' else ("🏁 Завершен" if status == 'finished' else status)
            if status == 'active':
                last = await db.get_last_bid(a['auction_id'])
                price = last['bid_amount'] if last else a['start_price']
                ends = a['end_time'].astimezone(MOSCOW_TZ).strftime('%d.%m.%Y %H:%M')
                lines.append(f"{prefix}: «{a['title']}» — {price:,.0f} ₽ (до {ends})")
            else:
                final = a.get('final_price')
                price_txt = f"{final:,.0f} ₽" if final is not None else "—"
                lines.append(f"{prefix}: «{a['title']}» — {price_txt}")
        text = "\n".join(lines) if lines else "Пока аукционов нет."
        kb_markup = kb.auctions_pagination_keyboard(page=page, total=total, page_size=page_size)

    try:
        await callback.bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            text=text,
            reply_markup=kb_markup
        )
    except TelegramAPIError:
        pass
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "menu_contact")
async def menu_contact(callback: CallbackQuery):
    admin_username = "CoId_Siemens"  # TODO: Вынести в .env
    await callback.bot.edit_message_text(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        text=f"По всем вопросам вы можете написать нашему администратору: @{admin_username}",
        reply_markup=kb.back_to_menu_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """
    Универсальный обработчик "Назад в меню",
    сбрасывает FSM и редактирует сообщение.
    """

    # 1. Сброс FSM
    await state.clear()

    # 2. Определение клавиатуры
    keyboard = kb.get_main_menu_admin() if int(callback.from_user.id) in ADMIN_IDS else kb.get_main_menu()
    text = "Добро пожаловать в аукцион!"

    # 3. Пытаемся отредактировать в текст.
    try:
        await bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            text=text,
            reply_markup=keyboard
        )
    except TelegramAPIError as e:
        # Если не вышло (было фото), удаляем старое и шлем новое
        logging.warning(f"Failed to edit to text menu: {e}. Re-sending message.")
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass

        await callback.message.answer(text, reply_markup=keyboard)

    await callback.answer()


@router.callback_query(F.data == "admin_menu")
async def admin_menu(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Возврат в админ-меню (также сбрасывает FSM)."""
    if int(callback.from_user.id) not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)

    # Сбрасываем FSM на всякий случай, если админ был в процессе
    await state.clear()

    text = "Админ-панель: выберите действие"
    kb_markup = kb.admin_menu_keyboard()

    try:
        await bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            text=text,
            reply_markup=kb_markup
        )
    except TelegramAPIError:
        # Если было фото, удаляем
        try:
            await callback.message.delete()
        except TelegramAPIError:
            pass
        await callback.message.answer(text, reply_markup=kb_markup)

    await callback.answer()


@router.callback_query(F.data == "admin_finish")
async def admin_finish(callback: CallbackQuery, bot: Bot):
    if int(callback.from_user.id) not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)

    active = await db.get_active_auction()
    if not active:
        await callback.answer("Нет активного аукциона", show_alert=True)
        return

    top_bids = await db.get_top_bids(active['auction_id'], limit=5)

    try:
        await callback.bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            text=f"Выберите победителя для аукциона: \n\n«{active['title']}»",
            reply_markup=kb.admin_select_winner_keyboard(top_bids)
        )
    except TelegramAPIError:
        pass
    await callback.answer()


@router.callback_query(F.data == "admin_winner_none")
async def admin_winner_none(callback: CallbackQuery, bot: Bot):
    if int(callback.from_user.id) not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)
    active = await db.get_active_auction()
    if not active:
        return await callback.answer("Нет активного аукциона", show_alert=True)
    await db.finish_auction(active['auction_id'], None, None)
    finished_post_text = await format_auction_post(active, bot, finished=True)
    try:
        await bot.edit_message_caption(
            chat_id=CHANNEL_ID,
            message_id=active['channel_message_id'],
            caption=finished_post_text,
            parse_mode="HTML",
            reply_markup=None
        )
    except TelegramAPIError as e:
        logging.error(f"Не удалось обновить пост в канале после завершения без победителя: {e}")
    await callback.message.edit_text("Аукцион завершён без победителя.", reply_markup=kb.admin_menu_keyboard())
    await callback.answer("Аукцион закрыт", show_alert=True)


@router.callback_query(F.data.startswith("admin_winner_bid_"))
async def admin_winner_bid(callback: CallbackQuery, bot: Bot):
    if int(callback.from_user.id) not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)
    try:
        bid_id = int(callback.data.split("_")[-1])
    except Exception:
        return await callback.answer("Некорректный выбор", show_alert=True)
    bid = await db.get_bid_by_id(bid_id)
    if not bid:
        return await callback.answer("Ставка не найдена", show_alert=True)
    active = await db.get_active_auction()
    if not active or active['auction_id'] != bid['auction_id']:
        return await callback.answer("Аукцион уже не активен", show_alert=True)

    await db.finish_auction(active['auction_id'], bid['user_id'], bid['bid_amount'])
    finished_post_text = await format_auction_post(active, bot, finished=True)
    try:
        await bot.edit_message_caption(
            chat_id=CHANNEL_ID,
            message_id=active['channel_message_id'],
            caption=finished_post_text,
            parse_mode="HTML",
            reply_markup=None
        )
    except TelegramAPIError as e:
        logging.error(f"Не удалось обновить пост в канале после выбора победителя: {e}")
    try:
        await bot.send_message(
            bid['user_id'],
            f"🎉 Поздравляем! Вы победили в аукционе «{active['title']}». Ваша ставка: {bid['bid_amount']:,.2f} руб."
        )
    except TelegramAPIError as e:
        logging.warning(f"Не удалось уведомить победителя {bid['user_id']}: {e}")
    await callback.message.edit_text(
        f"Аукцион завершён. Победитель: {bid.get('username') or bid.get('full_name') or bid['user_id']} за {bid['bid_amount']:,.2f} руб.",
        reply_markup=kb.admin_menu_keyboard()
    )
    await callback.answer("Аукцион закрыт", show_alert=True)


# --- 4. ЛОГИКА СТАВОК (ИНЛАЙН FSM) ---

@router.callback_query(F.data.startswith("bid_auction_"))
async def make_bid_start(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Начало процесса ставки (FSM)."""
    auction_id = int(callback.data.split("_")[2])
    auction = await db.get_active_auction()

    if not auction or auction['auction_id'] != auction_id:
        await callback.answer("Аукцион уже завершен или неактивен.", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        return

    # Проверка подписки (уже в middleware, но дублируем для кнопки "Проверить")
    if not await is_user_subscribed(bot, callback.from_user.id):
        channel_url = f"https://t.me/{CHANNEL_USERNAME}" if CHANNEL_USERNAME else None
        try:
            await callback.bot.edit_message_caption(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                caption=(
                    "Для участия в аукционе необходимо быть подписанным на наш канал.\n"
                    "Подпишитесь и нажмите ‘Проверить подписку’."
                ),
                reply_markup=kb.subscribe_keyboard(channel_url, auction_id)
            )
        except TelegramAPIError:
            pass
        await callback.answer("Подпишитесь на канал, затем нажмите ‘Проверить’", show_alert=True)
        return

    # Проверка интервала (cooldown)
    end_time_dt = auction['end_time']
    time_to_end = end_time_dt - datetime.now(end_time_dt.tzinfo)
    cooldown_off_before_end = int(auction.get('cooldown_off_before_end_minutes') or 0)
    cooldown_minutes = int(auction.get('cooldown_minutes') or 0)

    if cooldown_minutes > 0 and time_to_end > timedelta(minutes=cooldown_off_before_end):
        last_bid_time = await db.get_user_last_bid_time(callback.from_user.id, auction_id)
        if last_bid_time:
            elapsed = datetime.now(last_bid_time.tzinfo) - last_bid_time
            if elapsed < timedelta(minutes=cooldown_minutes):
                remaining_time = timedelta(minutes=cooldown_minutes) - elapsed
                await callback.answer(
                    f"Следующую ставку можно сделать через {max(1, remaining_time.seconds // 60)} мин.", show_alert=True
                )
                return

    # Входим в FSM для ввода ставки
    await state.set_state(Bidding.waiting_for_bid_amount)
    # Запоминаем ID сообщения с карточкой аукциона
    await state.update_data(
        auction_id=auction_id,
        menu_message_id=callback.message.message_id
    )

    last_bid = await db.get_last_bid(auction_id)
    current_price = last_bid['bid_amount'] if last_bid else auction['start_price']

    # Редактируем карточку, запрашивая ставку
    await callback.bot.edit_message_caption(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        caption=(
            f"Текущая ставка: {current_price:,.0f} руб.\n"
            f"Минимальный шаг: {auction['min_step']:,.0f} руб.\n\n"
            f"{hbold('Введите вашу ставку (число):')}"
        ),
        parse_mode="HTML",
        # Добавляем кнопку "Отмена" (возврат к карточке)
        reply_markup=kb.cancel_fsm_keyboard(f"show_auction_{auction_id}")
    )
    await callback.answer()


@router.callback_query(F.data.startswith("show_auction_"))
async def show_auction_card(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """
    Возвращает карточку аукциона (используется для "Отмены" из FSM ставки).
    """
    await state.clear()  # Выходим из FSM
    auction_id_str = callback.data.split("_")[2]
    auction_id = int(auction_id_str)

    # Пытаемся получить аукцион по ID. Если неактивен, то get_active_auction() вернет None
    auction = await db.get_active_auction()
    if not auction or auction['auction_id'] != auction_id:
        # Если аукцион кончился, пока мы были в FSM
        return await back_to_menu(callback, state, bot)

    text = await format_auction_post(auction, bot)
    await bot.edit_message_caption(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        caption=text,
        parse_mode="HTML",
        reply_markup=kb.get_auction_keyboard(auction['auction_id'], auction['blitz_price'])
    )
    await callback.answer()


@router.callback_query(F.data == "check_sub")
async def check_subscription_generic(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """Глобальная проверка (меню)."""
    if await is_user_subscribed(bot, callback.from_user.id):
        await back_to_menu(callback, state, bot)  # Переходим в главное меню
        await callback.answer("Подписка подтверждена", show_alert=True)
    else:
        await callback.answer("Вы ещё не подписаны на канал", show_alert=True)


@router.callback_query(F.data.startswith("check_sub_"))
async def check_subscription_auction(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """Проверка подписки (карточка аукциона)."""
    if await is_user_subscribed(bot, callback.from_user.id):
        # Возвращаем на карточку аукциона
        auction_id_str = callback.data.split("_")[1]
        if auction_id_str == "0":
            return await check_subscription_generic(callback, bot, state)

        await show_auction_card(callback, state, bot)  # Имитируем нажатие "Отмена"
        await callback.answer("Подписка подтверждена", show_alert=True)
    else:
        await callback.answer("Вы ещё не подписаны на канал", show_alert=True)


@router.callback_query(F.data.startswith("blitz_auction_"))
async def blitz_buy(callback: CallbackQuery, bot: Bot, state: FSMContext):
    """Покупка по блиц-цене."""
    await state.clear()  # На случай, если пользователь был в FSM ставки

    auction_id = int(callback.data.split("_")[2])
    auction = await db.get_active_auction()

    if not auction or auction['auction_id'] != auction_id:
        await callback.answer("Аукцион уже завершен или неактивен.", show_alert=True)
        return

    blitz_price = auction.get('blitz_price')
    if not blitz_price:
        await callback.answer("Блиц-цена недоступна для этого лота.", show_alert=True)
        return

    await db.add_bid(auction_id, callback.from_user.id, blitz_price)
    await db.finish_auction(auction_id, callback.from_user.id, blitz_price)
    finished_post_text = await format_auction_post(auction, bot, finished=True)

    try:
        await bot.edit_message_caption(
            chat_id=CHANNEL_ID,
            message_id=auction['channel_message_id'],
            caption=finished_post_text,
            parse_mode="HTML",
            reply_markup=None
        )
    except TelegramAPIError as e:
        logging.warning(f"Не удалось обновить пост в канале после блиц-покупки: {e}")

    try:
        await callback.bot.edit_message_caption(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            caption=finished_post_text,
            parse_mode="HTML",
            reply_markup=kb.back_to_menu_keyboard()  # Заменено None на кнопку "Назад"
        )
    except TelegramAPIError as e:
        logging.warning(f"Не удалось обновить приватную карточку после блиц-покупки: {e}")

    try:
        await bot.send_message(
            callback.from_user.id,
            f"🎉 Поздравляем! Вы купили лот «{(auction['title'])}» по блиц-цене {blitz_price:,.2f} руб.\n\n"
            f"В ближайшее время с вами свяжется администратор."
        )
    except TelegramAPIError:
        pass
    await callback.answer("Покупка по блиц-цене оформлена!", show_alert=True)


@router.message(StateFilter(Bidding.waiting_for_bid_amount), F.text)
async def process_bid_amount(message: Message, state: FSMContext, bot: Bot):
    """Обработка введенной суммы ставки (ИНЛАЙН FSM)."""

    try:
        await message.delete()  # 1. Удаляем сообщение пользователя
    except TelegramAPIError:
        pass

    data = await state.get_data()
    menu_message_id = data.get('menu_message_id')
    auction_id = data.get('auction_id')

    auction = await db.get_active_auction()

    if not auction or auction['auction_id'] != auction_id:
        await state.clear()
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=menu_message_id,
                text="Аукцион завершился, пока вы делали ставку.",
                reply_markup=kb.back_to_menu_keyboard()
            )
        except TelegramAPIError:
            pass
        return

    # Проверка формата
    try:
        bid_amount = parse_amount(message.text)
        if bid_amount <= 0: raise ValueError
    except ValueError:
        # Пере-редактируем меню с ошибкой
        last_bid = await db.get_last_bid(auction_id)
        current_price = last_bid['bid_amount'] if last_bid else auction['start_price']
        try:
            await bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=menu_message_id,
                caption=(
                    f"Текущая ставка: {current_price:,.0f} руб.\n"
                    f"Минимальный шаг: {auction['min_step']:,.0f} руб.\n\n"
                    f"{hbold('Ошибка! Введите числовое значение (например: 150000).')}"
                ),
                parse_mode="HTML",
                reply_markup=kb.cancel_fsm_keyboard(f"show_auction_{auction_id}")
            )
        except TelegramAPIError:
            pass
        return

    last_bid = await db.get_last_bid(auction['auction_id'])
    current_price = last_bid['bid_amount'] if last_bid else auction['start_price']

    # Блиц-покупка
    blitz_price = auction.get('blitz_price')
    if blitz_price and bid_amount >= blitz_price:
        await state.clear()  # Выходим из FSM
        # Имитируем нажатие кнопки, чтобы не дублировать код
        fake_callback_query = types.CallbackQuery(
            id="fake_blitz",
            from_user=message.from_user,
            chat_instance="fake",
            message=types.Message(message_id=menu_message_id, chat=message.chat, date=datetime.now()),
            data=f"blitz_auction_{auction_id}"
        )
        # У `blitz_buy` свой `await state.clear()`, так что это безопасно
        await blitz_buy(fake_callback_query, bot, state)
        return

    # Проверка минимальной ставки
    if bid_amount < current_price + auction['min_step']:
        try:
            min_bid_value = current_price + auction['min_step']
            error_text = f"Ошибка! Ваша ставка должна быть как минимум {min_bid_value:,.0f} руб."
            await bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=menu_message_id,
                caption=(
                    f"Текущая ставка: {current_price:,.0f} руб.\n"
                    f"Минимальный шаг: {auction['min_step']:,.0f} руб.\n\n"
                    f"{hbold(error_text)}"
                ),
                parse_mode="HTML",
                reply_markup=kb.cancel_fsm_keyboard(f"show_auction_{auction_id}")
            )
        except TelegramAPIError:
            pass
        return

    # --- Ставка принята ---
    await state.clear()  # 2. Выходим из FSM

    previous_leader = last_bid['user_id'] if last_bid else None
    await db.add_bid(auction['auction_id'], message.from_user.id, bid_amount)

    # 3. Антиснайпинг
    try:
        end_dt = auction['end_time']
        now_dt = datetime.now(end_dt.tzinfo)
        if (end_dt - now_dt) <= timedelta(minutes=2):
            new_end = end_dt + timedelta(minutes=2)
            await db.update_auction_end_time(auction['auction_id'], new_end)
            auction = await db.get_active_auction()  # Обновляем данные
    except Exception as e:
        logging.warning(f"Антиснайпинг не сработал: {e}")

    # 4. Уведомляем предыдущего лидера
    if previous_leader and previous_leader != message.from_user.id:
        try:
            await bot.send_message(previous_leader,
                                   f"❗️ Вашу ставку на аукционе '{auction['title']}' перебили! Новая ставка: {bid_amount:,.0f} руб.")
        except TelegramAPIError as e:
            logging.warning(f"Не удалось уведомить пользователя {previous_leader}: {e}")

    # 5. Обновляем главный пост в канале
    new_text_channel = await format_auction_post(auction, bot)
    try:
        await bot.edit_message_caption(
            chat_id=CHANNEL_ID,
            message_id=auction['channel_message_id'],
            caption=new_text_channel,
            parse_mode="HTML"
        )
    except TelegramAPIError as e:
        logging.error(f"Не удалось обновить пост в канале {CHANNEL_ID}: {e}")

    # 6. Обновляем приватную карточку (бывшее FSM-меню)
    # Добавляем плашку об успехе
    new_text_private = f"✅ Ваша ставка: {bid_amount:,.0f} руб.\n\n" + new_text_channel
    try:
        await bot.edit_message_caption(
            chat_id=message.chat.id,
            message_id=menu_message_id,
            caption=new_text_private,
            parse_mode="HTML",
            reply_markup=kb.get_auction_keyboard(auction['auction_id'], auction['blitz_price'])
        )
    except TelegramAPIError as e:
        logging.warning(f"Не удалось обновить приватную карточку для {message.chat.id}: {e}")


# --- 5. АДМИН-ПАНЕЛЬ (ИНЛАЙН FSM) ---

# handlers.py

@router.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def admin_panel_command(message: Message, state: FSMContext, bot: Bot):
    """Инлайн админ-меню (вызывается текстом)."""
    await safe_delete_message(message)

    # Сбрасываем FSM
    await state.clear()

    # Отправляем НОВОЕ меню
    await message.answer("Админ-панель: выберите действие", reply_markup=kb.admin_menu_keyboard())


@router.callback_query(F.data == "admin_ban")
async def admin_ban_start(callback: CallbackQuery, state: FSMContext):
    if int(callback.from_user.id) not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)
    await state.set_state(AdminActions.waiting_for_ban_id)
    await state.update_data(menu_message_id=callback.message.message_id)

    await callback.bot.edit_message_text(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        text=f"{hbold('Введите ID / @username / телефон пользователя для БАНА:')}",
        parse_mode="HTML",
        reply_markup=kb.cancel_fsm_keyboard("admin_menu")
    )
    await callback.answer()


@router.callback_query(F.data == "admin_unban")
async def admin_unban_start(callback: CallbackQuery, state: FSMContext):
    if int(callback.from_user.id) not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)
    await state.set_state(AdminActions.waiting_for_unban_id)
    await state.update_data(menu_message_id=callback.message.message_id)

    await callback.bot.edit_message_text(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        text=f"{hbold('Введите ID / @username / телефон пользователя для РАЗБАНА:')}",
        parse_mode="HTML",
        reply_markup=kb.cancel_fsm_keyboard("admin_menu")
    )
    await callback.answer()


@router.message(StateFilter(AdminActions.waiting_for_ban_id), F.from_user.id.in_(ADMIN_IDS))
async def admin_ban_handle(message: Message, state: FSMContext, bot: Bot):
    try:
        await message.delete()
    except TelegramAPIError:
        pass

    data = await state.get_data()
    menu_message_id = data.get('menu_message_id')

    target_user_id = await find_user_by_text(message.text)

    if target_user_id is None:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=menu_message_id,
                text=f"❌ Пользователь не найден.\n{hbold('Введите ID / @username / телефон:')}",
                parse_mode="HTML",
                reply_markup=kb.cancel_fsm_keyboard("admin_menu")
            )
        except TelegramAPIError:
            pass
        return

    await db.update_user_status(target_user_id, 'banned')
    await state.clear()

    try:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=menu_message_id,
            text=f"✅ Пользователь {target_user_id} забанен.",
            reply_markup=kb.admin_menu_keyboard()
        )
    except TelegramAPIError:
        pass


@router.message(StateFilter(AdminActions.waiting_for_unban_id), F.from_user.id.in_(ADMIN_IDS))
async def admin_unban_handle(message: Message, state: FSMContext, bot: Bot):
    try:
        await message.delete()
    except TelegramAPIError:
        pass

    data = await state.get_data()
    menu_message_id = data.get('menu_message_id')

    target_user_id = await find_user_by_text(message.text)

    if target_user_id is None:
        try:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=menu_message_id,
                text=f"❌ Пользователь не найден.\n{hbold('Введите ID / @username / телефон:')}",
                parse_mode="HTML",
                reply_markup=kb.cancel_fsm_keyboard("admin_menu")
            )
        except TelegramAPIError:
            pass
        return

    await db.update_user_status(target_user_id, 'approved')
    await state.clear()

    try:
        await bot.edit_message_text(
            chat_id=message.chat.id,
            message_id=menu_message_id,
            text=f"✅ Пользователь {target_user_id} разбанен.",
            reply_markup=kb.admin_menu_keyboard()
        )
    except TelegramAPIError:
        pass


async def render_auction_creation_card(
        bot: Bot,
        chat_id: int,
        state: FSMContext,
        prompt: str,
        kb_override: types.InlineKeyboardMarkup = None
):
    """
    Обновляет "карточку" создаваемого лота.
    Автоматически обрабатывает переход от текста к фото.
    """
    data = await state.get_data()
    menu_message_id = data.get('menu_message_id')
    if not menu_message_id:
        logging.error(f"FSM (AuctionCreation) в {chat_id} потерял menu_message_id.")
        return

    title = escape(data.get('title', '...'))
    desc = escape(data.get('description', '...'))
    photo = data.get('photo', None)
    start_price = data.get('start_price', '...')
    min_step = data.get('min_step', '...')
    cooldown = data.get('cooldown_minutes', '...')
    cooldown_off = data.get('cooldown_off_before_end_minutes', '...')
    blitz = data.get('blitz_price', '...')
    end_time = data.get('end_time', '...')
    if isinstance(end_time, datetime):
        end_time = end_time.strftime("%d.%m.%Y %H:%M")

    text = (
        f"<b>--- Создание аукциона ---</b>\n\n"
        f"1. Название: <code>{title}</code>\n"
        f"2. Описание: <code>{escape(desc[:50])}...</code>\n"
        f"3. Фото: <code>{'✅ Загружено' if photo else '...'}</code>\n"
        f"4. Старт. цена: <code>{start_price}</code>\n"
        f"5. Мин. шаг: <code>{min_step}</code>\n"
        f"6. Кулдаун (мин): <code>{cooldown}</code>\n"
        f"7. Откл. кулдаун (мин): <code>{cooldown_off}</code>\n"
        f"8. Блиц-цена: <code>{blitz}</code>\n"
        f"9. Окончание: <code>{end_time}</code>\n\n"
        f"<b>{prompt}</b>"
    )

    # Клавиатура по умолчанию - "Отмена", если не передана другая
    kb_markup = kb_override if kb_override else kb.cancel_fsm_keyboard("admin_menu")
    is_photo_card = data.get('is_photo_card', False)

    try:
        if photo and is_photo_card:
            # Меню УЖЕ с фото, просто редактируем подпись
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=menu_message_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb_markup
            )
        elif photo and not is_photo_card:
            # Меню БЫЛО текстовым, конвертируем в фото
            await bot.delete_message(chat_id, menu_message_id)
            new_msg = await bot.send_photo(
                chat_id=chat_id,
                photo=photo,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb_markup
            )
            # Обновляем ID сообщения и флаг в FSM
            await state.update_data(
                menu_message_id=new_msg.message_id,
                is_photo_card=True
            )
        else:
            # Меню текстовое (фото еще нет)
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=menu_message_id,
                text=text,
                parse_mode="HTML",
                reply_markup=kb_markup
            )
    except TelegramAPIError as e:
        logging.error(f"Failed to render creation card: {e}. State: {await state.get_state()} Data: {data}")
        # Попытка спасения: если меню удалено, отправить новое
        if "message to edit not found" in str(e) or "message to delete not found" in str(e):
            await state.clear()
            await bot.send_message(chat_id, "Произошла ошибка, FSM сброшен.", reply_markup=kb.admin_menu_keyboard())


# НОВЫЙ ХЕЛПЕР
async def return_to_confirmation(bot: Bot, chat_id: int, state: FSMContext):
    """
    Вспомогательная функция.
    После редактирования поля возвращает на экран "Опубликовать...".
    """
    await state.update_data(editing=False)  # Снимаем флаг
    await state.set_state(AuctionCreation.waiting_for_confirmation)

    # Рендерим карточку + даем ей спец. клавиатуру
    await render_auction_creation_card(
        bot=bot,
        chat_id=chat_id,
        state=state,
        prompt="ПРОВЕРЬТЕ ДАННЫЕ. Готово к публикации.",
        kb_override=kb.admin_confirm_auction_keyboard()
    )


# --- FSM Хэндлеры создания аукциона ---

# 1. Вход в FSM
@router.callback_query(F.data == "admin_create")
async def admin_create_auction_start(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if int(callback.from_user.id) not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)

    active_auction = await db.get_active_auction()
    if active_auction:
        await callback.answer("Нельзя создать новый аукцион, пока не завершен предыдущий.", show_alert=True)
        return

    await state.set_state(AuctionCreation.waiting_for_title)
    # Инициализируем FSM
    await state.update_data(
        menu_message_id=callback.message.message_id,
        is_photo_card=False  # Меню пока текстовое
    )

    await render_auction_creation_card(
        bot=bot,
        chat_id=callback.message.chat.id,
        state=state,
        prompt="Шаг 1/9: Введите название лота:"
    )
    await callback.answer()


# 2. Ловим Название
@router.message(StateFilter(AuctionCreation.waiting_for_title), F.text)
async def process_auction_title(message: Message, state: FSMContext, bot: Bot):
    await safe_delete_message(message)
    title = (message.text or "").strip()
    if not title or len(title) > 120:
        # Показываем ошибку в меню
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt=f"{hbold('Ошибка: Название (1-120 симв).')} Попробуйте снова:"
        )
        return # Остаемся в этом же состоянии

    data = await state.get_data()
    await state.update_data(title=title)

    if data.get('editing', False):
        await return_to_confirmation(bot, message.chat.id, state)
    else:
        await state.set_state(AuctionCreation.waiting_for_description)
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt="Шаг 2/9: Введите описание лота:"
        )

# 3. Ловим Описание
@router.message(StateFilter(AuctionCreation.waiting_for_description), F.text)
async def process_auction_desc(message: Message, state: FSMContext, bot: Bot):
    await safe_delete_message(message)
    desc = (message.text or "").strip()
    if not desc or len(desc) > 3000:
        # Показываем ошибку в меню
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt=f"{hbold('Ошибка: Описание (1-3000 симв).')} Попробуйте снова:"
        )
        return

    data = await state.get_data()
    await state.update_data(description=desc)

    if data.get('editing', False):
        await return_to_confirmation(bot, message.chat.id, state)
    else:
        await state.set_state(AuctionCreation.waiting_for_photo)
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt="Шаг 3/9: Отправьте фотографию лота:"
        )


@router.message(StateFilter(AuctionCreation.waiting_for_photo), ~F.photo)
async def process_auction_wrong_photo(message: Message, state: FSMContext, bot: Bot):
    """Ловит не-фото сообщения на шаге ожидания фото."""
    await safe_delete_message(message)
    # Показываем ошибку в основном меню
    await render_auction_creation_card(
        bot=bot,
        chat_id=message.chat.id,
        state=state,
        prompt=f"{hbold('Ошибка: Пожалуйста, отправьте именно фотографию.')} Попробуйте снова:"
    )
    return # <-- Важно! Предотвращаем дальнейшую обработку

# 4. Ловим Фото (этот хэндлер остается без изменений в логике ошибки)
@router.message(StateFilter(AuctionCreation.waiting_for_photo), F.photo)
async def process_auction_photo(message: Message, state: FSMContext, bot: Bot):
    await safe_delete_message(message)
    data = await state.get_data()

    await state.update_data(photo=message.photo[-1].file_id)

    if data.get('editing', False):
        await return_to_confirmation(bot, message.chat.id, state)
    else:
        await state.set_state(AuctionCreation.waiting_for_start_price)
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt="Шаг 4/9: Введите начальную цену (число):"
        )

# 5. Ловим Стартовую цену
@router.message(StateFilter(AuctionCreation.waiting_for_start_price), F.text)
async def process_auction_start_price(message: Message, state: FSMContext, bot: Bot):
    await safe_delete_message(message)
    try:
        value = float(message.text)
        if value <= 0: raise ValueError("Price must be positive")
    except ValueError:
        # Показываем ошибку в меню
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt=f"{hbold('Ошибка: Цена должна быть числом > 0.')} Попробуйте снова:"
        )
        return

    data = await state.get_data()
    await state.update_data(start_price=value)

    if data.get('editing', False):
        await return_to_confirmation(bot, message.chat.id, state)
    else:
        await state.set_state(AuctionCreation.waiting_for_min_step)
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt="Шаг 5/9: Введите минимальный шаг (число):"
        )

# 6. Ловим Мин. шаг
@router.message(StateFilter(AuctionCreation.waiting_for_min_step), F.text)
async def process_auction_min_step(message: Message, state: FSMContext, bot: Bot):
    await safe_delete_message(message)
    try:
        min_step = float(message.text)
        if min_step <= 0: raise ValueError("Step must be positive")
    except ValueError:
        # Показываем ошибку в меню
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt=f"{hbold('Ошибка: Шаг должен быть числом > 0.')} Попробуйте снова:"
        )
        return

    data = await state.get_data()
    await state.update_data(min_step=min_step)

    if data.get('editing', False):
        await return_to_confirmation(bot, message.chat.id, state)
    else:
        await state.set_state(AuctionCreation.waiting_for_cooldown_minutes)
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt="Шаг 6/9: Ограничение м/у ставками (в минутах, 0 = нет):"
        )

# 7. Ловим Кулдаун
@router.message(StateFilter(AuctionCreation.waiting_for_cooldown_minutes), F.text)
async def process_auction_cooldown_minutes(message: Message, state: FSMContext, bot: Bot):
    await safe_delete_message(message)
    try:
        cooldown = int(message.text)
        if cooldown < 0: raise ValueError("Cooldown cannot be negative")
    except ValueError:
        # Показываем ошибку в меню
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt=f"{hbold('Ошибка: Введите целое число (0 или >).')} Попробуйте снова:"
        )
        return

    data = await state.get_data()
    await state.update_data(cooldown_minutes=cooldown)

    if data.get('editing', False):
        await return_to_confirmation(bot, message.chat.id, state)
    else:
        await state.set_state(AuctionCreation.waiting_for_cooldown_off_before_end)
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt="Шаг 7/9: За сколько минут до конца откл. кулдаун (0 = всегда вкл):"
        )

# 8. Ловим Откл. Кулдауна
@router.message(StateFilter(AuctionCreation.waiting_for_cooldown_off_before_end), F.text)
async def process_auction_cooldown_off(message: Message, state: FSMContext, bot: Bot):
    await safe_delete_message(message)
    try:
        threshold = int(message.text)
        if threshold < 0: raise ValueError("Threshold cannot be negative")
    except ValueError:
        # Показываем ошибку в меню
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt=f"{hbold('Ошибка: Введите целое число (0 или >).')} Попробуйте снова:"
        )
        return

    data = await state.get_data()
    await state.update_data(cooldown_off_before_end_minutes=threshold)

    if data.get('editing', False):
        await return_to_confirmation(bot, message.chat.id, state)
    else:
        await state.set_state(AuctionCreation.waiting_for_blitz_price)
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt="Шаг 8/9: Введите блиц-цену (0 = нет):"
        )

# 9. Ловим Блиц-цену
@router.message(StateFilter(AuctionCreation.waiting_for_blitz_price), F.text)
async def process_auction_blitz_price(message: Message, state: FSMContext, bot: Bot):
    await safe_delete_message(message)
    try:
        blitz_price = float(message.text)
        if blitz_price < 0: raise ValueError("Blitz price cannot be negative")
    except ValueError:
        # Показываем ошибку в меню
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt=f"{hbold('Ошибка: Введите число (0 или >).')} Попробуйте снова:"
        )
        return

    data = await state.get_data()
    start_price = float(data.get('start_price') or 0) # Use 0 if not set yet
    if blitz_price > 0 and start_price > 0 and blitz_price < start_price:
        # Показываем ошибку в меню
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt=f"{hbold('Ошибка: Блиц-цена д.б. >= стартовой.')} Попробуйте снова:"
        )
        return

    await state.update_data(blitz_price=blitz_price if blitz_price > 0 else None)

    if data.get('editing', False):
        await return_to_confirmation(bot, message.chat.id, state)
    else:
        await state.set_state(AuctionCreation.waiting_for_end_time)
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt="Шаг 9/9: Введите дату/время окончания (ДД.ММ.ГГГГ ЧЧ:ММ):"
        )

# 10. Ловим Дату и Показываем Подтверждение
@router.message(StateFilter(AuctionCreation.waiting_for_end_time), F.text)
async def process_auction_end_time(message: Message, state: FSMContext, bot: Bot):
    await safe_delete_message(message)
    error_prompt = None # Переменная для текста ошибки

    try:
        naive_end_time = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
        end_time = MOSCOW_TZ.localize(naive_end_time)
        now = datetime.now(MOSCOW_TZ)
        if end_time <= now:
            error_prompt = f"{hbold('Ошибка: Дата должна быть в будущем.')} Попробуйте снова:"
        elif end_time - now < timedelta(minutes=10):
            error_prompt = f"{hbold('Ошибка: Мин. длительность 10 минут.')} Попробуйте снова:"

    except ValueError:
        error_prompt = f"{hbold('Ошибка: Неверный формат (ДД.ММ.ГГГГ ЧЧ:ММ).')} Попробуйте снова:"

    if error_prompt:
        # Показываем ошибку в меню
        await render_auction_creation_card(
            bot=bot,
            chat_id=message.chat.id,
            state=state,
            prompt=error_prompt
        )
        return # Остаемся в этом состоянии

    # Ошибки нет, сохраняем и переходим к подтверждению
    await state.update_data(end_time=end_time)
    await return_to_confirmation(bot, message.chat.id, state)

# --- 7. ПОДТВЕРЖДЕНИЕ АУКЦИОНА (НОВЫЕ ХЭНДЛЕРЫ) ---

# handlers.py

@router.callback_query(F.data == "auction_post", StateFilter(AuctionCreation.waiting_for_confirmation))
async def confirm_auction_post(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Публикуем аукцион."""
    data = await state.get_data()
    menu_message_id = data.get('menu_message_id')

    # Проверка, что все поля заполнены (особенно фото)
    if not data.get('photo'):
        await callback.answer("Ошибка: Фотография не загружена.", show_alert=True)
        # Возвращаем на шаг "Редактировать", чтобы можно было добавить фото
        await confirm_auction_edit(callback, state, bot)
        return

    try:
        # Убираем кнопки, показываем загрузку
        if data.get('is_photo_card', False):
            await bot.edit_message_caption(
                chat_id=callback.message.chat.id,
                message_id=menu_message_id,
                caption="Публикация...",
                reply_markup=None
            )
        else:
            await bot.edit_message_text(
                "Публикация...",
                chat_id=callback.message.chat.id,
                message_id=menu_message_id,
                reply_markup=None
            )
    except TelegramAPIError as e:
        logging.warning(f"Failed to edit message during post confirmation: {e}")
        pass

    try:
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

        # --- ИСПРАВЛЕННЫЙ БЛОК ---
        # 1. Удаляем старое FSM-сообщение (которое "Публикация...")
        try:
            await bot.delete_message(
                chat_id=callback.message.chat.id,
                message_id=menu_message_id
            )
        except TelegramAPIError:
            pass  # Игнорируем, если уже удалено

        # 2. Отправляем НОВОЕ сообщение с админ-меню
        await bot.send_message(
            chat_id=callback.message.chat.id,
            text=f"✅ Аукцион «{data['title']}» успешно создан.",
            reply_markup=kb.admin_menu_keyboard()  # Возврат в админ-меню
        )
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---

    except Exception as e:
        logging.error(f"Ошибка создания аукциона: {e}")
        # --- ИСПРАВЛЕННЫЙ БЛОК ОШИБКИ ---
        try:
            await bot.delete_message(
                chat_id=callback.message.chat.id,
                message_id=menu_message_id
            )
        except TelegramAPIError:
            pass

        await bot.send_message(
            chat_id=callback.message.chat.id,
            text=f"❌ Ошибка при публикации: {e}",
            reply_markup=kb.admin_menu_keyboard()
        )
        # --- КОНЕЦ ИСПРАВЛЕНИЯ ---
    finally:
        await state.clear()
    await callback.answer()


@router.callback_query(F.data == "auction_cancel", StateFilter(AuctionCreation))
async def confirm_auction_cancel(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Отменяем создание (эквивалент admin_menu)."""
    await admin_menu(callback, state, bot)
    await callback.answer("Создание аукциона отменено")


@router.callback_query(F.data == "auction_edit", StateFilter(AuctionCreation.waiting_for_confirmation))
async def confirm_auction_edit(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Показываем меню выбора полей для редактирования."""
    await state.set_state(AuctionCreation.waiting_for_edit_choice)

    await render_auction_creation_card(
        bot=bot,
        chat_id=callback.message.chat.id,
        state=state,
        prompt="Какое поле вы хотите отредактировать?",
        kb_override=kb.admin_edit_auction_fields_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_field_"), StateFilter(AuctionCreation.waiting_for_edit_choice))
async def process_auction_edit_choice(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """
    Ловим выбор поля, устанавливаем нужный FSM state
    и флаг 'editing', чтобы вернуться к подтверждению.
    """
    field_to_state_map = {
        "title": (AuctionCreation.waiting_for_title, "Шаг 1: Введите новое название:"),
        "desc": (AuctionCreation.waiting_for_description, "Шаг 2: Введите новое описание:"),
        "photo": (AuctionCreation.waiting_for_photo, "Шаг 3: Отправьте новое фото:"),
        "price": (AuctionCreation.waiting_for_start_price, "Шаг 4: Введите новую старт. цену:"),
        "step": (AuctionCreation.waiting_for_min_step, "Шаг 5: Введите новый мин. шаг:"),
        "cooldown": (AuctionCreation.waiting_for_cooldown_minutes, "Шаг 6: Введите новый кулдаун:"),
        "cooldown_off": (AuctionCreation.waiting_for_cooldown_off_before_end,
                         "Шаг 7: Введите новое время откл. кулдауна:"),
        "blitz": (AuctionCreation.waiting_for_blitz_price, "Шаг 8: Введите новую блиц-цену:"),
        "time": (AuctionCreation.waiting_for_end_time, "Шаг 9: Введите новое время окончания:"),
    }

    field = callback.data.split("_")[-1]

    if field == "back":
        # Возвращаемся к экрану "Опубликовать / Редактировать"
        await return_to_confirmation(bot, callback.message.chat.id, state)
        await callback.answer()
        return

    if field in field_to_state_map:
        new_state, prompt = field_to_state_map[field]

        await state.set_state(new_state)
        await state.update_data(editing=True)  # Флаг, что мы редактируем

        await render_auction_creation_card(
            bot=bot,
            chat_id=callback.message.chat.id,
            state=state,
            prompt=prompt
        )

    await callback.answer()


# --- 8. ЭКСПОРТ (ИНЛАЙН ФЛОУ) ---

@router.callback_query(F.data == "admin_export_users")
async def admin_export_users(callback: CallbackQuery, bot: Bot):
    if int(callback.from_user.id) not in ADMIN_IDS:
        return await callback.answer("Нет доступа", show_alert=True)

    # 1. Редактируем меню -> "Загрузка"
    try:
        await callback.message.edit_text(
            "⏳ Генерирую экспорт... Пожалуйста, подождите.",
            reply_markup=None
        )
    except TelegramAPIError:
        pass

    rows = await db.get_users_with_bid_stats()

    # 2. Готовим CSV
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(["user_id", "username", "full_name", "phone_number", "status", "bids_count", "bids_sum"])
    for r in rows:
        writer.writerow([
            r.get('user_id'),
            csv_safe(("@" + r['username']) if r.get('username') else ''),
            csv_safe(r.get('full_name') or ''),
            csv_safe(r.get('phone_number') or ''),
            csv_safe(r.get('status') or ''),
            int(r.get('bids_count') or 0),
            float(r.get('bids_sum') or 0.0),
        ])

    content_text = output.getvalue()
    output.close()
    content_bytes = content_text.encode('utf-8-sig')
    buf = BufferedInputFile(content_bytes, filename="users_export.csv")

    # 3. Отправляем файл НОВЫМ сообщением
    try:
        await callback.message.answer_document(
            document=buf,
            caption="Экспорт пользователей (CSV)"
        )

        # 4. Редактируем исходное меню, возвращая админ-панель
        await callback.message.edit_text(
            "✅ Экспорт успешно отправлен.\n\nАдмин-панель:",
            reply_markup=kb.admin_menu_keyboard()
        )

    except TelegramAPIError as e:
        # Если не смогли отправить файл
        await callback.message.edit_text(
            f"❌ Ошибка при отправке файла: {e}",
            reply_markup=kb.admin_menu_keyboard()
        )

    await callback.answer()


# --- 9. РУЧНОЕ ЗАВЕРШЕНИЕ (КОМАНДА) ---

@router.message(Command("finish_auction"), F.from_user.id.in_(ADMIN_IDS))
async def finish_auction_command(message: Message, bot: Bot):
    """
    Принудительно завершает текущий активный аукцион.
    (Оставлен как команда для экстренных случаев).
    """
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
            parse_mode="HTML",
            reply_markup=None
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