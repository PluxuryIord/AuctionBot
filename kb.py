# kb.py
from typing import Optional

from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
)
from aiogram.utils.keyboard import InlineKeyboardBuilder  # Используем Builder


def get_main_menu():
    # Задаем username администратора прямо здесь
    # TODO: Вынести admin_username в .env файл для лучшей конфигурации
    admin_username = "@AvroraDiamonds"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💎 Актуальный аукцион", callback_data="menu_current"))
    builder.row(InlineKeyboardButton(text="📚 Все аукционы", callback_data="menu_all")) # Отдельный ряд
    # Кнопка как ссылка
    builder.row(InlineKeyboardButton(text="📞 Связь с администратором", url=f"https://t.me/{admin_username}")) # Отдельный ряд и URL

    return builder.as_markup()


def get_auction_keyboard(
        auction_id: int,
        blitz_price: Optional[float],
        participation_status: Optional[str],
        is_admin: bool = False
):
    builder = InlineKeyboardBuilder()

    # --- ДИНАМИЧЕСКАЯ КНОПКА СТАВКИ/ЗАЯВКИ ---
    if participation_status == 'approved':
        builder.row(InlineKeyboardButton(text="Сделать ставку", callback_data=f"bid_auction_{auction_id}"))
    elif participation_status == 'pending':
        builder.row(InlineKeyboardButton(text="⏳ Заявка на рассмотрении", callback_data="noop"))
    elif participation_status == 'rejected':
        builder.row(InlineKeyboardButton(text="❌ Участие отклонено", callback_data="noop"))
    else:  # status is None
        builder.row(
            InlineKeyboardButton(text="✅ Подать заявку на участие", callback_data=f"apply_auction_{auction_id}"))
    # ---

    if blitz_price and blitz_price > 0:
        builder.row(InlineKeyboardButton(text=f"⚡️ Блиц-цена: {blitz_price:,.0f} ₽",
                                         callback_data=f"blitz_auction_{auction_id}"))

    builder.row(InlineKeyboardButton(text="📜 Все ставки", callback_data=f"show_bids_{auction_id}_1"))

    if is_admin:
        builder.row(
            InlineKeyboardButton(text="✏️ Ред. Название", callback_data=f"edit_auction_title_{auction_id}"),
            InlineKeyboardButton(text="✏️ Ред. Описание", callback_data=f"edit_auction_desc_{auction_id}")
        )

    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu"))
    return builder.as_markup()


def confirm_blitz_keyboard(auction_id: int) -> InlineKeyboardMarkup:
    """
    Клавиатура для подтверждения блиц-покупки.
    """
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="✅ Да, купить", callback_data=f"confirm_blitz_{auction_id}"))
    # Эта кнопка отмены вернет пользователя на карточку аукциона
    builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data=f"show_auction_{auction_id}"))
    return builder.as_markup()


def admin_approval_keyboard(user_id):
    buttons = [
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_user_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_user_{user_id}")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_participation_keyboard(user_id: int, auction_id: int) -> InlineKeyboardMarkup:
    """Кнопки одобрения/отклонения заявки на УЧАСТИЕ В ЛОТЕ."""
    buttons = [
        [
            InlineKeyboardButton(text="✅ Одобрить участие", callback_data=f"approve_part_{user_id}_{auction_id}"),
            InlineKeyboardButton(text="❌ Отклонить участие", callback_data=f"decline_part_{user_id}_{auction_id}")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_to_menu_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]]
    )


# --- НОВАЯ ФУНКЦИЯ ---
def cancel_fsm_keyboard(cancel_callback_data: str = "back_to_menu"):
    """
    Универсальная клавиатура для FSM с кнопкой "Отмена".
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=cancel_callback_data)]
        ]
    )


def get_main_menu_admin():
    # Задаем username администратора прямо здесь
    # TODO: Вынести admin_username в .env файл
    admin_username = "@AvroraDiamonds"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="💎 Актуальный аукцион", callback_data="menu_current"))
    builder.row(InlineKeyboardButton(text="📚 Все аукционы", callback_data="menu_all")) # Отдельный ряд
    # Кнопка как ссылка
    builder.row(InlineKeyboardButton(text="📞 Связь с администратором", url=f"https://t.me/{admin_username}")) # Отдельный ряд и URL
    builder.row(InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin_menu")) # Отдельный ряд

    return builder.as_markup()


async def admin_menu_keyboard() -> InlineKeyboardMarkup:
    """Генерирует клавиатуру админ-меню, включая статус автопринятия."""
    from db import get_auto_approve_status # Импортируем здесь, чтобы избежать циклического импорта

    auto_approve_enabled = await get_auto_approve_status()
    auto_approve_text = "✅ Автопринятие ВКЛ" if auto_approve_enabled else "❌ Автопринятие ВЫКЛ"

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🆕 Создать аукцион", callback_data="admin_create"))
    builder.row(InlineKeyboardButton(text="🛑 Завершить аукцион", callback_data="admin_finish"))
    builder.row(
        InlineKeyboardButton(text="⛔️ Забанить", callback_data="admin_ban"),
        InlineKeyboardButton(text="✅ Разбанить", callback_data="admin_unban")
    )
    # Кнопка автопринятия
    builder.row(InlineKeyboardButton(text=auto_approve_text, callback_data="admin_toggle_auto_approve"))
    # Кнопки массового управления
    builder.row(
        InlineKeyboardButton(text="👍 Одобрить ВСЕ заявки на регистрацию", callback_data="admin_bulk_approve"),
        InlineKeyboardButton(text="👎 Отклонить ВСЕ заявки на регистрацию", callback_data="admin_bulk_decline")
    )
    builder.row(InlineKeyboardButton(text="📤 Экспорт пользователей", callback_data="admin_export_users"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu"))

    return builder.as_markup()


def admin_select_winner_keyboard(top_bids: list[dict]) -> InlineKeyboardMarkup:
    # (без изменений)
    rows = []
    if top_bids:
        for i, b in enumerate(top_bids, start=1):
            user_disp = f"@{b.get('username')}" if b.get('username') else (b.get('full_name') or str(b.get('user_id')))
            text = f"{i}) {b.get('bid_amount', 0):,.0f} ₽ — {user_disp}".replace(',', ' ')
            rows.append([InlineKeyboardButton(text=text, callback_data=f"admin_winner_bid_{b['bid_id']}")])
    else:
        rows.append([InlineKeyboardButton(text="Ставок нет", callback_data="noop")])
    rows.append([InlineKeyboardButton(text="Без победителя", callback_data="admin_winner_none")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# --- НОВЫЕ КЛАВИАТУРЫ ---

def admin_confirm_auction_keyboard() -> InlineKeyboardMarkup:
    """Кнопки "Опубликовать / Редактировать / Отмена"."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data="auction_post")],
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data="auction_edit")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_menu")]
    ])


def admin_edit_auction_fields_keyboard() -> InlineKeyboardMarkup:
    """Выбор поля для редактирования."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Название", callback_data="edit_field_title"),
        InlineKeyboardButton(text="Описание", callback_data="edit_field_desc")
    )
    builder.row(
        InlineKeyboardButton(text="Фото", callback_data="edit_field_photo"),
        InlineKeyboardButton(text="Старт. цена", callback_data="edit_field_price")
    )
    builder.row(
        InlineKeyboardButton(text="Мин. шаг", callback_data="edit_field_step"),
        InlineKeyboardButton(text="Блиц-цена", callback_data="edit_field_blitz")
    )
    builder.row(
        InlineKeyboardButton(text="Кулдаун", callback_data="edit_field_cooldown"),
        InlineKeyboardButton(text="Откл. Кулдаун", callback_data="edit_field_cooldown_off")
    )
    builder.row(InlineKeyboardButton(text="Время оконч.", callback_data="edit_field_time"))
    builder.row(InlineKeyboardButton(text="⬅️ Назад к подтверждению", callback_data="edit_field_back"))
    return builder.as_markup()


# ---

def contact_request_keyboard():
    """Кнопка запроса контакта."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить мой номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True  # Кнопка исчезнет после нажатия
    )


def remove_reply_keyboard():
    return ReplyKeyboardRemove()


def subscribe_keyboard(channel_url: str | None = None, auction_id: int = 0):
    """Клавиатура для проверки подписки."""
    builder = InlineKeyboardBuilder()
    # Если URL передан, добавляем кнопку подписки
    if channel_url:
        builder.row(InlineKeyboardButton(text="📢 Подписаться на канал", url=channel_url))
    # Кнопка проверки подписки
    check_cb = f"check_sub_{auction_id}" if auction_id else "check_sub"
    builder.row(InlineKeyboardButton(text="🔄 Проверить подписку", callback_data=check_cb))
    # Кнопка Назад только для контекста аукциона
    if auction_id:
        builder.row(
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"show_auction_{auction_id}"))  # Возврат на карточку
    return builder.as_markup()


def auctions_pagination_keyboard(page: int, total: int, page_size: int = 5) -> InlineKeyboardMarkup:
    # (без изменений)
    total_pages = max(1, (total + page_size - 1) // page_size)
    buttons = []
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"all_page_{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"all_page_{page + 1}"))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_cancel_fsm_keyboard():
    """Кнопка Отмена, ведущая в админ-меню."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_menu")]
    ])


def bids_pagination_keyboard(auction_id: int, page: int, total_pages: int) -> InlineKeyboardMarkup:
    """
    Клавиатура пагинации для списка ставок.
    """
    builder = InlineKeyboardBuilder()
    nav_row = []

    if page > 1:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"show_bids_{auction_id}_{page - 1}"))

    nav_row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))

    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"show_bids_{auction_id}_{page + 1}"))

    if nav_row:
        builder.row(*nav_row)

    # Кнопка "Назад" должна возвращать на карточку аукциона
    builder.row(InlineKeyboardButton(text="⬅️ Назад к лоту", callback_data=f"show_auction_{auction_id}"))
    return builder.as_markup()