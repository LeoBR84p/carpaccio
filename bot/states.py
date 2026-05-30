from aiogram.fsm.state import State, StatesGroup


class DownloadFlow(StatesGroup):
    waiting_url = State()
    waiting_choice = State()
    processing = State()
    waiting_payment = State()
