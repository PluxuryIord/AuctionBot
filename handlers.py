
import os
import re
import pytz
from datetime import datetime, timedelta
import logging

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, User, InputMediaPhoto, BufferedInputFile
from html import escape

from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import default_state
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.markdown import hbold, html_decoration

import db as db
import kb
from states import Registration, AuctionCreation, Bidding, AdminActions

# Загружаем переменные окружения
ADMIN_ID = os.getenv("ADMIN_ID")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")

CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")


async def is_user_subscribed(bot: Bot, user_id: int) -> bool:
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

    # Пропускаем /admin всегда; /start только для новых (без статуса)
    if isinstance(event, Message) and event.text == "/admin":
        return await handler(event, data)
    if isinstance(event, Message) and event.text == "/start":
        status_for_start = await db.get_user_status(user.id)
        if not status_for_start:  # новый пользователь, статуса ещё нет
            return await handler(event, data)
    # Разрешаем любые активные FSM шаги
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
    # Глобальная проверка подписки (кроме админа и кнопки проверки подписки)
    try:
        bot_inst: Bot = data.get("bot")
    except Exception:
        bot_inst = None
    allow_check = isinstance(event, CallbackQuery) and getattr(event, "data", None) and str(event.data).startswith("check_sub")
    if bot_inst and not allow_check:
        try:
            subscribed = await is_user_subscribed(bot_inst, user.id)
        except Exception:
            subscribed = False
        if not subscribed:
            channel_url = f"https://t.me/{CHANNEL_USERNAME}" if CHANNEL_USERNAME else None
            text = (
                "Для пользования ботом необходимо быть подписанным на наш канал.\n"
                "Подпишитесь и нажмите ‘Проверить подписку’."
            )
            try:
                if isinstance(event, CallbackQuery) and event.message:
                    msg = event.message
                    if getattr(msg, "photo", None) or msg.caption is not None:
                        await bot_inst.edit_message_caption(
                            chat_id=msg.chat.id,
                            message_id=msg.message_id,
                            caption=text,
                            reply_markup=kb.subscribe_keyboard(channel_url, 0)
                        )
                    else:
                        await bot_inst.edit_message_text(
                            chat_id=msg.chat.id,
                            message_id=msg.message_id,
                            text=text,
                            reply_markup=kb.subscribe_keyboard(channel_url, 0)
                        )
                    try:
                        await event.answer("Подписка обязательна", show_alert=True)
                    except Exception:
                        pass
                else:
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

# Общие валидаторы/нормализаторы ввода
NAME_ALLOWED_RE = re.compile(r"^[A-Za-zА-Яа-яЁё\-\s]{2,100}$")

def clean_full_name(s: str) -> str:
    s = (s or "").strip()
    # сжать повторяющиеся пробелы
    s = re.sub(r"\s+", " ", s)
    return s

def is_valid_full_name(s: str) -> bool:
    return bool(NAME_ALLOWED_RE.match(s))

def parse_amount(s: str) -> float:
    # поддержка форматов вида "100 000,50" и т.п.
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

    # Активный аукцион: текущая цена, лидер и ТОП-5 ставок
    current_price = last_bid['bid_amount'] if last_bid else auction_data['start_price']
    leader_text = f"@{(last_bid['username'])}" if last_bid else "Ставок еще нет"
    end_time_dt = auction_data['end_time'].astimezone(MOSCOW_TZ)

    # История ставок (ТОП-5)
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

# --- 1. РЕГИСТРАЦИЯ ПОЛЬЗОВАТЕЛЕЙ ---

@router.message(CommandStart(), StateFilter(default_state))
async def cmd_start(message: Message, state: FSMContext):
    """Обработчик команды /start."""
    user_status = await db.get_user_status(message.from_user.id)
    if str(message.from_user.id) == ADMIN_ID:
        await message.answer("Добро пожаловать в аукцион!", reply_markup=kb.get_main_menu_admin())
    elif user_status == 'banned':
        await message.answer("Ваш доступ к боту заблокирован.")
    elif user_status == 'pending':
        await message.answer("Ваша заявка на регистрацию находится на рассмотрении.")
    elif user_status == 'approved':
        await message.answer("Добро пожаловать в аукцион!", reply_markup=kb.get_main_menu())
    else:
        await state.set_state(Registration.waiting_for_full_name)
        await message.answer(
            "Здравствуйте! Для участия в аукционе, пожалуйста, зарегистрируйтесь.\n\n"
            "Введите ваше ФИО:"
        )


@router.message(StateFilter(Registration.waiting_for_full_name), F.text)
async def process_full_name(message: Message, state: FSMContext):
    """Ловит ФИО и запрашивает номер телефона."""
    name = clean_full_name(message.text)
    if not is_valid_full_name(name):
        await message.answer("Введите корректное ФИО (2–100 символов, только буквы и пробелы).")
        return
    await state.update_data(full_name=name)
    await state.set_state(Registration.waiting_for_phone)
    await message.answer(
        "Отлично! Теперь, пожалуйста, отправьте ваш номер телефона в формате +7XXXXXXXXXX\n\n"
        "Можно нажать кнопку ниже, чтобы отправить контакт, или ввести номер вручную.",
        reply_markup=kb.contact_request_keyboard()
    )



@router.message(StateFilter(Registration.waiting_for_phone), F.contact)
async def process_phone_contact(message: Message, state: FSMContext, bot: Bot):
    """Обработка контакта с кнопки 'Отправить номер'."""
    phone_number = normalize_phone(message.contact.phone_number)
    if not re.fullmatch(r"\+7\d{10}", phone_number or ""):
        await message.answer("Некорректный номер. Укажите номер в формате +7XXXXXXXXXX или отправьте контакт снова.")
        return
    existing = await db.get_user_by_phone(phone_number)
    if existing and existing.get('user_id') != message.from_user.id:
        await message.answer("Этот номер уже используется другим пользователем. Укажите другой номер.")
        return
    user_data = await state.get_data()

    await db.add_user_request(
        user_id=message.from_user.id,
        username=message.from_user.username,
        full_name=user_data['full_name'],
        phone_number=phone_number
    )

    await message.answer("Спасибо! Ваша заявка отправлена на модерацию. Ожидайте подтверждения.", reply_markup=kb.remove_reply_keyboard())
    await state.clear()

    # Уведомляем админа
    try:
        await bot.send_message(
            int(ADMIN_CHAT_ID),
            f"Новая заявка на регистрацию:\n\n"
            f"ID: <code>{message.from_user.id}</code>\n"
            f"Username: @{escape(message.from_user.username or '')}\n"
            f"ФИО: {escape(user_data.get('full_name') or '')}\n"
            f"Телефон: <code>{escape(phone_number)}</code>",
            parse_mode="HTML",
            reply_markup=kb.admin_approval_keyboard(message.from_user.id)
        )
    except Exception as e:
        logging.error(f"Не удалось отправить заявку админу: {e}")


@router.message(StateFilter(Registration.waiting_for_phone), F.text)
async def process_phone(message: Message, state: FSMContext, bot: Bot):
    """Ловит номер, сохраняет заявку и отправляет админу на модерацию."""
    phone_number = normalize_phone(message.text)
    if not re.fullmatch(r"\+7\d{10}", phone_number or ""):
        await message.answer("Некорректный номер. Укажите номер в формате +7XXXXXXXXXX.")
        return
    existing = await db.get_user_by_phone(phone_number)
    if existing and existing.get('user_id') != message.from_user.id:
        await message.answer("Этот номер уже используется другим пользователем. Укажите другой номер.")
        return
    user_data = await state.get_data()

    await db.add_user_request(
        user_id=message.from_user.id,
        username=message.from_user.username,
        full_name=user_data['full_name'],
        phone_number=phone_number
    )

    await message.answer("Спасибо! Ваша заявка отправлена на модерацию. Ожидайте подтверждения.", reply_markup=kb.remove_reply_keyboard())

    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"❗️ Новая заявка на регистрацию:\n\n"
            f"ID: <code>{message.from_user.id}</code>\n"
            f"Username: @{escape(message.from_user.username or '')}\n"
            f"ФИО: {escape(user_data.get('full_name') or '')}\n"
            f"Телефон: <code>{escape(phone_number)}</code>",
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
    try:
        user_id = int(callback.data.split("_")[2])
    except Exception:
        return await callback.answer("Некорректный идентификатор", show_alert=True)
    await db.update_user_status(user_id, 'approved')
    await callback.message.edit_text(f"✅ Пользователь {user_id} одобрен.")
    try:
        await bot.send_message(user_id, "Ваша заявка одобрена! Теперь вы можете участвовать в аукционах.",
                               reply_markup=kb.get_main_menu())
    except TelegramAPIError as e:
        logging.error(f"Не удалось уведомить пользователя {user_id} об одобрении: {e}")
    await callback.answer()


@router.callback_query(F.data.startswith("decline_user_"))
async def decline_user(callback: CallbackQuery, state: FSMContext):
    """Отклонение заявки пользователя: запросить причину (опционально)."""
    if str(callback.from_user.id) != ADMIN_ID:
        return await callback.answer("Нет доступа", show_alert=True)
    try:
        target_user_id = int(callback.data.split("_")[2])
    except Exception:
        return await callback.answer("Некорректный идентификатор", show_alert=True)
    await state.set_state(AdminActions.waiting_for_decline_reason)
    await state.update_data(target_user_id=target_user_id, admin_message_id=callback.message.message_id)
    await callback.bot.edit_message_text(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        text=("Введите причину отклонения (опционально).\n"
              "Отправьте ‘-’ или оставьте пусто, чтобы отклонить без причины."),
        reply_markup=kb.back_to_menu_keyboard()
    )
    await callback.answer()


@router.message(StateFilter(AdminActions.waiting_for_decline_reason))
async def decline_reason_process(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target_user_id = data.get('target_user_id')
    reason = (message.text or '').strip()
    no_reason = (reason == '-' or reason == '')
    await db.update_user_status(target_user_id, 'banned')

    # Уведомим пользователя о решении
    notify_text = "К сожалению, ваша заявка на регистрацию была отклонена."
    if not no_reason:
        notify_text += f"\nПричина: {reason}"
    try:
        await bot.send_message(target_user_id, notify_text)
    except TelegramAPIError as e:
        logging.error(f"Не удалось уведомить пользователя {target_user_id} об отклонении: {e}")

    await message.answer(f"❌ Заявка пользователя {target_user_id} отклонена." + (" (без причины)" if no_reason else ""))
    await state.clear()


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
    await bot.edit_message_media(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        media=InputMediaPhoto(media=auction['photo_id'], caption=text, parse_mode="HTML"),
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
    except TelegramAPIError as e:
        logging.warning(f"Не удалось отредактировать сообщение со списком аукционов: {e}")
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data == "menu_contact")
async def menu_contact(callback: CallbackQuery):
    admin_username = "CoId_Siemens"
    await callback.bot.edit_message_text(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        text=f"По всем вопросам вы можете написать нашему администратору: @{admin_username}",
        reply_markup=kb.back_to_menu_keyboard()
    )

@router.callback_query(F.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    keyboard = kb.get_main_menu_admin() if str(callback.from_user.id) == ADMIN_ID else kb.get_main_menu()

    # Если сейчас показана карточка лота (фото/подпись), удаляем её и отправляем новое текстовое меню,
    # чтобы не оставался "меню с фото".
    if getattr(callback.message, "photo", None) or callback.message.caption is not None:
        try:
            await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=callback.message.message_id)
        except TelegramAPIError:
            # если удалить не удалось, попробуем хотя бы заменить подпись
            try:
                await callback.bot.edit_message_caption(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    caption="Добро пожаловать в аукцион!",
                    reply_markup=keyboard
                )
                await callback.answer()
                return
            except TelegramAPIError:
                pass
        # Отправляем новое текстовое меню
        await callback.message.answer("Добро пожаловать в аукцион!", reply_markup=keyboard)
    else:
        # Обычное текстовое сообщение можно редактировать
        await callback.bot.edit_message_text(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            text="Добро пожаловать в аукцион!",
            reply_markup=keyboard
        )
    await callback.answer()


@router.callback_query(F.data == "admin_menu")
async def admin_menu(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != ADMIN_ID:
        return await callback.answer("Нет доступа", show_alert=True)
    await callback.bot.edit_message_text(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        text="Админ-панель: выберите действие",
        reply_markup=kb.admin_menu_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "admin_create")
async def admin_create(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != ADMIN_ID:
        return await callback.answer("Нет доступа", show_alert=True)
    await create_auction_start(callback.message, state)
    await callback.answer()


@router.callback_query(F.data == "admin_finish")
async def admin_finish(callback: CallbackQuery, bot: Bot):
    if str(callback.from_user.id) != ADMIN_ID:
        return await callback.answer("Нет доступа", show_alert=True)
    active = await db.get_active_auction()
    if not active:
        await callback.answer("Нет активного аукциона", show_alert=True)
        return
    top_bids = await db.get_top_bids(active['auction_id'], limit=5)
    await callback.bot.edit_message_text(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        text=f"Выберите победителя для аукциона: \n\n«{active['title']}»",
        reply_markup=kb.admin_select_winner_keyboard(top_bids)
    )
    await callback.answer()



@router.callback_query(F.data == "admin_winner_none")
async def admin_winner_none(callback: CallbackQuery, bot: Bot):
    if str(callback.from_user.id) != ADMIN_ID:
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
    if str(callback.from_user.id) != ADMIN_ID:
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
    # Завершаем с выбранной ставкой
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
    # Уведомим победителя
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


@router.callback_query(F.data == "admin_ban")
async def admin_ban(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != ADMIN_ID:
        return await callback.answer("Нет доступа", show_alert=True)
    await state.set_state(AdminActions.waiting_for_ban_id)
    await callback.bot.edit_message_text(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        text="Введите ID / @username / телефон пользователя для бана:",
        reply_markup=kb.back_to_menu_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "admin_unban")
async def admin_unban(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != ADMIN_ID:
        return await callback.answer("Нет доступа", show_alert=True)
    await state.set_state(AdminActions.waiting_for_unban_id)
    await callback.bot.edit_message_text(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        text="Введите ID / @username / телефон пользователя для разбана:",
        reply_markup=kb.back_to_menu_keyboard()
    )
    await callback.answer()


@router.message(StateFilter(AdminActions.waiting_for_ban_id), F.from_user.id == int(ADMIN_ID))
async def admin_ban_handle(message: Message, state: FSMContext):
    text = message.text.strip()
    target_user_id = None

    # По username
    if text.startswith('@'):
        user = await db.get_user_by_username(text)
        if user:
            target_user_id = user['user_id']
    else:
        # По телефону
        normalized = normalize_phone(text)
        if normalized != text or text.startswith('+'):
            user = await db.get_user_by_phone(normalized)
            if user:
                target_user_id = user['user_id']
        # По ID
        if target_user_id is None:
            try:
                target_user_id = int(text)
            except ValueError:
                pass

    if target_user_id is None:
        await message.answer("❌ Пользователь не найден по указанным данным.")
        return

    await db.update_user_status(target_user_id, 'banned')
    await message.answer(f"✅ Пользователь {target_user_id} забанен.")
    await state.clear()


@router.message(StateFilter(AdminActions.waiting_for_unban_id), F.from_user.id == int(ADMIN_ID))
async def admin_unban_handle(message: Message, state: FSMContext):
    text = message.text.strip()
    target_user_id = None

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

    if target_user_id is None:
        await message.answer("❌ Пользователь не найден по указанным данным.")
        return

    await db.update_user_status(target_user_id, 'approved')
    await message.answer(f"✅ Пользователь {target_user_id} разбанен.")
    await state.clear()



# --- 4. ЛОГИКА СТАВОК ---

@router.callback_query(F.data.startswith("bid_auction_"))
async def make_bid_start(callback: CallbackQuery, state: FSMContext, bot: Bot):
    """Начало процесса ставки."""
    auction_id = int(callback.data.split("_")[2])
    auction = await db.get_active_auction()

    if not auction or auction['auction_id'] != auction_id:
        await callback.answer("Аукцион уже завершен или неактивен.", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        return

    # Проверка подписки на канал (обязательное условие участия)
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

    # Проверка интервала между ставками
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

    # Готовим ввод суммы ставки
    await state.set_state(Bidding.waiting_for_bid_amount)
    await state.update_data(auction_id=auction_id, private_message_id=callback.message.message_id)

    last_bid = await db.get_last_bid(auction_id)
    current_price = last_bid['bid_amount'] if last_bid else auction['start_price']

    await callback.bot.edit_message_caption(
        chat_id=callback.message.chat.id,
        message_id=callback.message.message_id,
        caption=(
            f"Текущая ставка: {current_price:,.0f} руб.\n"
            f"Минимальный шаг: {auction['min_step']:,.0f} руб.\n\n"
            f"{hbold('Введите вашу ставку:')}"
        ),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "check_sub")
async def check_subscription_generic(callback: CallbackQuery, bot: Bot):
    """Глобальная проверка подписки: если подписан — показываем главное меню."""
    if await is_user_subscribed(bot, callback.from_user.id):
        keyboard = kb.get_main_menu_admin() if str(callback.from_user.id) == ADMIN_ID else kb.get_main_menu()
        # Если текущее сообщение — фото, удаляем и отправляем новое текстовое меню
        if getattr(callback.message, "photo", None) or callback.message.caption is not None:
            try:
                await callback.bot.delete_message(chat_id=callback.message.chat.id, message_id=callback.message.message_id)
            except Exception:
                pass
            await callback.message.answer("Добро пожаловать в аукцион!", reply_markup=keyboard)
        else:
            await callback.bot.edit_message_text(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                text="Добро пожаловать в аукцион!",
                reply_markup=keyboard
            )
        await callback.answer("Подписка подтверждена", show_alert=True)
    else:
        await callback.answer("Вы ещё не подписаны на канал", show_alert=True)






@router.callback_query(F.data.startswith("check_sub_"))
async def check_subscription(callback: CallbackQuery, bot: Bot):
    """Проверка подписки по кнопке."""
    if await is_user_subscribed(bot, callback.from_user.id):
        auction = await db.get_active_auction()
        if not auction:
            await callback.bot.edit_message_text(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id,
                text="На данный момент активных аукционов нет.",
                reply_markup=kb.back_to_menu_keyboard()
            )
        else:
            text = await format_auction_post(auction, bot)
            try:
                await callback.bot.edit_message_media(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    media=InputMediaPhoto(media=auction['photo_id'], caption=text, parse_mode="HTML"),
                    reply_markup=kb.get_auction_keyboard(auction['auction_id'], auction['blitz_price'])
                )
            except TelegramAPIError as e:
                logging.warning(f"Не удалось перерисовать карточку аукциона после проверки подписки: {e}")
        await callback.answer("Подписка подтверждена", show_alert=True)
    else:
        await callback.answer("Вы ещё не подписаны на канал", show_alert=True)





@router.callback_query(F.data.startswith("blitz_auction_"))
async def blitz_buy(callback: CallbackQuery, bot: Bot):
    """Покупка по блиц-цене через кнопку."""
    auction_id = int(callback.data.split("_")[2])
    auction = await db.get_active_auction()

    if not auction or auction['auction_id'] != auction_id:
        await callback.answer("Аукцион уже завершен или неактивен.", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        return

    blitz_price = auction.get('blitz_price')
    if not blitz_price:
        await callback.answer("Блиц-цена недоступна для этого лота.", show_alert=True)
        return

    # Фиксируем покупку и завершаем аукцион
    await db.add_bid(auction_id, callback.from_user.id, blitz_price)
    await db.finish_auction(auction_id, callback.from_user.id, blitz_price)

    finished_post_text = await format_auction_post(auction, bot, finished=True)

    # Обновляем пост в канале
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

    # Обновляем приватное сообщение (один экран) и показываем кнопку Назад
    try:
        await callback.bot.edit_message_caption(
            chat_id=callback.message.chat.id,
            message_id=callback.message.message_id,
            caption=finished_post_text,
            parse_mode="HTML",
            reply_markup=None
        )
    except TelegramAPIError as e:
        logging.warning(f"Не удалось обновить приватную карточку после блиц-покупки: {e}")

    # Уведомляем победителя
    try:
        await bot.send_message(
            callback.from_user.id,
            f"🎉 Поздравляем! Вы купили лот «{(auction['title'])}» по блиц-цене {blitz_price:,.2f} руб.\n\n"
            f"В ближайшее время с вами свяжется администратор."
        )
    except TelegramAPIError as e:
        logging.warning(f"Не удалось уведомить победителя {callback.from_user.id} после блиц-покупки: {e}")

    await callback.answer("Покупка по блиц-цене оформлена!", show_alert=True)

@router.message(StateFilter(Bidding.waiting_for_bid_amount), F.text)
async def process_bid_amount(message: Message, state: FSMContext, bot: Bot):
    """Обработка введенной суммы ставки."""
    try:
        bid_amount = parse_amount(message.text)
        if bid_amount <= 0:
            await message.answer("Сумма ставки должна быть положительным числом.")
            return
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
    # Проверяем подписку перед обработкой ставки
    data = await state.get_data()
    if not await is_user_subscribed(bot, message.from_user.id):
        channel_url = f"https://t.me/{CHANNEL_USERNAME}" if CHANNEL_USERNAME else None
        try:
            await bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=data.get('private_message_id'),
                caption=(
                    "Для участия в аукционе необходимо быть подписанным на наш канал.\n"
                    "Подпишитесь и нажмите ‘Проверить подписку’."
                ),
                reply_markup=kb.subscribe_keyboard(channel_url, data.get('auction_id', 0))
            )
        except Exception:
            pass
        try:
            await message.delete()
        except Exception:
            pass
        return

    current_price = last_bid['bid_amount'] if last_bid else auction['start_price']

    # Блиц-покупка через ручной ввод суммы >= blitz_price
    blitz_price = auction.get('blitz_price')
    if blitz_price and bid_amount >= blitz_price:
        await db.add_bid(auction['auction_id'], message.from_user.id, blitz_price)
        await db.finish_auction(auction['auction_id'], message.from_user.id, blitz_price)

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
            logging.error(f"Не удалось обновить пост в канале после блиц-покупки: {e}")
        try:
            await bot.edit_message_caption(
                chat_id=message.chat.id,
                message_id=data['private_message_id'],
                caption=finished_post_text,
                parse_mode="HTML",
                reply_markup=None
            )
        except TelegramAPIError as e:
            logging.warning(f"Не удалось обновить приватную карточку после блиц-покупки: {e}")

        await message.answer(f"⚡️ Вы купили лот по блиц-цене {blitz_price:,.0f} руб.")
        await state.clear()
        return

    if bid_amount < current_price + auction['min_step']:
        await message.answer(f"Ваша ставка должна быть как минимум {current_price + auction['min_step']:,.0f} руб.")
        return

    previous_leader = last_bid['user_id'] if last_bid else None

    await db.add_bid(auction['auction_id'], message.from_user.id, bid_amount)
    await message.answer(f"✅ Ваша ставка в размере {bid_amount:,.0f} руб. принята!")
    await state.clear()
    # Антиснайпинг: если осталось ≤ 2 минут, продлеваем на 2 минуты
    try:
        end_dt = auction['end_time']
        now_dt = datetime.now(end_dt.tzinfo)
        if (end_dt - now_dt) <= timedelta(minutes=2):
            new_end = end_dt + timedelta(minutes=2)
            await db.update_auction_end_time(auction['auction_id'], new_end)
            auction = await db.get_active_auction()
    except Exception as e:
        logging.warning(f"Антиснайпинг не сработал: {e}")


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
    """Инлайн админ-меню."""
    await message.answer("Админ-панель: выберите действие", reply_markup=kb.admin_menu_keyboard())


# --- Создание аукциона (FSM) ---
@router.message(Command("create_auction"), F.from_user.id == int(ADMIN_ID))
async def create_auction_start(message: Message, state: FSMContext):
    active_auction = await db.get_active_auction()
    if active_auction:
        await message.answer("Нельзя создать новый аукцион, пока не завершен предыдущий.")
        return
    await state.set_state(AuctionCreation.waiting_for_title)
    await message.answer("Шаг 1/9: Введите название лота:")


@router.message(StateFilter(AuctionCreation.waiting_for_title), F.text)
async def process_auction_title(message: Message, state: FSMContext):
    title = (message.text or "").strip()
    if not title or len(title) > 120:
        await message.answer("Название должно быть от 1 до 120 символов. Попробуйте снова.")
        return
    await state.update_data(title=title)
    await state.set_state(AuctionCreation.waiting_for_description)
    await message.answer("Шаг 2/9: Введите описание лота")


@router.message(StateFilter(AuctionCreation.waiting_for_description), F.text)
async def process_auction_desc(message: Message, state: FSMContext):
    desc = (message.text or "").strip()
    if not desc or len(desc) > 3000:
        await message.answer("Описание должно быть от 1 до 3000 символов. Попробуйте снова.")
        return
    await state.update_data(description=desc)
    await state.set_state(AuctionCreation.waiting_for_photo)
    await message.answer("Шаг 3/9: Отправьте фотографию лота:")


@router.message(StateFilter(AuctionCreation.waiting_for_photo), F.photo)
async def process_auction_photo(message: Message, state: FSMContext):
    await state.update_data(photo=message.photo[-1].file_id)
    await state.set_state(AuctionCreation.waiting_for_start_price)
    await message.answer("Шаг 4/9: Введите начальную цену (число, например: 150000):")


@router.message(StateFilter(AuctionCreation.waiting_for_start_price))
async def process_auction_start_price(message: Message, state: FSMContext):
    try:
        value = float(message.text)
        if value <= 0:
            await message.answer("Начальная цена должна быть положительным числом (> 0). Попробуйте снова.")
            return
        await state.update_data(start_price=value)
        await state.set_state(AuctionCreation.waiting_for_min_step)
        await message.answer("Шаг 5/9: Введите минимальный шаг ставки (число, например: 1000):")
    except ValueError:
        await message.answer("Неверный формат. Введите число (например, 150000).")



@router.message(StateFilter(AuctionCreation.waiting_for_min_step))
async def process_auction_min_step(message: Message, state: FSMContext):
    try:
        min_step = float(message.text)
        if min_step <= 0:
            await message.answer("Минимальный шаг должен быть положительным числом (> 0). Попробуйте снова.")
            return
        await state.update_data(min_step=min_step)
        await state.set_state(AuctionCreation.waiting_for_cooldown_minutes)
        await message.answer("Шаг 6/9: Введите ограничение между ставками в минутах (например: 10):")
    except ValueError:
        await message.answer("Неверный формат. Введите число (например, 1000).")


@router.message(StateFilter(AuctionCreation.waiting_for_cooldown_minutes))
async def process_auction_cooldown_minutes(message: Message, state: FSMContext):
    try:
        cooldown = int(message.text)
        if cooldown < 0:
            await message.answer("Ограничение между ставками должно быть 0 или больше. Попробуйте снова.")
            return
        await state.update_data(cooldown_minutes=cooldown)
        await state.set_state(AuctionCreation.waiting_for_cooldown_off_before_end)
        await message.answer("Шаг 7/9: За сколько минут до конца аукциона снять ограничение? (например: 30). Если введёте 0 — ограничений не будет:")
    except ValueError:
        await message.answer("Неверный формат. Введите целое число минут (например, 10).")


@router.message(StateFilter(AuctionCreation.waiting_for_cooldown_off_before_end))
async def process_auction_cooldown_off_threshold(message: Message, state: FSMContext):
    try:
        threshold = int(message.text)
        if threshold < 0:
            await message.answer("Значение должно быть 0 или больше. Попробуйте снова.")
            return
        if threshold == 0:
            # 0 = полностью отключить ограничение между ставками
            await state.update_data(cooldown_minutes=0)
        await state.update_data(cooldown_off_before_end_minutes=threshold)
        await state.set_state(AuctionCreation.waiting_for_blitz_price)
        await message.answer("Шаг 8/9: Введите блиц-цену (число, если не нужна — введите 0):")
    except ValueError:
        await message.answer("Неверный формат. Введите целое число минут (например, 30).")


@router.message(StateFilter(AuctionCreation.waiting_for_blitz_price))
async def process_auction_blitz_price(message: Message, state: FSMContext):
    try:
        blitz_price = float(message.text)
        if blitz_price < 0:
            await message.answer("Блиц-цена не может быть отрицательной. Введите 0, если блиц-цена не нужна.")
            return
        data = await state.get_data()
        start_price = float(data.get('start_price') or 0)
        if blitz_price > 0 and blitz_price < start_price:
            await message.answer("Блиц-цена должна быть не меньше начальной цены. Попробуйте снова.")
            return
        await state.update_data(blitz_price=blitz_price if blitz_price > 0 else None)
        await state.set_state(AuctionCreation.waiting_for_end_time)
        await message.answer(
            "Шаг 9/9: Введите дату и время окончания аукциона в формате: ДД.ММ.ГГГГ ЧЧ:ММ\n\nНапример: 25.10.2025 21:00")
    except ValueError:
        await message.answer("Неверный формат. Введите число (например, 300000).")


@router.message(StateFilter(AuctionCreation.waiting_for_end_time), F.text)
async def process_auction_end_time(message: Message, state: FSMContext, bot: Bot):
    try:
        naive_end_time = datetime.strptime(message.text, "%d.%m.%Y %H:%M")
        end_time = MOSCOW_TZ.localize(naive_end_time)
        now = datetime.now(MOSCOW_TZ)
        if end_time <= now:
            await message.answer("Дата и время окончания должны быть в будущем. Укажите корректное время.")
            return
        # Минимальная длительность 10 минут
        if end_time - now < timedelta(minutes=10):
            await message.answer("Минимальная длительность аукциона — 10 минут от текущего времени.")
            return
        await state.update_data(end_time=end_time)

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



@router.callback_query(F.data == "admin_export_users")
async def admin_export_users(callback: CallbackQuery):
    if str(callback.from_user.id) != ADMIN_ID:
        return await callback.answer("Нет доступа", show_alert=True)
    rows = await db.get_users_with_bid_stats()
    # Готовим CSV (можно открыть в Excel); кодировка UTF-8 BOM для корректного открытия кириллицы в Excel
    import io, csv
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
    await callback.message.answer_document(document=buf, caption="Экспорт пользователей (CSV; откроется в Excel)")
    await callback.answer()

