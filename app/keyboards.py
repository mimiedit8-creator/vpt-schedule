from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Расписание"), KeyboardButton(text="📦 Посылки")],
            [KeyboardButton(text="💬 Общение"), KeyboardButton(text="👤 Профиль")],
        ], resize_keyboard=True
    )

def schedule_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data="schedule:today"),
         InlineKeyboardButton(text="Завтра", callback_data="schedule:tomorrow")],
        [InlineKeyboardButton(text="Неделя", callback_data="schedule:week"),
         InlineKeyboardButton(text="🔄 Обновить", callback_data="schedule:refresh")],
    ])

def message_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отправить", callback_data="msg:confirm"),
        InlineKeyboardButton(text="Отмена", callback_data="msg:cancel")
    ]])

def privacy_keyboard(enabled: bool):
    label = "Выключить входящие" if enabled else "Включить входящие"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data="privacy:toggle")]
    ])
