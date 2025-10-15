
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

def get_main_menu():
    buttons = [
        [InlineKeyboardButton(text="💎 Актуальный аукцион", callback_data="menu_current")],
        [InlineKeyboardButton(text="📚 Все аукционы", callback_data="menu_all"),
         InlineKeyboardButton(text="📞 Связь с администратором", callback_data="menu_contact")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_auction_keyboard(auction_id, blitz_price=None):
    buttons = [
        [InlineKeyboardButton(text="Сделать ставку", callback_data=f"bid_auction_{auction_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_approval_keyboard(user_id):
    buttons = [
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_user_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_user_{user_id}")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)





def back_to_menu_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")]]
    )


def get_main_menu_admin():
    buttons = [
        [InlineKeyboardButton(text="💎 Актуальный аукцион", callback_data="menu_current")],
        [InlineKeyboardButton(text="📚 Все аукционы", callback_data="menu_all"),
         InlineKeyboardButton(text="📞 Связь с администратором", callback_data="menu_contact")],
        [InlineKeyboardButton(text="⚙️ Админ", callback_data="admin_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_menu_keyboard():
    buttons = [
        [InlineKeyboardButton(text="🆕 Создать аукцион", callback_data="admin_create")],
        [InlineKeyboardButton(text="🛑 Завершить аукцион", callback_data="admin_finish")],
        [InlineKeyboardButton(text="⛔️ Забанить пользователя", callback_data="admin_ban")],
        [InlineKeyboardButton(text="✅ Разбанить пользователя", callback_data="admin_unban")],
        [InlineKeyboardButton(text="📤 Экспорт пользователей", callback_data="admin_export_users")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_select_winner_keyboard(top_bids: list[dict]) -> InlineKeyboardMarkup:
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




def contact_request_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить номер", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )


def remove_reply_keyboard():
    return ReplyKeyboardRemove()



def subscribe_keyboard(channel_url: str | None, auction_id: int):
    rows = []
    if channel_url:
        rows.append([InlineKeyboardButton(text="📢 Подписаться", url=channel_url)])
    check_cb = f"check_sub_{auction_id}" if auction_id else "check_sub"
    rows.append([InlineKeyboardButton(text="🔄 Проверить подписку", callback_data=check_cb)])
    if auction_id:
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)



def auctions_pagination_keyboard(page: int, total: int, page_size: int = 5) -> InlineKeyboardMarkup:
    total_pages = max(1, (total + page_size - 1) // page_size)
    buttons = []
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"all_page_{page-1}"))
    nav_row.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"all_page_{page+1}"))
    if nav_row:
        buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
