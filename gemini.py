import asyncio
import sqlite3
import os
import time
import logging
import sys
import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, RPCError

# --- НАСТРОЙКА ЛОГИРОВАНИЯ ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# --- КОНФИГУРАЦИЯ ---
API_TOKEN = '8648072212:AAE-hC9VtVpHpAgdY3tgj8GNNEucu1QfRXc'
API_ID = 20652575
API_HASH = 'c0d5c94ec3c668444dca9525940d876d'
ADMIN_ID = 7785932103

bot = Bot(token=API_TOKEN)
dp = Dispatcher()

# База данных
db = sqlite3.connect('bot_data.db', check_same_thread=False, timeout=30)
cur = db.cursor()


def init_db():
    cur.execute('''CREATE TABLE IF NOT EXISTS accounts 
                   (phone TEXT PRIMARY KEY, owner_id INTEGER, expires INTEGER, 
                    text TEXT DEFAULT 'Привет!', photo_id TEXT, 
                    interval INTEGER DEFAULT 5, chats TEXT DEFAULT '',
                    is_running INTEGER DEFAULT 0)''')
    db.commit()


init_db()


class States(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_password = State()
    waiting_for_rent_time = State()
    edit_text = State()
    edit_interval = State()
    edit_chats = State()
    add_photo = State()


def main_menu():
    kb = ReplyKeyboardBuilder()
    kb.button(text="📂 Каталог аккаунтов")
    kb.button(text="🔑 Моя аренда")
    return kb.as_markup(resize_keyboard=True)


# --- СТАРТ ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer("🤖 Бот запущен и готов к работе.", reply_markup=main_menu())


# --- УПРАВЛЕНИЕ АККАУНТАМИ (АДМИН) ---
@dp.message(Command("addacc"))
async def add_acc_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("Введите номер телефона (+7...):")
    await state.set_state(States.waiting_for_phone)


@dp.message(States.waiting_for_phone)
async def process_phone(message: types.Message, state: FSMContext):
    phone = message.text.strip().replace(" ", "")
    client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
    await client.connect()
    try:
        sent_code = await client.send_code_request(phone)
        await state.update_data(phone=phone, hash=sent_code.phone_code_hash)
        await message.answer(f"📩 Код отправлен на {phone}. Введите его:")
        await state.set_state(States.waiting_for_code)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        await state.clear()
    finally:
        await client.disconnect()


@dp.message(States.waiting_for_code)
async def process_code(message: types.Message, state: FSMContext):
    data = await state.get_data()
    phone, code = data['phone'], message.text.strip()
    client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(phone, code, phone_code_hash=data['hash'])
        cur.execute('INSERT OR REPLACE INTO accounts (phone, owner_id, expires, is_running) VALUES (?, NULL, 0, 0)',
                    (phone,))
        db.commit()
        await message.answer(f"✅ Аккаунт {phone} успешно добавлен.")
        await state.clear()
    except SessionPasswordNeededError:
        await message.answer("🔐 На аккаунте 2FA. Введите пароль:")
        await state.set_state(States.waiting_for_password)
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")
        await state.clear()
    finally:
        await client.disconnect()


@dp.message(States.waiting_for_password)
async def process_password(message: types.Message, state: FSMContext):
    data = await state.get_data()
    phone = data['phone']
    password = message.text.strip()

    client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
    await client.connect()
    try:
        await client.sign_in(password=password)
        cur.execute('INSERT OR REPLACE INTO accounts (phone, owner_id, expires, is_running) VALUES (?, NULL, 0, 0)',
                    (phone,))
        db.commit()
        await message.answer(f"✅ Аккаунт {phone} успешно добавлен (2FA пройдена).")
        await state.clear()
    except Exception as e:
        await message.answer(f"❌ Ошибка пароля или входа: {e}")
        await state.clear()
    finally:
        await client.disconnect()


@dp.message(Command("delacc"))
async def del_acc_cmd(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args:
        return await message.answer("⚠️ Укажите номер. Пример: `/delacc +79991234567`", parse_mode="Markdown")

    phone = command.args.strip().replace(" ", "")
    cur.execute('DELETE FROM accounts WHERE phone = ?', (phone,))
    db.commit()

    session_path = f"sessions/{phone}.session"
    if os.path.exists(session_path):
        try:
            os.remove(session_path)
        except PermissionError:
            pass

    await message.answer(f"🗑 Аккаунт {phone} успешно удален.")


# --- КАТАЛОГ И АРЕНДА ---
@dp.message(F.text == "📂 Каталог аккаунтов")
async def show_catalog(message: types.Message):
    now = int(time.time())
    cur.execute('SELECT phone FROM accounts WHERE owner_id IS NULL OR expires < ?', (now,))
    free_accs = cur.fetchall()
    if not free_accs: return await message.answer("📭 Свободных аккаунтов нет.")
    kb = InlineKeyboardBuilder()
    for (phone,) in free_accs:
        kb.button(text=f"📱 {phone}", callback_data=f"rent_init_{phone}")
    kb.adjust(2)
    await message.answer("Выберите аккаунт для аренды:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("rent_init_"))
async def rent_input_time(call: types.CallbackQuery, state: FSMContext):
    phone = call.data.replace("rent_init_", "").strip()
    await state.update_data(rent_phone=phone)
    await call.message.answer(f"На сколько минут арендовать {phone}?")
    await state.set_state(States.waiting_for_rent_time)


@dp.message(States.waiting_for_rent_time)
async def process_rent_finish(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Введите число минут.")
    mins = int(message.text)
    data = await state.get_data()
    expires = int(time.time()) + (mins * 60)
    cur.execute('UPDATE accounts SET owner_id = ?, expires = ?, is_running = 0 WHERE phone = ?',
                (message.from_user.id, expires, data['rent_phone']))
    db.commit()
    await message.answer(f"✅ Аккаунт {data['rent_phone']} арендован на {mins} мин.", reply_markup=main_menu())
    await state.clear()


# --- УПРАВЛЕНИЕ АРЕНДОЙ ---
@dp.message(F.text == "🔑 Моя аренда")
async def my_rent(message: types.Message):
    now = int(time.time())
    cur.execute('SELECT phone, expires FROM accounts WHERE owner_id = ? AND expires > ?', (message.from_user.id, now))
    rented = cur.fetchall()
    if not rented: return await message.answer("У вас нет активных аренд.")
    kb = InlineKeyboardBuilder()
    for (phone, exp) in rented:
        kb.button(text=f"⚙️ {phone}", callback_data=f"manage_{phone}")
    kb.adjust(1)
    await message.answer("Ваши арендованные аккаунты:", reply_markup=kb.as_markup())


@dp.callback_query(F.data.startswith("manage_"))
async def manage_acc(call: types.CallbackQuery):
    phone = call.data.replace("manage_", "").strip()
    cur.execute('SELECT text, photo_id, interval, chats, is_running FROM accounts WHERE phone = ?', (phone,))
    res = cur.fetchone()
    if not res: return
    text, photo, interval, chats, is_running = res
    status = "🟢 ЗАПУЩЕНА" if is_running else "⚪️ ОСТАНОВЛЕНА"

    kb = InlineKeyboardBuilder()
    kb.button(text="📝 Текст", callback_data=f"edit_text_{phone}")
    kb.button(text="⏱ Инт.", callback_data=f"edit_int_{phone}")
    kb.button(text="👥 Чаты", callback_data=f"edit_chats_{phone}")
    kb.button(text="🖼 Фото", callback_data=f"edit_photo_{phone}")
    kb.button(text="🛑 СТОП" if is_running else "🚀 ПУСК", callback_data=f"{'stop' if is_running else 'run'}_{phone}")
    kb.adjust(2, 2, 1)

    try:
        await call.message.edit_text(
            f"🛠 **Настройки {phone}**\n\nСтатус: {status}\nИнтервал: {interval} сек.\nТекст: {text[:50]}...",
            reply_markup=kb.as_markup(), parse_mode="Markdown"
        )
    except Exception:
        pass


# --- ОБРАБОТЧИКИ КНОПОК РЕДАКТИРОВАНИЯ ---
@dp.callback_query(F.data.startswith("edit_text_"))
async def edit_text_call(call: types.CallbackQuery, state: FSMContext):
    phone = call.data.replace("edit_text_", "").strip()
    await state.update_data(edit_phone=phone)
    await call.message.answer(f"Введите новый текст для {phone}:")
    await state.set_state(States.edit_text)


@dp.message(States.edit_text)
async def save_text(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cur.execute('UPDATE accounts SET text = ? WHERE phone = ?', (message.text, data['edit_phone']))
    db.commit()
    await message.answer("✅ Текст обновлен.")
    await state.clear()


@dp.callback_query(F.data.startswith("edit_int_"))
async def edit_int_call(call: types.CallbackQuery, state: FSMContext):
    phone = call.data.replace("edit_int_", "").strip()
    await state.update_data(edit_phone=phone)
    await call.message.answer("Введите интервал в секундах (число):")
    await state.set_state(States.edit_interval)


@dp.message(States.edit_interval)
async def save_interval(message: types.Message, state: FSMContext):
    if not message.text.isdigit(): return await message.answer("Нужно число.")
    data = await state.get_data()
    cur.execute('UPDATE accounts SET interval = ? WHERE phone = ?', (int(message.text), data['edit_phone']))
    db.commit()
    await message.answer("✅ Интервал обновлен.")
    await state.clear()


@dp.callback_query(F.data.startswith("edit_chats_"))
async def edit_chats_call(call: types.CallbackQuery, state: FSMContext):
    phone = call.data.replace("edit_chats_", "").strip()
    await state.update_data(edit_phone=phone)
    await call.message.answer("Пришлите список чатов через запятую или с новой строки:")
    await state.set_state(States.edit_chats)


@dp.message(States.edit_chats)
async def save_chats(message: types.Message, state: FSMContext):
    data = await state.get_data()
    cur.execute('UPDATE accounts SET chats = ? WHERE phone = ?', (message.text, data['edit_phone']))
    db.commit()
    await message.answer("✅ Список чатов обновлен.")
    await state.clear()


@dp.callback_query(F.data.startswith("edit_photo_"))
async def edit_photo_call(call: types.CallbackQuery, state: FSMContext):
    phone = call.data.replace("edit_photo_", "").strip()
    await state.update_data(edit_phone=phone)
    await call.message.answer("Отправьте фото (как файл или картинку) или напишите 'нет' для удаления:")
    await state.set_state(States.add_photo)


@dp.message(States.add_photo)
async def save_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    photo_id = message.photo[-1].file_id if message.photo else None
    if message.text and message.text.lower() == 'нет': photo_id = None
    cur.execute('UPDATE accounts SET photo_id = ? WHERE phone = ?', (photo_id, data['edit_phone']))
    db.commit()
    await message.answer("✅ Фото обновлено.")
    await state.clear()


# --- ЛОГИКА ПУСКА / СТОПА ---
@dp.callback_query(F.data.startswith("run_"))
async def run_cmd(call: types.CallbackQuery):
    phone = call.data.replace("run_", "").strip()
    cur.execute('UPDATE accounts SET is_running = 1 WHERE phone = ?', (phone,))
    db.commit()
    asyncio.create_task(broadcast_loop(phone, call.from_user.id))
    await manage_acc(call)


@dp.callback_query(F.data.startswith("stop_"))
async def stop_cmd(call: types.CallbackQuery):
    phone = call.data.replace("stop_", "").strip()
    cur.execute('UPDATE accounts SET is_running = 0 WHERE phone = ?', (phone,))
    db.commit()
    await manage_acc(call)


# --- ЦИКЛ РАССЫЛКИ ---
async def broadcast_loop(phone, user_id):
    logger.info(f"--- [START] Рассылка {phone} ---")
    client = TelegramClient(f"sessions/{phone}", API_ID, API_HASH)
    try:
        await client.connect()
        while True:
            cur.execute('SELECT is_running, expires, text, photo_id, interval, chats FROM accounts WHERE phone = ?',
                        (phone,))
            res = cur.fetchone()
            if not res or res[0] == 0: break

            is_run, expires, text, photo_id, interval, chats_str = res
            if int(time.time()) > expires:
                cur.execute('UPDATE accounts SET is_running = 0, owner_id = NULL WHERE phone = ?', (phone,))
                db.commit()
                await bot.send_message(user_id, f"⏰ Аренда {phone} истекла.")
                break

            if not client.is_connected():
                try:
                    await client.connect()
                except:
                    await asyncio.sleep(10)
                    continue

            chats = [c.strip() for c in chats_str.replace('\n', ',').split(',') if c.strip()]
            if not chats:
                await bot.send_message(user_id, f"⚠️ У аккаунта {phone} не заданы чаты. Остановка.")
                cur.execute('UPDATE accounts SET is_running = 0 WHERE phone = ?', (phone,))
                db.commit()
                break

            for chat in chats:
                cur.execute('SELECT is_running FROM accounts WHERE phone = ?', (phone,))
                if cur.fetchone()[0] == 0: break
                try:
                    if photo_id:
                        file = await bot.get_file(photo_id)
                        path = f"temp_{phone}.jpg"
                        await bot.download_file(file.file_path, path)
                        await client.send_file(chat, path, caption=text)
                        if os.path.exists(path): os.remove(path)
                    else:
                        await client.send_message(chat, text)
                    logger.info(f"[{phone}] -> {chat} ✅")
                except RPCError as e:
                    err = str(e).upper()
                    if "CHAT_WRITE_FORBIDDEN" in err:
                        logger.error(f"[{phone}] ❌ Нет прав на запись в {chat}.")
                    elif "PEER_ID_INVALID" in err:
                        logger.error(f"[{phone}] ❌ Аккаунт не вступил в {chat}.")
                    else:
                        logger.error(f"[{phone}] ❌ Ошибка TG в {chat}: {e}")
                except Exception as e:
                    logger.error(f"[{phone}] ❌ Ошибка в {chat}: {e}")

                await asyncio.sleep(interval)
            await asyncio.sleep(5)
    except Exception as e:
        logger.error(f"Критическая ошибка рассылки {phone}: {e}")
    finally:
        if client.is_connected(): await client.disconnect()


# --- БЕЗОПАСНЫЙ ЦИКЛ ПОЛЛИНГА ---
async def start_polling_safe():
    while True:
        try:
            logger.info("Запуск Polling бота...")
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)
        except (aiohttp.ClientConnectorError, asyncio.TimeoutError):
            logger.error("Сеть api.telegram.org недоступна. Жду 5 секунд...")
            await asyncio.sleep(5)
        except Exception as e:
            logger.critical(f"Глобальная ошибка: {e}")
            await asyncio.sleep(5)


# --- ТОЧКА ВХОДА ---
async def main():
    if not os.path.exists('sessions'): os.makedirs('sessions')
    await start_polling_safe()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот выключен.")