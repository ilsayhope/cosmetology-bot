import os
import asyncio
import logging
import datetime
import locale

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
import aiosqlite

from aiogram3_calendar import SimpleCalendar
from aiogram3_calendar.simple_calendar import SimpleCalendarCallback

# Установка русской локали для корректного отображения дат библиотеками
try:
    locale.setlocale(locale.LC_TIME, 'ru_RU.UTF-8')
except locale.Error:
    try:
        locale.setlocale(locale.LC_TIME, 'Russian_Russia.1251')
    except locale.Error:
        logging.warning("Не удалось установить русскую локаль. Используются дефолтные настройки.")

# Загрузка конфигурации
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(uid.strip()) for uid in os.getenv("ADMIN_IDS", "").split(",") if uid.strip()]
DB_NAME = "cosmetology.db"

if not BOT_TOKEN:
    raise ValueError("Переменная BOT_TOKEN не задана в файле .env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()

TIME_SLOTS = ["10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00", "19:00"]
RU_DAYS = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

# ==========================================
# ИНИЦИАЛИЗАЦИЯ И РАБОТА С БАЗОЙ ДАННЫХ
# ==========================================

async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("PRAGMA foreign_keys = ON;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                phone TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL
            )
        """)
        # Добавлено поле photo_id
        await db.execute("""
            CREATE TABLE IF NOT EXISTS services (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER,
                name TEXT NOT NULL,
                price INTEGER,
                photo_id TEXT,
                FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
            )
        """)
        # Добавлено поле admin_comment
        await db.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                service_id INTEGER,
                date TEXT,
                time TEXT,
                photo_id TEXT,
                allergies_comment TEXT,
                status TEXT DEFAULT 'pending',
                reminded INTEGER DEFAULT 0,
                admin_comment TEXT,
                FOREIGN KEY (service_id) REFERENCES services(id) ON DELETE CASCADE
            )
        """)
        await db.commit()
        
    # Мягкая миграция: добавляем новые колонки в уже существующую БД, если их там нет
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("ALTER TABLE services ADD COLUMN photo_id TEXT;")
            await db.commit()
    except Exception: pass
    try:
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("ALTER TABLE appointments ADD COLUMN admin_comment TEXT;")
            await db.commit()
    except Exception: pass

# ==========================================
# МАШИНЫ СОСТОЯНИЙ (FSM)
# ==========================================

class ClientBooking(StatesGroup):
    category = State()
    service = State()
    date = State()
    time = State()
    phone = State()
    allergies = State()
    photo = State()

class AdminPrice(StatesGroup):
    add_category = State()
    service_category = State()
    service_name = State()
    service_price = State()
    service_photo = State() # Новое состояние для фото услуги

class AdminSchedule(StatesGroup):
    select_date = State()
    add_comment = State() # Новое состояние для заметки

class AdminBroadcast(StatesGroup):
    message = State()

# ==========================================
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ КАЛЕНДАРЯ
# ==========================================

async def highlight_calendar(markup, year: int, month: int):
    """Добавляет маркер 🟢 к дням, на которые есть подтвержденные записи"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT DISTINCT date FROM appointments WHERE date LIKE ? AND status = 'confirmed'",
            (f"{year}-{month:02d}-%",)
        ) as cursor:
            rows = await cursor.fetchall()
            
    booked_days = {int(row[0].split('-')[2]) for row in rows}
    
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.text.isdigit(): 
                day = int(btn.text)
                if day in booked_days:
                    btn.text = f"{day}🟢" 
    return markup

# ==========================================
# КЛАВИАТУРЫ
# ==========================================

def get_main_menu() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(KeyboardButton(text="📋 Прайс-лист"), KeyboardButton(text="📅 Записаться"))
    builder.row(KeyboardButton(text="👤 Мои записи"), KeyboardButton(text="ℹ️ О мастере / Контакты"))
    return builder.as_markup(resize_keyboard=True)

def get_admin_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📥 Новые заявки", callback_data="adm_pending"))
    builder.row(InlineKeyboardButton(text="📅 Выбрать дату расписания", callback_data="adm_sched_calendar"))
    builder.row(InlineKeyboardButton(text="➕ Категорию", callback_data="adm_add_cat"), InlineKeyboardButton(text="➕ Услугу", callback_data="adm_add_serv"))
    builder.row(InlineKeyboardButton(text="❌ Удалить услугу", callback_data="adm_del_serv_list"))
    builder.row(InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast"))
    return builder.as_markup()

# ==========================================
# ОБЩИЕ ХЕНДЛЕРЫ И КЛИЕНТСКИЙ СЦЕНАРИЙ
# ==========================================

@router.message(CommandStart())
async def cmd_start(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)", 
                         (message.from_user.id, message.from_user.username))
        await db.commit()
    
    await message.answer(
        f"✨ Приветствуем вас, {message.from_user.first_name}! ✨\n"
        f"Я — бот-ассистент для онлайн-записи.\n"
        f"Используйте меню ниже для навигации.",
        reply_markup=get_main_menu()
    )

@router.message(F.text == "ℹ️ О мастере / Контакты")
async def cmd_contacts(message: Message):
    await message.answer(
        "👩‍⚕️ <b>О мастере:</b>\n"
        "Ваш сертифицированный косметолог-эстетист.\n"
        "Опыт работы более 5 лет. Индивидуальный подход и премиальные материалы.\n\n"
        "📍 <b>Контакты:</b>\n"
        "Адрес: ул. Красоты, д. 10, кабинет 305\n"
        "Телефон: +7 (999) 123-45-67\n"
        "График: Ежедневно с 10:00 до 20:00",
        parse_mode="HTML"
    )

# Интерактивный прайс-лист
@router.message(F.text == "📋 Прайс-лист")
async def cmd_price(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name FROM categories") as cursor:
            categories = await cursor.fetchall()
            
    if not categories:
        await message.answer("Прайс-лист пока не заполнен.")
        return
        
    builder = InlineKeyboardBuilder()
    for cat_id, cat_name in categories:
        builder.row(InlineKeyboardButton(text=cat_name, callback_data=f"view_cat_{cat_id}"))
        
    await message.answer("📋 <b>Выберите категорию для просмотра услуг:</b>", reply_markup=builder.as_markup(), parse_mode="HTML")

@router.callback_query(F.data.startswith("view_cat_"))
async def view_category_services(callback: CallbackQuery):
    cat_id = int(callback.data.split("_")[2])
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name, price, photo_id FROM services WHERE category_id = ?", (cat_id,)) as cursor:
            services = await cursor.fetchall()
            
    if not services:
        await callback.answer("В этой категории услуг пока нет.", show_alert=True)
        return
        
    await callback.message.answer("✨ <b>Список услуг категории:</b>", parse_mode="HTML")
    for name, price, photo_id in services:
        caption = f"💆‍♀️ <b>{name}</b>\n💰 Цена: {price} руб."
        if photo_id:
            await callback.message.answer_photo(photo=photo_id, caption=caption, parse_mode="HTML")
        else:
            await callback.message.answer(caption, parse_mode="HTML")
    await callback.answer()

# Просмотр своих записей и отмена клиентом
@router.message(F.text == "👤 Мои записи")
async def cmd_my_appointments(message: Message):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT a.id, a.date, a.time, s.name, a.status 
            FROM appointments a
            LEFT JOIN services s ON a.service_id = s.id
            WHERE a.user_id = ? ORDER BY a.date, a.time
        """, (message.from_user.id,)) as cursor:
            rows = await cursor.fetchall()
            
    if not rows:
        await message.answer("У вас пока нет активных записей.")
        return
        
    await message.answer("👤 <b>Ваши визиты:</b>", parse_mode="HTML")
    status_mapping = {"pending": "⏳ Ожидает подтверждения", "confirmed": "✅ Подтверждена", "rejected": "❌ Отклонена"}
    
    for row_id, date, time, name, status in rows:
        service_name = name if name else "Удаленная процедура"
        text = f"📅 <b>{date} в {time}</b>\n💆‍♀️ Процедура: {service_name}\nСтатус: {status_mapping.get(status, status)}"
        
        builder = InlineKeyboardBuilder()
        if status in ['pending', 'confirmed']:
            builder.row(InlineKeyboardButton(text="❌ Отменить запись", callback_data=f"cli_cancel_{row_id}"))
            await message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        else:
            await message.answer(text, parse_mode="HTML")

@router.callback_query(F.data.startswith("cli_cancel_"))
async def cli_cancel_appointment(callback: CallbackQuery):
    app_id = int(callback.data.split("_")[2])
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT date, time FROM appointments WHERE id = ?", (app_id,)) as cursor:
            app = await cursor.fetchone()
        if app:
            await db.execute("UPDATE appointments SET status = 'rejected' WHERE id = ?", (app_id,))
            await db.commit()
            
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(chat_id=admin_id, text=f"⚠️ Клиент отменил свою запись №{app_id} на {app[0]} в {app[1]}")
                except Exception: pass
                
    await callback.message.edit_text(callback.message.text + "\n\n🔴 <i>Вы успешно отменили эту запись.</i>", parse_mode="HTML")
    await callback.answer("Запись отменена")

# ==========================================
# ПРОЦЕСС ЗАПИСИ С ИСПОЛЬЗОВАНИЕМ КАЛЕНДАРЯ
# ==========================================

@router.message(F.text == "📅 Записаться")
async def start_booking(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name FROM categories") as cursor:
            categories = await cursor.fetchall()
            
    if not categories:
        await message.answer("Извините, запись временно недоступна (нет категорий услуг).")
        return

    builder = InlineKeyboardBuilder()
    for cat_id, name in categories:
        builder.row(InlineKeyboardButton(text=name, callback_data=f"book_cat_{cat_id}"))
    
    await state.set_state(ClientBooking.category)
    await message.answer("Выберите интересующую категорию услуг:", reply_markup=builder.as_markup())

@router.callback_query(ClientBooking.category, F.data.startswith("book_cat_"))
async def process_category(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[2])
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name, price FROM services WHERE category_id = ?", (cat_id,)) as cursor:
            services = await cursor.fetchall()
            
    if not services:
        await callback.answer("В этой категории пока нет услуг.", show_alert=True)
        return
        
    builder = InlineKeyboardBuilder()
    for s_id, name, price in services:
        builder.row(InlineKeyboardButton(text=f"{name} ({price} руб.)", callback_data=f"book_serv_{s_id}"))
        
    await state.set_state(ClientBooking.service)
    await callback.message.edit_text("Выберите конкретную услугу:", reply_markup=builder.as_markup())
    await callback.answer()

@router.callback_query(ClientBooking.service, F.data.startswith("book_serv_"))
async def process_service(callback: CallbackQuery, state: FSMContext):
    service_id = int(callback.data.split("_")[2])
    await state.update_data(service_id=service_id)
    
    await state.set_state(ClientBooking.date)
    
    calendar = SimpleCalendar()
    now = datetime.date.today()
    markup = await calendar.start_calendar()
    markup = await highlight_calendar(markup, now.year, now.month)

    await callback.message.edit_text("Выберите дату визита в календаре (🟢 — есть записи):", reply_markup=markup)
    await callback.answer()

@router.callback_query(SimpleCalendarCallback.filter(), ClientBooking.date)
async def process_calendar(callback: CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    calendar = SimpleCalendar()
    selected, date = await calendar.process_selection(callback, callback_data)
    
    if selected:
        if date.date() < datetime.date.today():
            await callback.answer("Нельзя выбрать дату в прошлом!", show_alert=True)
            return

        chosen_date = date.strftime("%Y-%m-%d")
        await state.update_data(date=chosen_date)
        
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT time FROM appointments WHERE date = ? AND status != 'rejected'", (chosen_date,)) as cursor:
                rows = await cursor.fetchall()
                occupied_slots = [row[0] for row in rows]
                
        builder = InlineKeyboardBuilder()
        available_slots = [slot for slot in TIME_SLOTS if slot not in occupied_slots]
        
        if not available_slots:
            await callback.message.answer("К сожалению, на этот день все слоты заняты. Выберите другой день через меню 'Записаться'.")
            await state.clear()
            return
            
        for i in range(0, len(available_slots), 2):
            row_buttons = [InlineKeyboardButton(text=available_slots[i], callback_data=f"book_time_{available_slots[i]}")]
            if i + 1 < len(available_slots):
                row_buttons.append(InlineKeyboardButton(text=available_slots[i+1], callback_data=f"book_time_{available_slots[i+1]}"))
            builder.row(*row_buttons)
            
        await state.set_state(ClientBooking.time)
        await callback.message.edit_text(f"Выбранная дата: {chosen_date}\nТеперь выберите доступное время:", reply_markup=builder.as_markup())
    else:
        markup = await highlight_calendar(callback.message.reply_markup, callback_data.year, callback_data.month)
        await callback.message.edit_reply_markup(reply_markup=markup)

@router.callback_query(ClientBooking.time, F.data.startswith("book_time_"))
async def process_time(callback: CallbackQuery, state: FSMContext):
    chosen_time = callback.data.split("_")[2]
    await state.update_data(time=chosen_time)
    
    await state.set_state(ClientBooking.phone)
    await callback.message.edit_text("Пожалуйста, введите ваш номер телефона для связи:")
    await callback.answer()

@router.message(ClientBooking.phone, F.text)
async def process_phone(message: Message, state: FSMContext):
    await state.update_data(phone=message.text)
    await state.set_state(ClientBooking.allergies)
    await message.answer("Есть ли у вас аллергии или противопоказания? Если нет, напишите 'Нет'.")

@router.message(ClientBooking.allergies, F.text)
async def process_allergies(message: Message, state: FSMContext):
    await state.update_data(allergies=message.text)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Пропустить ➡️", callback_data="skip_photo"))
    
    await state.set_state(ClientBooking.photo)
    await message.answer(
        "Отправьте фото проблемной зоны без макияжа. Или нажмите кнопку 'Пропустить'.", 
        reply_markup=builder.as_markup()
    )

@router.callback_query(ClientBooking.photo, F.data == "skip_photo")
async def skip_photo_callback(callback: CallbackQuery, state: FSMContext):
    await state.update_data(photo_id=None)
    await finish_booking(callback.message, state, callback.from_user.id)
    await callback.answer()

@router.message(ClientBooking.photo, F.photo)
async def process_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await state.update_data(photo_id=photo_id)
    await finish_booking(message, state, message.from_user.id)

@router.message(ClientBooking.photo)
async def process_photo_invalid(message: Message):
    await message.answer("Пожалуйста, отправьте фото или нажмите кнопку 'Пропустить'.")

async def finish_booking(message: Message, state: FSMContext, user_id: int):
    data = await state.get_data()
    await state.clear()
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE users SET phone = ? WHERE user_id = ?", (data['phone'], user_id))
        
        cursor = await db.execute("""
            INSERT INTO appointments (user_id, service_id, date, time, photo_id, allergies_comment)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, data['service_id'], data['date'], data['time'], data['photo_id'], data['allergies']))
        app_id = cursor.lastrowid
        
        async with db.execute("SELECT name, price FROM services WHERE id = ?", (data['service_id'],)) as s_cursor:
            service = await s_cursor.fetchone()
        await db.commit()
        
    await message.answer("🎉 Ваша заявка успешно отправлена! Ожидайте подтверждения от мастера.", reply_markup=get_main_menu())
    
    admin_text = (
        f"🚨 <b>Новая заявка на запись #{app_id}</b>\n\n"
        f"👤 Клиент: ID {user_id} (@{message.from_user.username or 'нет'})\n"
        f"📞 Телефон: {data['phone']}\n"
        f"💆‍♀️ Процедура: {service[0]} ({service[1]} руб.)\n"
        f"📅 Дата и время: {data['date']} в {data['time']}\n"
        f"⚠️ Аллергии/Комментарий: {data['allergies']}\n"
    )
    
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_approve_{app_id}"),
        InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_reject_{app_id}")
    )
    
    for admin_id in ADMIN_IDS:
        try:
            if data['photo_id']:
                await bot.send_photo(chat_id=admin_id, photo=data['photo_id'], caption=admin_text, reply_markup=builder.as_markup(), parse_mode="HTML")
            else:
                await bot.send_message(chat_id=admin_id, text=admin_text, reply_markup=builder.as_markup(), parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка уведомления админа {admin_id}: {e}")

# ==========================================
# ПАНЕЛЬ АДМИНИСТРАТОРА (УПРАВЛЕНИЕ ЗАЯВКАМИ)
# ==========================================

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer("🎛️ Панель управления мастера:", reply_markup=get_admin_menu())

@router.callback_query(F.data.startswith("adm_approve_"))
async def adm_approve(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    app_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT a.user_id, a.date, a.time, s.name 
            FROM appointments a LEFT JOIN services s ON a.service_id = s.id WHERE a.id = ?
        """, (app_id,)) as cursor:
            app = await cursor.fetchone()
            
        if app:
            await db.execute("UPDATE appointments SET status = 'confirmed' WHERE id = ?", (app_id,))
            await db.commit()
            
            try:
                await bot.send_message(
                    chat_id=app[0],
                    text=f"✅ <b>Ваша запись подтверждена!</b>\n\n💆‍♀️ Процедура: {app[3] or 'Услуга'}\n📅 Дата: {app[1]}\n⏰ Время: {app[2]}\n\nЖдем вас! 🥰",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"Не удалось уведомить клиента: {e}")
                
            await callback.message.edit_text(callback.message.text + "\n\n🟢 Статус: Подтверждена")
    await callback.answer("Запись подтверждена")

@router.callback_query(F.data.startswith("adm_reject_"))
async def adm_reject(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    app_id = int(callback.data.split("_")[2])
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, date, time FROM appointments WHERE id = ?", (app_id,)) as cursor:
            app = await cursor.fetchone()
            
        if app:
            await db.execute("UPDATE appointments SET status = 'rejected' WHERE id = ?", (app_id,))
            await db.commit()
            
            try:
                await bot.send_message(
                    chat_id=app[0],
                    text=f"❌ Извините, запись на {app[1]} в {app[2]} была отклонена мастером."
                )
            except Exception as e:
                logging.error(f"Не удалось уведомить клиента: {e}")
                
            await callback.message.edit_text(callback.message.text + "\n\n🔴 Статус: Отклонена")
    await callback.answer("Запись отклонена")

@router.callback_query(F.data == "adm_pending")
async def adm_view_pending(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("""
            SELECT a.id, a.user_id, a.date, a.time, s.name, u.phone 
            FROM appointments a 
            LEFT JOIN services s ON a.service_id = s.id 
            LEFT JOIN users u ON a.user_id = u.user_id
            WHERE a.status = 'pending'
        """) as cursor:
            rows = await cursor.fetchall()
            
    if not rows:
        await callback.message.answer("Новых заявок на запись нет.")
        await callback.answer()
        return
        
    for row in rows:
        service_name = row[4] if row[4] else "Удаленная процедура"
        phone_num = row[5] if row[5] else "Не указан"
        text = f"📋 Заявка #{row[0]}\nКлиент: ID {row[1]}\nТел: {phone_num}\nПроцедура: {service_name}\nДата/Время: {row[2]} в {row[3]}"
        
        builder = InlineKeyboardBuilder()
        builder.row(
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm_approve_{row[0]}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"adm_reject_{row[0]}")
        )
        await callback.message.answer(text, reply_markup=builder.as_markup())
    await callback.answer()

# Календарь Админа
@router.callback_query(F.data == "adm_sched_calendar")
async def adm_sched_calendar(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS: return
    
    await state.set_state(AdminSchedule.select_date)
    calendar = SimpleCalendar()
    
    now = datetime.date.today()
    markup = await calendar.start_calendar()
    markup = await highlight_calendar(markup, now.year, now.month)

    await callback.message.edit_text("Выберите дату расписания (🟢 — есть записи):", reply_markup=markup)
    await callback.answer()

@router.callback_query(SimpleCalendarCallback.filter(), AdminSchedule.select_date)
async def process_admin_calendar(callback: CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    calendar = SimpleCalendar()
    selected, date = await calendar.process_selection(callback, callback_data)
    
    if selected:
        await state.clear() 
        date_str = date.strftime("%Y-%m-%d")
        day_name_ru = RU_DAYS[date.weekday()]
        
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("""
                SELECT a.id, a.time, s.name, u.phone, u.username, a.photo_id, a.allergies_comment, a.admin_comment 
                FROM appointments a
                LEFT JOIN services s ON a.service_id = s.id
                LEFT JOIN users u ON a.user_id = u.user_id
                WHERE a.date = ? AND a.status = 'confirmed'
                ORDER BY a.time
            """, (date_str,)) as cursor:
                rows = await cursor.fetchall()
                
        if not rows:
            await callback.message.answer(f"На {date_str} ({day_name_ru}) подтвержденных записей нет.")
            await callback.answer()
            return
            
        await callback.message.answer(f"📅 Расписание на {date_str} ({day_name_ru}):")
        for row in rows:
            app_id, time, s_name, phone, username, photo_id, allergies, admin_comment = row
            s_name = s_name if s_name else "Удаленная процедура"
            
            text = f"⏰ <b>{time}</b>\n💆‍♀️ {s_name}\n📞 Тел: {phone or 'Не указан'}\n👤 Аккаунт: @{username or 'нет'}\n⚠️ Клиент: {allergies}"
            if admin_comment:
                text += f"\n📝 <b>Заметка мастера:</b> {admin_comment}"
                
            builder = InlineKeyboardBuilder()
            builder.row(
                InlineKeyboardButton(text="📝 Заметка", callback_data=f"adm_note_{app_id}"),
                InlineKeyboardButton(text="❌ Отменить", callback_data=f"adm_cancel_{app_id}")
            )

            if photo_id:
                await callback.message.answer_photo(photo=photo_id, caption=text, reply_markup=builder.as_markup(), parse_mode="HTML")
            else:
                await callback.message.answer(text, reply_markup=builder.as_markup(), parse_mode="HTML")
        await callback.answer()
    else:
        markup = await highlight_calendar(callback.message.reply_markup, callback_data.year, callback_data.month)
        await callback.message.edit_reply_markup(reply_markup=markup)

# Отмена и комментарии мастера (хендлеры)
@router.callback_query(F.data.startswith("adm_cancel_"))
async def adm_cancel_appointment(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    app_id = int(callback.data.split("_")[2])
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, date, time FROM appointments WHERE id = ?", (app_id,)) as cursor:
            app = await cursor.fetchone()
        if app:
            await db.execute("UPDATE appointments SET status = 'rejected' WHERE id = ?", (app_id,))
            await db.commit()
            try:
                await bot.send_message(chat_id=app[0], text=f"🔴 Извините, ваша запись на {app[1]} в {app[2]} была отменена мастером.")
            except Exception: pass
            
    # Удаляем клавиатуру и пишем что отменено
    await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.reply("🔴 Запись успешно отменена мастером.")
    await callback.answer("Запись отменена")

@router.callback_query(F.data.startswith("adm_note_"))
async def adm_start_note(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS: return
    app_id = int(callback.data.split("_")[2])
    
    await state.update_data(note_app_id=app_id)
    await state.set_state(AdminSchedule.add_comment)
    await callback.message.answer("Введите текст внутренней заметки для этой записи:")
    await callback.answer()

@router.message(AdminSchedule.add_comment, F.text)
async def adm_save_note(message: Message, state: FSMContext):
    data = await state.get_data()
    app_id = data['note_app_id']
    await state.clear()
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE appointments SET admin_comment = ? WHERE id = ?", (message.text, app_id))
        await db.commit()
        
    await message.answer("✅ Заметка к записи успешно сохранена!")

# Управление прайсом
@router.callback_query(F.data == "adm_add_cat")
async def adm_add_cat_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminPrice.add_category)
    await callback.message.answer("Введите название новой категории:")
    await callback.answer()

@router.message(AdminPrice.add_category, F.text)
async def adm_add_cat_finish(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO categories (name) VALUES (?)", (message.text,))
        await db.commit()
    await state.clear()
    await message.answer(f"Категория '{message.text}' успешно создана!", reply_markup=get_admin_menu())

@router.callback_query(F.data == "adm_add_serv")
async def adm_add_serv_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS: return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name FROM categories") as cursor:
            categories = await cursor.fetchall()
            
    if not categories:
        await callback.message.answer("Сначала добавьте хотя бы одну категорию.")
        await callback.answer()
        return
        
    builder = InlineKeyboardBuilder()
    for cat_id, name in categories:
        builder.row(InlineKeyboardButton(text=name, callback_data=f"adm_sel_cat_{cat_id}"))
        
    await state.set_state(AdminPrice.service_category)
    await callback.message.answer("Выберите категорию для новой услуги:", reply_markup=builder.as_markup())
    await callback.answer()

@router.callback_query(AdminPrice.service_category, F.data.startswith("adm_sel_cat_"))
async def adm_add_serv_cat_chosen(callback: CallbackQuery, state: FSMContext):
    cat_id = int(callback.data.split("_")[3])
    await state.update_data(cat_id=cat_id)
    await state.set_state(AdminPrice.service_name)
    await callback.message.edit_text("Введите название услуги:")
    await callback.answer()

@router.message(AdminPrice.service_name, F.text)
async def adm_add_serv_name_chosen(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await state.set_state(AdminPrice.service_price)
    await message.answer("Введите стоимость услуги (число):")

@router.message(AdminPrice.service_price, F.text)
async def adm_add_serv_price_chosen(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите корректное число.")
        return
        
    await state.update_data(price=int(message.text))
    await state.set_state(AdminPrice.service_photo)
    
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="Пропустить ➡️", callback_data="skip_serv_photo"))
    await message.answer("Отправьте ФОТОГРАФИЮ для услуги или нажмите 'Пропустить':", reply_markup=builder.as_markup())

@router.callback_query(AdminPrice.service_photo, F.data == "skip_serv_photo")
async def skip_serv_photo(callback: CallbackQuery, state: FSMContext):
    await save_service_to_db(callback.message, state, None)
    await callback.answer()

@router.message(AdminPrice.service_photo, F.photo)
async def process_serv_photo(message: Message, state: FSMContext):
    photo_id = message.photo[-1].file_id
    await save_service_to_db(message, state, photo_id)

async def save_service_to_db(message: Message, state: FSMContext, photo_id: str | None):
    data = await state.get_data()
    await state.clear()
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT INTO services (category_id, name, price, photo_id) VALUES (?, ?, ?, ?)", 
                         (data['cat_id'], data['name'], data['price'], photo_id))
        await db.commit()
    await message.answer(f"Услуга '{data['name']}' успешно добавлена!", reply_markup=get_admin_menu())

@router.callback_query(F.data == "adm_del_serv_list")
async def adm_del_serv_list(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS: return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name FROM services") as cursor:
            services = await cursor.fetchall()
            
    if not services:
        await callback.message.answer("Прайс пуст.")
        await callback.answer()
        return
        
    builder = InlineKeyboardBuilder()
    for s_id, name in services:
        builder.row(InlineKeyboardButton(text=name, callback_data=f"adm_del_srv_{s_id}"))
        
    await callback.message.edit_text("Выберите услугу для удаления:", reply_markup=builder.as_markup())
    await callback.answer()

@router.callback_query(F.data.startswith("adm_del_srv_"))
async def adm_del_serv_finish(callback: CallbackQuery):
    s_id = int(callback.data.split("_")[3])
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM services WHERE id = ?", (s_id,))
        await db.commit()
    await callback.message.edit_text("Услуга успешно удалена!", reply_markup=get_admin_menu())
    await callback.answer()

# Рассылка
@router.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS: return
    await state.set_state(AdminBroadcast.message)
    await callback.message.answer("Введите текст сообщения для рассылки:")
    await callback.answer()

@router.message(AdminBroadcast.message, F.text)
async def adm_broadcast_finish(message: Message, state: FSMContext):
    text_to_send = message.text
    await state.clear()
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()
            
    count = 0
    for user in users:
        try:
            await bot.send_message(chat_id=user[0], text=text_to_send)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
            
    await message.answer(f"Рассылка завершена. Отправлено: {count}", reply_markup=get_admin_menu())

# ==========================================
# ФОНОВАЯ ЗАДАЧА: НАПОМИНАНИЯ (ЗА 24 ЧАСА)
# ==========================================

async def reminder_scheduler():
    while True:
        try:
            now = datetime.datetime.now()
            async with aiosqlite.connect(DB_NAME) as db:
                async with db.execute("""
                    SELECT a.id, a.user_id, a.date, a.time, s.name 
                    FROM appointments a
                    LEFT JOIN services s ON a.service_id = s.id
                    WHERE a.status = 'confirmed' AND a.reminded = 0
                """) as cursor:
                    appointments = await cursor.fetchall()
                    
                for app_id, user_id, date_str, time_str, service_name in appointments:
                    try:
                        app_datetime = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
                        time_delta = app_datetime - now
                        
                        if 0 < time_delta.total_seconds() <= 86400:
                            s_name = service_name if service_name else "процедуру"
                            await bot.send_message(
                                chat_id=user_id,
                                text=f"🔔 <b>Напоминание о визите!</b>\n\n"
                                     f"Завтра у вас визит к косметологу в <b>{time_str}</b> на процедуру <b>{s_name}</b>.\n"
                                     f"Ждем вас! Если планы изменились, пожалуйста, предупредите мастера.",
                                parse_mode="HTML"
                            )
                            await db.execute("UPDATE appointments SET reminded = 1 WHERE id = ?", (app_id,))
                            await db.commit()
                    except Exception as e:
                        logging.error(f"Ошибка обработки напоминания для записи #{app_id}: {e}")
                        
        except Exception as e:
            logging.error(f"Ошибка в планировщике: {e}")
            
        await asyncio.sleep(60) 

# ==========================================
# ЗАПУСК БОТА
# ==========================================

async def main():
    await init_db()
    dp.include_router(router)
    asyncio.create_task(reminder_scheduler())
    logging.info("Финальная версия бота (со всеми функциями) успешно запущена.")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())