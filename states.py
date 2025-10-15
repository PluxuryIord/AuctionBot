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
    waiting_for_blitz_price = State()
    waiting_for_end_time = State()


class Bidding(StatesGroup):
    waiting_for_bid_amount = State()
