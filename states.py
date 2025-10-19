# states.py
from aiogram.fsm.state import State, StatesGroup


class Registration(StatesGroup):
    waiting_for_full_name = State()
    waiting_for_phone = State()


class AuctionCreation(StatesGroup):
    waiting_for_title = State()
    waiting_for_description = State()
    waiting_for_photo = State()
    waiting_for_start_price = State()
    waiting_for_min_step = State()
    waiting_for_cooldown_minutes = State()
    waiting_for_cooldown_off_before_end = State()
    waiting_for_blitz_price = State()
    waiting_for_end_time = State()

    # --- НОВЫЕ СОСТОЯНИЯ ---
    waiting_for_confirmation = State()  # Экран "Опубликовать / Редактировать"
    waiting_for_edit_choice = State()  # Экран выбора поля для редактирования


class Bidding(StatesGroup):
    waiting_for_bid_amount = State()


class AdminActions(StatesGroup):
    waiting_for_ban_id = State()
    waiting_for_unban_id = State()
    waiting_for_decline_reason = State()