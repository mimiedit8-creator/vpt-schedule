from datetime import date, timedelta
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.markdown import hbold
from app.db import db
from app.keyboards import main_keyboard, schedule_keyboard, message_confirm_keyboard, privacy_keyboard
from app.services.schedule import format_schedule, refresh_schedule
from app.services.tracking import track_number

router = Router()

class Flow(StatesGroup):
    recipient = State()
    message_text = State()
    tracking_number = State()

async def save_user(user):
    await db().execute(
        "INSERT INTO users(user_id, username, first_name) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
        (user.id, (user.username or "").lower(), user.first_name or "")
    )
    await db().commit()

@router.message(CommandStart())
async def start(message: Message, state: FSMContext):
    await state.clear()
    await save_user(message.from_user)
    await message.answer(
        f"Привет, {message.from_user.first_name}! Это бот группы РКЛ-3 ВПТ.\n"
        "Расписание читается из кэша, поэтому обычные запросы отвечают быстро.",
        reply_markup=main_keyboard()
    )

@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "Команды: /start — запуск, /help — помощь.\n"
        "Для общения оба пользователя должны хотя бы раз запустить бота."
    )

@router.message(F.text == "📅 Расписание")
async def schedule_menu(message: Message):
    await message.answer("Расписание группы РКЛ-3:", reply_markup=schedule_keyboard())

@router.callback_query(F.data.startswith("schedule:"))
async def schedule_callback(call: CallbackQuery):
    action = call.data.split(":")[1]
    if action == "refresh":
        await call.answer("Проверяю сайт…", show_alert=False)
        ok, detail = await refresh_schedule()
        await call.message.answer(("✅ " if ok else "⚠️ ") + detail)
        return
    day = date.today() + (timedelta(days=1) if action == "tomorrow" else timedelta())
    if action == "week":
        parts = []
        for i in range(7):
            parts.append(await format_schedule(date.today() + timedelta(days=i)))
        text = "\n\n──────────\n\n".join(parts)
    else:
        text = await format_schedule(day)
    await call.message.answer(text, parse_mode="Markdown")
    await call.answer()

@router.message(F.text == "📦 Посылки")
async def tracking_menu(message: Message, state: FSMContext):
    await state.set_state(Flow.tracking_number)
    await message.answer("Отправь трек-номер посылки. Я попробую определить перевозчика и получить текущий статус.")

@router.message(Flow.tracking_number)
async def tracking_lookup(message: Message, state: FSMContext):
    number = (message.text or "").strip().replace(" ", "")
    if len(number) < 5 or len(number) > 50:
        await message.answer("Проверь трек-номер и отправь его ещё раз.")
        return
    await message.answer("Ищу отправление…")
    try:
        result = await track_number(number)
        await db().execute(
            "INSERT INTO tracks(user_id, number, last_status, last_location, updated_at) VALUES(?,?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(user_id, number) DO UPDATE SET last_status=excluded.last_status, last_location=excluded.last_location, updated_at=CURRENT_TIMESTAMP",
            (message.from_user.id, number, result["status"], result["location"])
        )
        await db().commit()
        await message.answer(
            f"📦 {hbold(number)}\nПеревозчик: {result['carrier']}\n"
            f"Статус: {result['status']}\nМестоположение: {result['location']}\n"
            f"Следующий шаг: {result['next']}",
            parse_mode="HTML", reply_markup=main_keyboard()
        )
    except Exception as exc:
        await message.answer(f"Не получилось получить данные: {exc}", reply_markup=main_keyboard())
    await state.clear()

@router.message(F.text == "💬 Общение")
async def messaging_menu(message: Message, state: FSMContext):
    await state.set_state(Flow.recipient)
    await message.answer("Введи @username пользователя. Найти можно только тех, кто уже запускал этого бота.")

@router.message(Flow.recipient)
async def find_recipient(message: Message, state: FSMContext):
    username = (message.text or "").strip().lstrip("@").lower()
    if not username or " " in username:
        await message.answer("Отправь один @username.")
        return
    cur = await db().execute("SELECT user_id, first_name, username, messaging_enabled FROM users WHERE username=? COLLATE NOCASE", (username,))
    user = await cur.fetchone()
    if not user:
        await message.answer("Пользователь не найден. Он должен сначала открыть бота и нажать /start.")
        return
    if user["user_id"] == message.from_user.id:
        await message.answer("Нельзя отправить сообщение самому себе.")
        return
    if not user["messaging_enabled"]:
        await message.answer("Этот пользователь отключил входящие сообщения.")
        return
    cur = await db().execute("SELECT 1 FROM blocks WHERE blocker_id=? AND blocked_id=?", (user["user_id"], message.from_user.id))
    if await cur.fetchone():
        await message.answer("Этот пользователь ограничил получение сообщений от тебя.")
        return
    await state.update_data(recipient_id=user["user_id"], recipient_name=user["first_name"])
    await state.set_state(Flow.message_text)
    await message.answer(f"Получатель: {user['first_name']} (@{username}). Теперь напиши сообщение.")

@router.message(Flow.message_text)
async def prepare_message(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text or len(text) > 1500:
        await message.answer("Сообщение должно быть от 1 до 1500 символов.")
        return
    await state.update_data(message_text=text)
    await message.answer("Предпросмотр:\n\n" + text + "\n\nОтправить?", reply_markup=message_confirm_keyboard())

@router.callback_query(F.data == "msg:confirm")
async def confirm_message(call: CallbackQuery, state: FSMContext, bot):
    data = await state.get_data()
    recipient_id = data.get("recipient_id")
    text = data.get("message_text")
    if not recipient_id or not text:
        await call.answer("Черновик не найден.", show_alert=True)
        return
    cur = await db().execute("SELECT messaging_enabled FROM users WHERE user_id=?", (recipient_id,))
    row = await cur.fetchone()
    cur = await db().execute("SELECT 1 FROM blocks WHERE blocker_id=? AND blocked_id=?", (recipient_id, call.from_user.id))
    blocked = await cur.fetchone()
    if not row or not row["messaging_enabled"] or blocked:
        await call.message.answer("Сообщение не отправлено: получатель недоступен.")
    else:
        try:
            await bot.send_message(recipient_id, f"💬 Сообщение от {call.from_user.first_name}:\n\n{text}")
            await call.message.answer("Сообщение отправлено.")
        except Exception:
            await call.message.answer("Не удалось доставить сообщение. Возможно, пользователь заблокировал бота.")
    await state.clear()
    await call.answer()

@router.callback_query(F.data == "msg:cancel")
async def cancel_message(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Отправка отменена.")
    await call.answer()

@router.message(F.text == "👤 Профиль")
async def profile(message: Message):
    cur = await db().execute("SELECT messaging_enabled FROM users WHERE user_id=?", (message.from_user.id,))
    row = await cur.fetchone()
    enabled = bool(row["messaging_enabled"]) if row else True
    await message.answer(
        f"Профиль: {message.from_user.first_name}\n"
        f"Telegram ID: {message.from_user.id}\n"
        f"Входящие сообщения: {'включены' if enabled else 'выключены'}",
        reply_markup=privacy_keyboard(enabled)
    )

@router.callback_query(F.data == "privacy:toggle")
async def toggle_privacy(call: CallbackQuery):
    cur = await db().execute("SELECT messaging_enabled FROM users WHERE user_id=?", (call.from_user.id,))
    row = await cur.fetchone()
    new_value = 0 if row and row["messaging_enabled"] else 1
    await db().execute("UPDATE users SET messaging_enabled=? WHERE user_id=?", (new_value, call.from_user.id))
    await db().commit()
    await call.message.answer("Входящие сообщения " + ("включены." if new_value else "выключены."))
    await call.answer()
