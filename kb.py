# keyboards.py
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton


def get_main_menu():
    buttons = [
        [KeyboardButton(text="💎 Актуальный аукцион")],
        [KeyboardButton(text="📚 Все аукционы"), KeyboardButton(text="📞 Связь с администратором")]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)
    return keyboard


def get_auction_keyboard(auction_id, blitz_price):
    buttons = [
        [InlineKeyboardButton(text="Сделать ставку", callback_data=f"bid_auction_{auction_id}")],
    ]
    if blitz_price:
        buttons.append([InlineKeyboardButton(text=f"Купить за {blitz_price} руб (Блиц-цена)",
                                             callback_data=f"blitz_auction_{auction_id}")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    return keyboard


def admin_approval_keyboard(user_id):
    buttons = [
        [
            InlineKeyboardButton(text="✅ Одобрить", callback_data=f"approve_user_{user_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"decline_user_{user_id}")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_phone_keyboard():
    buttons = [[KeyboardButton(text="📱 Поделиться номером телефона", request_contact=True)]]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)