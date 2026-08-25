import asyncio
import os
import random
import string
import secrets
import html
import sqlite3
import aiohttp
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest
from database import Database

# ==========================================
# КОНФИГУРАЦИЯ И КОНСТАНТЫ
# ==========================================
BOT_TOKEN = os.getenv('BOT_TOKEN', '8985331836:AAFTZPxdYegAhk-EYwqE_JDtKTN9HXD1uA4')
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db = Database()

ADMIN_IDS = [7921743592]

# Смещение часового пояса для отметки времени оплаты в отредактированном счете (МСК)
PAID_TIME_TZ_OFFSET_HOURS = 3

def format_paid_datetime():
    """Дата и время оплаты для отметки в счете, в формате '22.08.2026 в 18:10 (GMT+3)'."""
    now = datetime.utcnow() + timedelta(hours=PAID_TIME_TZ_OFFSET_HOURS)
    sign = "+" if PAID_TIME_TZ_OFFSET_HOURS >= 0 else "-"
    return now.strftime(f"%d.%m.%Y в %H:%M (GMT{sign}{abs(PAID_TIME_TZ_OFFSET_HOURS)})")

crypto_websites = {
    "USDT": "https://tether.to", "GRAM": "https://ton.org", "SOL": "https://solana.com",
    "TRX": "https://tron.network", "BTC": "https://bitcoin.org", "ETH": "https://ethereum.org",
    "DOGE": "https://dogecoin.com", "LTC": "https://litecoin.org", "BNB": "https://www.bnbchain.org",
    "USDC": "https://www.centre.io/usdc", "XAUT": "https://tether.to/en/tether-gold/"
}

USD_RATES = {
    "USDT": 1.0, "USDC": 1.0, "BTC": 77235, "ETH": 2431, "SOL": 93,
    "GRAM": 1.78, "TRX": 0.3431, "DOGE": 0.1, "LTC": 51, "BNB": 697, "XAUT": 4579
}
# ^ Это только резервные значения на случай, если при старте бота курсы ещё не
# успели подтянуться с биржи/агрегатора (см. ниже). В рабочем режиме bot обновляет
# эти же ключи реальными курсами — весь остальной код обращается к USD_RATES.get(...)
# и никаких других правок не требует.

# Соответствие внутренних тикеров идентификаторам CoinGecko — публичное бесплатное
# API курсов криптовалют, ключ не нужен.
COINGECKO_IDS = {
    "USDT": "tether", "USDC": "usd-coin", "BTC": "bitcoin", "ETH": "ethereum",
    "SOL": "solana", "GRAM": "the-open-network", "TRX": "tron", "DOGE": "dogecoin",
    "LTC": "litecoin", "BNB": "binancecoin", "XAUT": "tether-gold",
}

RATES_REFRESH_INTERVAL_SECONDS = 5 * 60  # как часто обновлять курсы, пока бот работает
RATES_REQUEST_TIMEOUT_SECONDS = 10
rates_last_updated = None  # datetime последнего успешного обновления (UTC), для /rates и т.п.

async def fetch_crypto_rates():
    """Подтягивает актуальные курсы криптовалют к USD с CoinGecko и обновляет USD_RATES.

    При любой ошибке (нет сети, API недоступен, некорректный ответ) прежние курсы
    НЕ затираются — бот продолжает работать на последних известных значениях,
    просто выводится предупреждение в лог. Так платежи не считаются по нулевым
    или битым курсам, даже если API временно лежит."""
    global rates_last_updated
    ids = ",".join(COINGECKO_IDS.values())
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd"
    try:
        timeout = aiohttp.ClientTimeout(total=RATES_REQUEST_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    print(f"[WARN] Курсы валют: CoinGecko вернул HTTP {resp.status}, "
                          f"использую прежние значения")
                    return
                data = await resp.json()

        updated = []
        for ticker, cg_id in COINGECKO_IDS.items():
            price = data.get(cg_id, {}).get("usd")
            if isinstance(price, (int, float)) and price > 0:
                USD_RATES[ticker] = float(price)
                updated.append(ticker)

        if updated:
            rates_last_updated = datetime.utcnow()
            print(f"[INFO] Курсы валют обновлены ({len(updated)}/{len(COINGECKO_IDS)}): "
                  f"{', '.join(updated)}")
        else:
            print("[WARN] Курсы валют: ответ CoinGecko не содержал ни одного известного тикера, "
                  "использую прежние значения")
    except asyncio.TimeoutError:
        print("[WARN] Курсы валют: тайм-аут запроса к CoinGecko, использую прежние значения")
    except Exception as e:
        print(f"[WARN] Курсы валют: ошибка при обновлении ({e}), использую прежние значения")

async def rates_refresh_loop():
    """Фоновая задача: обновляет курсы каждые RATES_REFRESH_INTERVAL_SECONDS,
    пока бот работает."""
    while True:
        await asyncio.sleep(RATES_REFRESH_INTERVAL_SECONDS)
        await fetch_crypto_rates()

CURRENCY_ORDER = ["USDT", "GRAM", "SOL", "TRX", "BTC", "ETH", "DOGE", "LTC", "BNB", "USDC", "XAUT"]

CRYPTO_EMOJIS = {
    "USDT": "5406841020769936275", "GRAM": "5318901904686754959", "SOL": "5407016676342401484",
    "TRX": "5406978786140918829", "BTC": "5409133571233319295", "ETH": "5406930321729948822",
    "DOGE": "5406581441536495663", "LTC": "5407128573125366746", "BNB": "5406671889252781489",
    "USDC": "5406575600380974539", "XAUT": "5407080001340215945"
}

# Словарь случайных английских слов для генерации имен приложений
RANDOM_WORDS = [
    "Alpha", "Beta", "Gamma", "Delta", "Omega", "Sigma", "Theta", "Zeta", "Epsilon", "Lambda",
    "Quantum", "Nexus", "Vertex", "Apex", "Zenith", "Nova", "Pulse", "Flux", "Core", "Prime",
    "Cyber", "Neon", "Solar", "Lunar", "Stellar", "Cosmic", "Galactic", "Orbital", "Atomic", "Digital",
    "Crystal", "Phasic", "Arawana", "Velvet", "Azure", "Crimson", "Golden", "Silver", "Bronze", "Iron",
    "Rapid", "Swift", "Echo", "Shadow", "Light", "Storm", "Thunder", "Blaze", "Frost", "Spark"
]

user_states = {}

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
def format_balance(value):
    if value == 0:
        return "0"
    return f"{value:.8f}".rstrip('0').rstrip('.')

def format_usd(value):
    """Форматирует сумму в USD с округлением до центов (для эквивалентов при конвертации из крипты)."""
    if value == 0:
        return "0"
    return f"{value:.2f}".rstrip('0').rstrip('.')

def generate_invoice_id():
    while True:
        invoice_id = "IV" + ''.join(random.choices(string.digits, k=8))
        if not db.get_invoice(invoice_id):
            return invoice_id

def generate_app_id():
    """Генерирует уникальный ID приложения в формате #A + 6 цифр"""
    while True:
        app_id = "#A" + ''.join(random.choices(string.digits, k=6))
        # Проверка уникальности через БД (предполагается наличие метода или можно хранить в памяти)
        # Для простоты здесь используем проверку по длине, в реальном проекте нужен запрос к БД
        # Если метод get_app_by_id есть в Database, используйте его:
        try:
            if hasattr(db, 'get_app_by_id') and db.get_app_by_id(app_id):
                continue
        except:
            pass
        return app_id

def generate_api_token():
    """Генерирует токен вида 6ЦИФР:БАЗА_ТОКЕНА"""
    prefix = ''.join(random.choices(string.digits, k=6))
    suffix = ''.join(random.choices(string.ascii_letters + string.digits, k=24))
    return f"{prefix}:{suffix}"

def generate_random_app_name():
    w1 = random.choice(RANDOM_WORDS)
    w2 = random.choice(RANDOM_WORDS)
    return f"{w1} {w2} App"

APP_NAME_EMOJI_ID = "5361781191722699867"

def app_display_name(name):
    """Возвращает экранированное имя приложения с эмодзи-иконкой перед названием (для текстов с parse_mode='HTML')."""
    return f'<tg-emoji emoji-id="{APP_NAME_EMOJI_ID}">🛒</tg-emoji> {html.escape(name)}'

def get_sorted_currencies(user_id):
    balances = db.get_all_balances(user_id)
    all_zero = all(balances.get(curr, 0) == 0 for curr in CURRENCY_ORDER)
    if all_zero:
        return CURRENCY_ORDER.copy()
    else:
        return sorted(CURRENCY_ORDER, key=lambda x: (-balances.get(x, 0), CURRENCY_ORDER.index(x)))

def get_wallet_text(user_id: int):
    b = db.get_all_balances(user_id)
    if not b:
        b = {k: 0.0 for k in CURRENCY_ORDER}
    
    total_btc = sum([
        b.get("USDT", 0)*0.00001, b.get("GRAM", 0)*0.0000001, b.get("SOL", 0)*0.002,
        b.get("TRX", 0)*0.000002, b.get("BTC", 0), b.get("ETH", 0)*0.03,
        b.get("DOGE", 0)*0.000001, b.get("LTC", 0)*0.001,
        b.get("BNB", 0)*0.005, b.get("USDC", 0)*0.00001, b.get("XAUT", 0)*0.03
    ])
    
    sorted_currencies = get_sorted_currencies(user_id)
    text = f"<b><tg-emoji emoji-id='5310191758255099001'>👛</tg-emoji> Кошелек</b>\n\n"
    for currency in sorted_currencies:
        emoji_id = CRYPTO_EMOJIS[currency]
        balance = b.get(currency, 0)
        website = crypto_websites[currency]
        text += f"<tg-emoji emoji-id='{emoji_id}'>☺️</tg-emoji>  <a href='{website}'>{currency}</a>: {format_balance(balance)} {currency}\n\n"
    text += f"≈ {format_balance(total_btc)} BTC"
    return text

def get_main_keyboard(user_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Кошелёк", callback_data="wallet", icon_custom_emoji_id="5310191758255099001"),
         InlineKeyboardButton(text="Обмен", callback_data="exchange", icon_custom_emoji_id="5361993818373655559")],
        [InlineKeyboardButton(text="P2P", callback_data="p2p", icon_custom_emoji_id="5312419154064607942"),
         InlineKeyboardButton(text="Биржа", callback_data="market", icon_custom_emoji_id="5312212278374861302")],
        [InlineKeyboardButton(text="Чеки", callback_data="checks", icon_custom_emoji_id="5311998535032409760"),
         InlineKeyboardButton(text="Счета", callback_data="invoices", icon_custom_emoji_id="5312043357311111246")],
        [InlineKeyboardButton(text="Crypto Pay", callback_data="cryptopay", icon_custom_emoji_id="5361543877599724417"),
         InlineKeyboardButton(text="Розыгрыши", callback_data="giveaways", icon_custom_emoji_id="5361986358015463601")],
        [InlineKeyboardButton(text="Подписки", callback_data="subscriptions", icon_custom_emoji_id="5312161417372142817"),
         InlineKeyboardButton(text="Настройки", callback_data="settings", icon_custom_emoji_id="5309974037772928528")]
    ])

wallet_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="Пополнить", callback_data="deposit"), 
     InlineKeyboardButton(text="Вывести", callback_data="withdraw")],
    [InlineKeyboardButton(text="‹ Назад", callback_data="back_to_main")]
])

# ==========================================
# ОБЩИЕ ХЕНДЛЕРЫ
# ==========================================
@dp.message(Command("rates"))
async def show_rates(message: types.Message):
    """Показывает текущие курсы криптовалют к USD и когда они обновлялись
    (для проверки, что бот действительно берет живые курсы, а не резервные)."""
    if rates_last_updated:
        updated_str = (rates_last_updated + timedelta(hours=PAID_TIME_TZ_OFFSET_HOURS)).strftime("%d.%m.%Y %H:%M")
        status_line = f"Обновлено: {updated_str} (GMT+{PAID_TIME_TZ_OFFSET_HOURS})"
    else:
        status_line = "⚠️ Ещё не получены с CoinGecko — показаны резервные значения"
    lines = [f"1 {c} ≈ ${format_usd(USD_RATES[c])}" for c in CURRENCY_ORDER]
    await message.answer(f"<b>Курсы валют</b>\n{status_line}\n\n" + "\n".join(lines), parse_mode='HTML')

@dp.message(Command("start"))
async def send_welcome(message: types.Message):
    db.add_user(message.from_user.id)
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("IV"):
        invoice_id = args[1]
        await handle_invoice_payment_start(message, invoice_id)
        return

    
    if message.from_user.id in ADMIN_IDS:
        usdt_balance = db.get_balance(message.from_user.id, "USDT")
        if usdt_balance == 0:
            db.update_balance(message.from_user.id, "USDT", 100)
            
    text = (
        "<tg-emoji emoji-id='5361914370068613491'>👛</tg-emoji> "
        "<a href='https://t.me/Crypto_Bot_RUSSIA/6'>Мультивалютный криптокошелек</a>\n\n"
        "Покупайте, продавайте, храните,\n"
        "отправляйте и платите криптовалютой,\n"
        "когда хотите.\n\n"
        "Подписывайтесь на <a href='https://t.me/Crypto_Bot_RUSSIA'>наш канал</a> и вступайте в\n"
        "<a href='https://t.me/Crypto_Bot_Russian_Chat'>наш чат</a>.  "
    )
    await message.answer(text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=get_main_keyboard(message.from_user.id))

@dp.callback_query(lambda c: c.data == "wallet")
async def open_wallet(callback: types.CallbackQuery):
    try:
        await callback.message.edit_text(get_wallet_text(callback.from_user.id), parse_mode='HTML', disable_web_page_preview=True, reply_markup=wallet_keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.callback_query(lambda c: c.data == "back_to_main")
async def back_to_main(callback: types.CallbackQuery):
    text = (
        "<tg-emoji emoji-id='5361914370068613491'>👛</tg-emoji>   "
        "<a href='https://t.me/Crypto_Bot_RUSSIA/6'>Мультивалютный криптокошелек</a>\n\n"
        "Покупайте, продавайте, храните,\n"
        "отправляйте и платите криптовалютой,\n"
        "когда хотите.\n\n"
        "Подписывайтесь на   <a href='https://t.me/Crypto_Bot_RUSSIA'>наш канал</a> и вступайте в\n"
        "<a href='https://t.me/Crypto_Bot_Russian_Chat'>наш чат</a>.    "
    )
    try:
        await callback.message.edit_text(text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=get_main_keyboard(callback.from_user.id))
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

# ==========================================
# CRYPTO PAY - ГЛАВНОЕ МЕНЮ
# ==========================================
@dp.callback_query(lambda c: c.data == "cryptopay")
async def open_cryptopay_menu(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    text = (
        "<tg-emoji emoji-id=\"5361543877599724417\">🏝</tg-emoji> Здесь вы можете интегрировать платёжную систему Crypto Pay в свои проекты.\n\n"
        "<tg-emoji emoji-id=\"5361781191722699867\">🛒</tg-emoji> Принимайте и отправляйте оплату в криптовалюте с помощью нашего API."
    )
    
    kb_rows = []
    kb_rows.append([InlineKeyboardButton(text="Создать приложение", callback_data="cp_create_app")])
    
    # Проверяем наличие приложений у пользователя
    # Предполагаем, что в Database есть метод get_user_apps(user_id) возвращающий список приложений
    user_apps = []
    if hasattr(db, 'get_user_apps'):
        user_apps = db.get_user_apps(user_id)
    
    if user_apps:
        kb_rows.append([InlineKeyboardButton(text="Мои приложения", callback_data="cp_my_apps")])
        
    kb_rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="back_to_main")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

# ==========================================
# CRYPTO PAY - МОИ ПРИЛОЖЕНИЯ
# ==========================================
@dp.callback_query(lambda c: c.data == "cp_my_apps")
async def cp_my_apps(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    text = "Здесь вы можете управлять своими созданными приложениями."
    
    kb_rows = []
    user_apps = []
    if hasattr(db, 'get_user_apps'):
        user_apps = db.get_user_apps(user_id)
        
    if user_apps:
        for app in user_apps:
            app_name = app.get('name', 'Unknown App')
            app_id = app.get('app_id', '')
            kb_rows.append([InlineKeyboardButton(text=f"{app_name}", callback_data=f"cp_open_app_{app_id}")])
    
    kb_rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="cryptopay")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

# ==========================================
# CRYPTO PAY - СОЗДАНИЕ ПРИЛОЖЕНИЯ
# ==========================================
@dp.callback_query(lambda c: c.data == "cp_create_app")
async def cp_create_app(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    # Генерация данных. app_id/token случайны, поэтому в редком случае коллизии
    # с уже существующей записью пробуем ещё раз (несколько попыток) вместо падения.
    app_id = None
    if hasattr(db, 'create_app'):
        for _ in range(5):
            app_id = generate_app_id()
            app_name = generate_random_app_name()
            api_token = generate_api_token()
            try:
                db.create_app(user_id=user_id, app_id=app_id, name=app_name, token=api_token)
                break
            except sqlite3.IntegrityError as e:
                print(f"[WARN] create_app: коллизия app_id/token, повторная попытка: {e}")
                app_id = None
        if app_id is None:
            await callback.answer("Не удалось создать приложение, попробуйте ещё раз.", show_alert=True)
            return
    else:
        app_id = generate_app_id()
    
    # Сохраняем текущее открытое приложение в стейт для удобства навигации
    user_states[user_id] = {'current_app_id': app_id, 'step': 'app_dashboard'}
    
    await show_app_dashboard(callback, app_id)
    await callback.answer()

async def show_app_dashboard(callback_or_msg, app_id):
    """Отображает дашборд приложения (баланс, кнопки управления)"""
    # Получаем данные приложения
    app = None
    if hasattr(db, 'get_app_by_id'):
        app = db.get_app_by_id(app_id)
    
    if not app:
        text = "Приложение не найдено."
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="‹ Назад", callback_data="cp_my_apps")]])
        if isinstance(callback_or_msg, types.CallbackQuery):
            await callback_or_msg.message.edit_text(text, reply_markup=kb)
            await callback_or_msg.answer()
        else:
            await callback_or_msg.answer(text, reply_markup=kb)
        return

    app_name = app.get('name', 'App')
    balance = app.get('balance', 0)
    
    # Формирование текста как на скриншоте 2
    text = f"Баланс приложения равен нулю.\nСоздайте и оплатите счёт."
    # Если баланс > 0, можно показать другую надпись, но по ТЗ просили именно этот текст для кнопки "Вывести"
    # Однако логичнее показывать реальный баланс. Оставим текст из ТЗ для кнопки вывода, 
    # а здесь покажем статус.
    # По ТЗ: "кнопка вывести в кошелек всегда показывает то что на другом фото"
    # Значит сам экран приложения может быть другим, но кнопка ВЫВЕСТИ ведет на этот алерт/текст.
    # Но в ТЗ сказано: "и снизу кнопки Вывести в кошелек ниже короче кнопки как на фото все"
    # Интерпретация: Экран приложения содержит кнопки как на фото 1. 
    
    # Давайте сделаем экран приложения информативным, а кнопку "Вывести" отдельной.
    dashboard_text = (
        f"<b>{app_display_name(app_name)}</b>\n\n"
        f"ID: <code>{app_id}</code>\n"
        f"Баланс: ${format_balance(balance)}\n"
        f"Комиссия: 1.5%"
    )
    
    # Клавиатура как на Фото 1
    kb_rows = [
        [InlineKeyboardButton(text="Вывести в кошелёк", callback_data=f"cp_withdraw_{app_id}")],
        [InlineKeyboardButton(text="API-токен", callback_data=f"cp_api_token_{app_id}"),
         InlineKeyboardButton(text=f"Вебхуки: {'Вкл.' if app.get('webhook_url') else 'Выкл.'}", callback_data=f"cp_webhooks_{app_id}")],
        [InlineKeyboardButton(text="Безопасность", callback_data=f"cp_security_{app_id}"),
         InlineKeyboardButton(text="Статистика", callback_data=f"cp_stats_view_{app_id}")],
        [InlineKeyboardButton(text="Изменить приложение", callback_data=f"cp_edit_app_{app_id}")],
        [InlineKeyboardButton(text="Удалить приложение", callback_data=f"cp_delete_confirm_{app_id}")],
        [InlineKeyboardButton(text="‹ Назад", callback_data="cp_my_apps")]
    ]
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    
    if isinstance(callback_or_msg, types.CallbackQuery):
        try:
            await callback_or_msg.message.edit_text(dashboard_text, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e): raise e
        await callback_or_msg.answer()
    else:
        await callback_or_msg.answer(dashboard_text, parse_mode='HTML', reply_markup=keyboard)

# ==========================================
# CRYPTO PAY - ОТКРЫТИЕ КОНКРЕТНОГО ПРИЛОЖЕНИЯ
# ==========================================
@dp.callback_query(lambda c: c.data.startswith("cp_open_app_"))
async def cp_open_app(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_open_app_", "")
    user_id = callback.from_user.id
    
    # Проверка прав доступа
    app = db.get_app_by_id(app_id) if hasattr(db, 'get_app_by_id') else None
    if not app or app.get('creator_id') != user_id:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
        
    user_states[user_id] = {'current_app_id': app_id, 'step': 'app_dashboard'}
    await show_app_dashboard(callback, app_id)

# ==========================================
# CRYPTO PAY - ВЫВОД СРЕДСТВ
# ==========================================
@dp.callback_query(lambda c: c.data.startswith("cp_withdraw_"))
async def cp_withdraw(callback: types.CallbackQuery):
    # Показывает сообщение как на Фото 2
    app_id = callback.data.replace("cp_withdraw_", "")
    text = "Баланс приложения равен нулю.\nСоздайте и оплатите счёт."
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="OK", callback_data=f"cp_open_app_{app_id}")]
    ])
    
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

# ==========================================
# CRYPTO PAY - API ТОКЕН
# ==========================================
@dp.callback_query(lambda c: c.data.startswith("cp_api_token_"))
async def cp_api_token_handler(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_api_token_", "")
    await cp_api_token(callback, app_id)

async def cp_api_token(callback: types.CallbackQuery, app_id: str):
    app = db.get_app_by_id(app_id) if hasattr(db, 'get_app_by_id') else None
    
    if not app:
        await callback.answer("Ошибка", show_alert=True)
        return
        
    app_name = app.get('name', 'App')
    token = app.get('token', 'ERROR_NO_TOKEN')
    
    text = (
        f"Токен для приложения {html.escape(app_name)}:\n\n"
        f"<code>{token}</code>\n\n"
        f"⚠️ Этот токен может использоваться для управления приложением. Храните его в надёжном месте."
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сбросить токен", callback_data=f"cp_reset_token_{app_id}")],
        [InlineKeyboardButton(text="‹ Назад", callback_data=f"cp_open_app_{app_id}")]
    ])
    
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("cp_reset_token_"))
async def cp_reset_token(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_reset_token_", "")
    new_token = generate_api_token()
    
    if hasattr(db, 'update_app_token'):
        db.update_app_token(app_id, new_token)
        
    await cp_api_token(callback, app_id)
    await callback.answer("Токен обновлен!")

# ==========================================
# CRYPTO PAY - ВЕБХУКИ
# ==========================================
@dp.callback_query(lambda c: c.data.startswith("cp_webhooks_"))
async def cp_webhooks_handler(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_webhooks_", "")
    await cp_webhooks(callback, app_id)

async def cp_webhooks(callback: types.CallbackQuery, app_id: str):
    app = db.get_app_by_id(app_id) if hasattr(db, 'get_app_by_id') else None
    
    if not app: return
    
    webhook_url = app.get('webhook_url')
    is_enabled = bool(webhook_url)
    
    status_icon = "" if is_enabled else "💤"
    status_text = f"Webhooks URL: {webhook_url}" if is_enabled else "Вебхуки отключены."
    
    text = (
        f"📡 Здесь вы можете настроить вебхуки для получения уведомлений (например, об оплате счетов) на свой сервер.\n\n"
        f"{status_icon} {status_text}"
    )
    
    btn_action_text = " Отключить вебхуки" if is_enabled else "🌕 Включить вебхуки"
    btn_action_data = f"cp_disable_webhook_{app_id}" if is_enabled else f"cp_enable_webhook_{app_id}"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn_action_text, callback_data=btn_action_data)],
        [InlineKeyboardButton(text="‹ Назад", callback_data=f"cp_open_app_{app_id}")]
    ])
    
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("cp_enable_webhook_"))
async def cp_enable_webhook(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_enable_webhook_", "")
    user_id = callback.from_user.id
    
    user_states[user_id] = {'step': 'cp_enter_webhook', 'app_id': app_id}
    
    text = "Пришлите URL, начинающийся с https://. Ваш сервер должен иметь домен, возможность принимать HTTPS-трафик и POST-запросы."
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="‹ Назад", callback_data=f"cp_webhooks_{app_id}")]])
    
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.message(lambda m: m.text and m.from_user.id in user_states and user_states[m.from_user.id].get('step') == 'cp_enter_webhook')
async def process_webhook_url(message: types.Message):
    user_id = message.from_user.id
    state = user_states[user_id]
    url = message.text.strip()
    app_id = state['app_id']
    
    if not url.startswith("https://"):
        await message.answer("❌ URL должен начинаться с https://")
        return
        
    if hasattr(db, 'update_app_webhook'):
        db.update_app_webhook(app_id, url)
        
    del user_states[user_id]
    # Возвращаемся на экран вебхуков, который теперь покажет "Включено"
    # Создаем фейковый коллбэк или просто отправляем новое сообщение
    # Лучше отправить новое сообщение с правильным текстом
    app = db.get_app_by_id(app_id)
    text = (
        f"📡 Здесь вы можете настроить вебхуки для получения уведомлений (например, об оплате счетов) на свой сервер.\n\n"
        f" Webhooks URL: {url}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💤 Отключить вебхуки", callback_data=f"cp_disable_webhook_{app_id}")],
        [InlineKeyboardButton(text="‹ Назад", callback_data=f"cp_open_app_{app_id}")]
    ])
    await message.answer(text, reply_markup=kb)

@dp.callback_query(lambda c: c.data.startswith("cp_disable_webhook_"))
async def cp_disable_webhook(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_disable_webhook_", "")
    if hasattr(db, 'update_app_webhook'):
        db.update_app_webhook(app_id, None)
    await cp_webhooks(callback, app_id)

# ==========================================
# CRYPTO PAY - БЕЗОПАСНОСТЬ
# ==========================================
@dp.callback_query(lambda c: c.data.startswith("cp_security_"))
async def cp_security_handler(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_security_", "")
    await cp_security(callback, app_id)

async def cp_security(callback: types.CallbackQuery, app_id: str):
    app = db.get_app_by_id(app_id) if hasattr(db, 'get_app_by_id') else None
    
    if not app: return
    
    sec = app.get('security', {})
    createcheck = "вкл." if sec.get('createcheck') else "выкл."
    transfer = "вкл." if sec.get('transfer') else "выкл."
    whitelist = "вкл." if sec.get('whitelist_ip') else "выкл."
    
    text = "Здесь вы можете управлять настройками безопасности приложения."
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Метод createcheck: {createcheck}", callback_data=f"cp_sec_toggle_createcheck_{app_id}")],
        [InlineKeyboardButton(text=f"Метод transfer: {transfer}", callback_data=f"cp_sec_toggle_transfer_{app_id}")],
        [InlineKeyboardButton(text=f"Белый список IP: {whitelist}", callback_data=f"cp_sec_toggle_whitelist_{app_id}")],
        [InlineKeyboardButton(text="‹ Назад", callback_data=f"cp_open_app_{app_id}")]
    ])
    
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

async def toggle_security_setting(callback, app_id, setting_key):
    app = db.get_app_by_id(app_id)
    if not app: return
    
    sec = app.get('security', {})
    current = sec.get(setting_key, False)
    new_val = not current
    
    if hasattr(db, 'update_app_security'):
        db.update_app_security(app_id, setting_key, new_val)
        
    await cp_security(callback, app_id)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("cp_sec_toggle_createcheck_"))
async def sec_toggle_createcheck(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_sec_toggle_createcheck_", "")
    await toggle_security_setting(callback, app_id, 'createcheck')

@dp.callback_query(lambda c: c.data.startswith("cp_sec_toggle_transfer_"))
async def sec_toggle_transfer(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_sec_toggle_transfer_", "")
    await toggle_security_setting(callback, app_id, 'transfer')

@dp.callback_query(lambda c: c.data.startswith("cp_sec_toggle_whitelist_"))
async def sec_toggle_whitelist(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_sec_toggle_whitelist_", "")
    await toggle_security_setting(callback, app_id, 'whitelist_ip')

# ==========================================
# CRYPTO PAY - СТАТИСТИКА
# ==========================================
@dp.callback_query(lambda c: c.data.startswith("cp_stats_view_"))
async def cp_stats_view(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_stats_view_", "")
    await cp_stats(callback, app_id)

async def cp_stats(callback: types.CallbackQuery, app_id: str):
    user_id = callback.from_user.id
    
    # Устанавливаем период по умолчанию "all" если нет в стейте
    if user_id not in user_states or user_states[user_id].get('step') != 'cp_stats':
        user_states[user_id] = {'step': 'cp_stats', 'app_id': app_id, 'period': 'all'}
    
    state = user_states[user_id]
    period = state.get('period', 'all')
    
    app = db.get_app_by_id(app_id)
    app_name = app.get('name', 'App') if app else 'App'
    
    # Получаем статистику (заглушка, т.к. реальной логики подсчета в предоставленном коде нет)
    # В реальном проекте здесь был бы запрос к таблице платежей
    stats = {
        'turnover': 0,
        'invoices_created': 0,
        'payments_count': 0,
        'users_count': 0,
        'conversion': 0
    }
    
    period_labels = {
        'today': 'За сегодня',
        'yesterday': 'За вчера',
        'week': 'За неделю',
        'month': 'За месяц',
        'all': 'За все время'
    }
    
    active_period_label = period_labels.get(period, 'За все время')
    
    text = (
        f"<b>Статистика приложения {html.escape(app_name)} за {active_period_label}:</b>\n\n"
        f"<b>Оборот:</b> ${stats['turnover']}\n"
        f"<b>Количество созданных счетов:</b> {stats['invoices_created']}\n"
        f"<b>Количество оплат:</b> {stats['payments_count']}\n"
        f"<b>Количество пользователей:</b> {stats['users_count']}\n\n"
        f"<b>Конверсия:</b> {stats['conversion']}%"
    )
    
    # Формирование кнопок периода
    # Ряд 1: За сегодня | За вчера
    # Ряд 2: За неделю | За месяц
    # Ряд 3: За все время (центр)
    # Активный период помечается точками · ... ·
    
    def fmt_btn(label, key):
        is_active = (key == period)
        prefix = "· " if is_active else ""
        suffix = " ·" if is_active else ""
        return f"{prefix}{label}{suffix}"
        
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=fmt_btn("За сегодня", "today"), callback_data=f"cp_stats_set_today_{app_id}"),
            InlineKeyboardButton(text=fmt_btn("За вчера", "yesterday"), callback_data=f"cp_stats_set_yesterday_{app_id}")
        ],
        [
            InlineKeyboardButton(text=fmt_btn("За неделю", "week"), callback_data=f"cp_stats_set_week_{app_id}"),
            InlineKeyboardButton(text=fmt_btn("За месяц", "month"), callback_data=f"cp_stats_set_month_{app_id}")
        ],
        [InlineKeyboardButton(text=fmt_btn("За все время", "all"), callback_data=f"cp_stats_set_all_{app_id}")],
        [InlineKeyboardButton(text="‹ Назад", callback_data=f"cp_open_app_{app_id}")]
    ])
    
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

# Хендлеры смены периода статистики
for p_key in ['today', 'yesterday', 'week', 'month', 'all']:
    @dp.callback_query(lambda c, k=p_key: c.data.startswith(f"cp_stats_set_{k}_"))
    async def set_stat_period(callback: types.CallbackQuery, k=None):
        parts = callback.data.split("_")
        # cp_stats_set_PERIOD_APPID
        # Индекс периода зависит от длины, но надежнее парсить
        # Формат: cp_stats_set_{period}_{app_id}
        # Но app_id может содержать подчеркивания? Нет, он #A123456. 
        # Лучше использовать фиксированные индексы или replace
        period = parts[3] 
        app_id = "_".join(parts[4:]) # На случай если в ID есть _, хотя unlikely
        
        user_id = callback.from_user.id
        if user_id in user_states:
            user_states[user_id]['period'] = period
        else:
            user_states[user_id] = {'step': 'cp_stats', 'app_id': app_id, 'period': period}
            
        await cp_stats(callback, app_id)

# ==========================================
# CRYPTO PAY - ИЗМЕНИТЬ ПРИЛОЖЕНИЕ
# ==========================================
@dp.callback_query(lambda c: c.data.startswith("cp_edit_app_"))
async def cp_edit_app_handler(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_edit_app_", "")
    await cp_edit_app(callback, app_id)

async def cp_edit_app(callback: types.CallbackQuery, app_id: str):
    app = db.get_app_by_id(app_id) if hasattr(db, 'get_app_by_id') else None
    
    if not app: return
    
    app_name = app.get('name', 'App')
    description = app.get('description', 'нет описания')
    
    text = (
        "Здесь вы можете изменить профиль своего приложения.\n\n"
        f"Имя: {html.escape(app_name)}\n"
        f"Описание: {html.escape(description)}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить имя", callback_data=f"cp_edit_name_{app_id}"),
         InlineKeyboardButton(text="Изменить описание", callback_data=f"cp_edit_desc_{app_id}")],
        [InlineKeyboardButton(text="‹ Назад", callback_data=f"cp_open_app_{app_id}")]
    ])
    
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

# --- Изменение имени ---
@dp.callback_query(lambda c: c.data.startswith("cp_edit_name_"))
async def cp_edit_name_prompt(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_edit_name_", "")
    user_id = callback.from_user.id
    app = db.get_app_by_id(app_id)
    if not app:
        await callback.answer("Приложение не найдено.", show_alert=True)
        return
    
    user_states[user_id] = {'step': 'cp_enter_new_name', 'app_id': app_id}
    
    current_name = app.get('name', 'App')
    text = (
        f"Пришлите новое имя для приложения, которое будет видно в счетах и уведомлениях о переводах (до 30 символов).\n\n"
        f"Имя приложения: {html.escape(current_name)}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить на случайное", callback_data=f"cp_rand_name_{app_id}")],
        [InlineKeyboardButton(text="‹ Назад", callback_data=f"cp_edit_app_{app_id}")]
    ])
    
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("cp_rand_name_"))
async def cp_rand_name(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_rand_name_", "")
    new_name = generate_random_app_name()
    
    if hasattr(db, 'update_app_name'):
        db.update_app_name(app_id, new_name)
        
    # Возвращаемся на экран редактирования, чтобы показать обновленное имя
    await cp_edit_app(callback, app_id)
    await callback.answer("Имя изменено!")

@dp.message(lambda m: m.text and m.from_user.id in user_states and user_states[m.from_user.id].get('step') == 'cp_enter_new_name')
async def process_new_name(message: types.Message):
    user_id = message.from_user.id
    state = user_states[user_id]
    app_id = state['app_id']
    new_name = message.text[:30]
    
    if hasattr(db, 'update_app_name'):
        db.update_app_name(app_id, new_name)
        
    del user_states[user_id]
    
    # Показываем экран редактирования с новым именем
    app = db.get_app_by_id(app_id)
    desc = app.get('description', 'нет описания') if app else 'нет описания'
    text = (
        "Здесь вы можете изменить профиль своего приложения.\n\n"
        f"Имя: {html.escape(new_name)}\n"
        f"Описание: {html.escape(desc)}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить имя", callback_data=f"cp_edit_name_{app_id}"),
         InlineKeyboardButton(text="Изменить описание", callback_data=f"cp_edit_desc_{app_id}")],
        [InlineKeyboardButton(text="‹ Назад", callback_data=f"cp_open_app_{app_id}")]
    ])
    await message.answer(text, parse_mode='HTML', reply_markup=kb)

# --- Изменение описания ---
@dp.callback_query(lambda c: c.data.startswith("cp_edit_desc_"))
async def cp_edit_desc_prompt(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_edit_desc_", "")
    user_id = callback.from_user.id
    app = db.get_app_by_id(app_id)
    if not app:
        await callback.answer("Приложение не найдено.", show_alert=True)
        return
    
    user_states[user_id] = {'step': 'cp_enter_new_desc', 'app_id': app_id}
    
    current_desc = app.get('description', 'нет описания')
    text = (
        f"Пришлите новое описание для приложения.\n\n"
        f"Описание приложения: {current_desc}"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="‹ Назад", callback_data=f"cp_edit_app_{app_id}")]
    ])
    
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.message(lambda m: m.text and m.from_user.id in user_states and user_states[m.from_user.id].get('step') == 'cp_enter_new_desc')
async def process_new_desc(message: types.Message):
    user_id = message.from_user.id
    state = user_states[user_id]
    app_id = state['app_id']
    new_desc = message.text
    
    if hasattr(db, 'update_app_description'):
        db.update_app_description(app_id, new_desc)
        
    del user_states[user_id]
    
    # Показываем экран редактирования
    app = db.get_app_by_id(app_id)
    name = app.get('name', 'App') if app else 'App'
    text = (
        "Здесь вы можете изменить профиль своего приложения.\n\n"
        f"Имя: {html.escape(name)}\n"
        f"Описание: {html.escape(new_desc)}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить имя", callback_data=f"cp_edit_name_{app_id}"),
         InlineKeyboardButton(text="Изменить описание", callback_data=f"cp_edit_desc_{app_id}")],
        [InlineKeyboardButton(text="‹ Назад", callback_data=f"cp_open_app_{app_id}")]
    ])
    await message.answer(text, parse_mode='HTML', reply_markup=kb)

# ==========================================
# CRYPTO PAY - УДАЛИТЬ ПРИЛОЖЕНИЕ
# ==========================================
@dp.callback_query(lambda c: c.data.startswith("cp_delete_confirm_"))
async def cp_delete_confirm(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_delete_confirm_", "")
    app = db.get_app_by_id(app_id) if hasattr(db, 'get_app_by_id') else None
    
    if not app: return
    
    app_name = app.get('name', 'App')
    text = f"<b>❌ Вы уверены, что хотите удалить приложение {html.escape(app_name)}?</b>"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да", callback_data=f"cp_delete_yes_{app_id}"),
         InlineKeyboardButton(text="Нет", callback_data=f"cp_open_app_{app_id}")]
    ])
    
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("cp_delete_yes_"))
async def cp_delete_yes(callback: types.CallbackQuery):
    app_id = callback.data.replace("cp_delete_yes_", "")
    
    if hasattr(db, 'delete_app'):
        db.delete_app(app_id)
        
    # Удаляем из стейта если было активно
    user_id = callback.from_user.id
    if user_id in user_states and user_states[user_id].get('current_app_id') == app_id:
        del user_states[user_id]
        
    # Возвращаемся в главное меню CryptoPay
    await open_cryptopay_menu(callback)
    await callback.answer("Приложение удалено.")


# ==========================================
# ЗАГЛУШКИ ДЛЯ ОСТАЛЬНЫХ КНОПОК
# ==========================================
@dp.callback_query(lambda c: c.data in [
    "exchange", "p2p", "market", "checks",
    "cryptopay", "giveaways", "subscriptions", "settings",
    "deposit", "withdraw"
])
async def placeholder(callback: types.CallbackQuery):
    # cryptopay уже обработан выше, но оставим здесь как fallback если лямбда перехватит раньше
    # Лучше убрать cryptopay из этого списка, так как мы добавили отдельный хендлер
    if callback.data == "cryptopay":
        await open_cryptopay_menu(callback)
        return
    await callback.answer("Раздел пока в разработке", show_alert=True)

# ==========================================
# СУЩЕСТВУЮЩИЙ КОД ДЛЯ СЧЕТОВ (INVOICES)
# ==========================================
@dp.callback_query(lambda c: c.data == "invoices")
async def open_invoices(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    text = (
        "Здесь вы можете создать счет\n"
        "для получения оплаты или сбора\n"
        "средств в криптовалюте. Смотрите   "
        "<a href='https://t.me/Crypto_Bot_RUSSIA/8'>инструкцию ›</a> "
    )
    user_invoices = db.get_active_invoices_for_list(user_id)
    keyboard_rows = []
    keyboard_rows.append([InlineKeyboardButton(text="Создать счет", callback_data="create_invoice")])
    if user_invoices:
        keyboard_rows.append([InlineKeyboardButton(text=f"Активные счета ({len(user_invoices)})", callback_data="view_invoices")])
    keyboard_rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="back_to_main")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    try:
        await callback.message.edit_text(text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.callback_query(lambda c: c.data == "create_invoice")
async def choose_invoice_type(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_states[user_id] = {'step': 'choose_type'}
    text = "Выберите тип счета. "
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Одноразовый", callback_data="invoice_single"),
         InlineKeyboardButton(text="Многоразовый", callback_data="invoice_multi")],
        [InlineKeyboardButton(text="‹ Назад к счетам", callback_data="invoices")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.callback_query(lambda c: c.data in ["invoice_single", "invoice_multi"])
async def select_invoice_type(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    invoice_type = "single" if callback.data == "invoice_single" else "multi"
    user_states[user_id] = {
        'step': 'select_currencies',
        'invoice_type': invoice_type,
        'selected_currencies': set(),
        'show_dots': False
    }
    await show_currency_selection(callback)
    await callback.answer()

def build_currency_selection_view(state):
    """Строит текст и клавиатуру экрана выбора валют. Используется и для edit_text (callback), и для нового сообщения (deep-link)."""
    text = ("Выберите одну или больше криптовалют,\n" "которыми может быть оплачен счет.")
    keyboard_rows = []
    for i in range(0, len(CURRENCY_ORDER), 3):
        row = []
        for j in range(i, min(i+3, len(CURRENCY_ORDER))):
            currency = CURRENCY_ORDER[j]
            selected = currency in state['selected_currencies']
            dot = " ·" if (selected or (not state['selected_currencies'] and state.get('show_dots', False))) else ""
            btn_text = f"{currency}{dot}"
            row.append(InlineKeyboardButton(text=btn_text, callback_data=f"toggle_currency_{currency}"))
        keyboard_rows.append(row)
    nav_buttons = [
        InlineKeyboardButton(text="Далее›", callback_data="invoice_next_after_currency"),
        InlineKeyboardButton(text="‹ Изменить тип счета", callback_data="create_invoice")
    ]
    keyboard_rows.append(nav_buttons)
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    return text, keyboard

async def show_currency_selection(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    state = user_states[user_id]
    text, keyboard = build_currency_selection_view(state)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e

@dp.callback_query(lambda c: c.data.startswith("toggle_currency_"))
async def toggle_currency(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    currency = callback.data.replace("toggle_currency_", "")
    state = user_states[user_id]
    if currency in state['selected_currencies']:
        state['selected_currencies'].remove(currency)
    else:
        state['selected_currencies'].add(currency)
    state['show_dots'] = True
    await show_currency_selection(callback)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "invoice_next_after_currency")
async def after_currency_selection(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    state = user_states[user_id]
    if not state['selected_currencies']:
        state['selected_currencies'] = set(CURRENCY_ORDER)
    state['step'] = 'enter_amount'
    currencies_str = ", ".join(sorted(state['selected_currencies']))
    text = f"Пришлите сумму счета в USD (мин. 0.01) с оплатой в {currencies_str}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="‹ Изменить монету", callback_data="select_currencies_again")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.callback_query(lambda c: c.data == "select_currencies_again")
async def back_to_currency_selection(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_states[user_id]['step'] = 'select_currencies'
    user_states[user_id]['show_dots'] = True
    await show_currency_selection(callback)
    await callback.answer()

@dp.message(lambda m: m.text and m.from_user.id in user_states and user_states[m.from_user.id].get('step') == 'enter_amount')
async def process_amount(message: types.Message):
    user_id = message.from_user.id
    state = user_states[user_id]
    try:
        amount = float(message.text)
        if amount < 0.01:
            await message.answer("❌ Минимальная сумма счета составляет 0.01 USD. Попробуйте еще раз.")
            return
        state['amount_usd'] = amount
        state['step'] = 'invoice_created'
        invoice_id = generate_invoice_id()
        state['invoice_id'] = invoice_id
        currencies_list = sorted(list(state['selected_currencies']))
        db.create_invoice(
            invoice_id=invoice_id,
            creator_id=user_id,
            amount_usd=amount,
            currencies=currencies_list,
            invoice_type=state['invoice_type']
        )
        await show_invoice_details(message, invoice_id)
    except ValueError:
        await message.answer("Пожалуйста, введите корректную сумму (число).")

async def show_invoice_details(message_or_callback, invoice_id):
    invoice = db.get_invoice(invoice_id)
    if not invoice: return
    
    is_owner = False
    if isinstance(message_or_callback, types.CallbackQuery):
        is_owner = (message_or_callback.from_user.id == invoice['creator_id'])
    elif isinstance(message_or_callback, types.Message):
        is_owner = True 
        
    currencies_str = ", ".join(invoice['currencies'])
    bot_username = (await bot.get_me()).username
    amount_line = "Сумма: не указано" if is_open_amount(invoice) else f"Сумма: ${format_balance(invoice['amount_usd'])}"
    text = (
        f"Счет #{invoice_id}\n\n"
        f"{amount_line}\n\n"
        f"Любой может оплатить этот счет в {currencies_str}.\n\n"
        f"Скопируйте ссылку, чтобы поделиться счетом:\n"
        f"https://t.me/{bot_username}?start={invoice_id}"
    )
    keyboard_rows = []
    if is_owner:
        # Кнопка "Поделиться" доступна только владельцу счета — раньше её видел
        # и мог ею воспользоваться любой, кто открыл чужой счет по ссылке.
        keyboard_rows.append([InlineKeyboardButton(text="Поделиться счетом", switch_inline_query=invoice_id)])
        if is_open_amount(invoice):
            min_amt = invoice.get('min_amount_usd', 0.01)
            keyboard_rows.append([InlineKeyboardButton(text=f"Мин. сумма: ${format_balance(min_amt)}", callback_data=f"set_min_amount_{invoice_id}")])
        keyboard_rows.append([InlineKeyboardButton(text="Разрешения", callback_data=f"invoice_permissions_{invoice_id}")])
        keyboard_rows.append([InlineKeyboardButton(text="Удалить счет", callback_data=f"delete_invoice_{invoice_id}")])
    keyboard_rows.append([InlineKeyboardButton(text="‹ Назад к списку счетов", callback_data="invoices")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    
    if isinstance(message_or_callback, types.CallbackQuery):
        try:
            await message_or_callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e): raise e
        await message_or_callback.answer()
    else:
        await message_or_callback.answer(text, reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("invoice_permissions_"))
async def show_invoice_permissions_handler(callback: types.CallbackQuery):
    invoice_id = callback.data.replace("invoice_permissions_", "")
    await show_invoice_permissions(callback, invoice_id)

async def show_invoice_permissions(callback: types.CallbackQuery, invoice_id: str):
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        await callback.answer("Счет не найден.", show_alert=True); return
    if callback.from_user.id != invoice['creator_id']:
        await callback.answer("Это не ваш счет.", show_alert=True); return
        
    comments_status = "Вкл." if invoice['allow_comments'] else "Выкл."
    anonymous_status = "Вкл." if invoice['allow_anonymous'] else "Выкл."
    text = ("Разрешите или запретите оплачивать счет анонимно  " "и добавлять коментарии при оплате.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Коментарии: {comments_status}", callback_data=f"toggle_comments_{invoice_id}")],
        [InlineKeyboardButton(text=f"Анонимные платежы: {anonymous_status}", callback_data=f"toggle_anonymous_{invoice_id}")],
        [InlineKeyboardButton(text="‹ Назад к счету", callback_data=f"view_invoice_{invoice_id}")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("toggle_comments_"))
async def toggle_comments(callback: types.CallbackQuery):
    invoice_id = callback.data.replace("toggle_comments_", "")
    invoice = db.get_invoice(invoice_id)
    if not invoice or callback.from_user.id != invoice['creator_id']:
        await callback.answer("Ошибка доступа.", show_alert=True); return
    new_value = 0 if invoice['allow_comments'] else 1
    db.update_invoice_settings(invoice_id, allow_comments=new_value)
    await show_invoice_permissions(callback, invoice_id)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("toggle_anonymous_"))
async def toggle_anonymous(callback: types.CallbackQuery):
    invoice_id = callback.data.replace("toggle_anonymous_", "")
    invoice = db.get_invoice(invoice_id)
    if not invoice or callback.from_user.id != invoice['creator_id']:
        await callback.answer("Ошибка доступа.", show_alert=True); return
    new_value = 0 if invoice['allow_anonymous'] else 1
    db.update_invoice_settings(invoice_id, allow_anonymous=new_value)
    await show_invoice_permissions(callback, invoice_id)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("delete_invoice_"))
async def confirm_delete_invoice(callback: types.CallbackQuery):
    invoice_id = callback.data.replace("delete_invoice_", "")
    invoice = db.get_invoice(invoice_id)
    if not invoice or callback.from_user.id != invoice['creator_id']:
        await callback.answer("Ошибка доступа.", show_alert=True); return
    text = "❌ Вы уверены, что хотите удалить этот счет?"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да", callback_data=f"confirm_delete_{invoice_id}"),
         InlineKeyboardButton(text="Нет", callback_data=f"view_invoice_{invoice_id}")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("confirm_delete_"))
async def delete_invoice(callback: types.CallbackQuery):
    invoice_id = callback.data.replace("confirm_delete_", "")
    invoice = db.get_invoice(invoice_id)
    if not invoice or callback.from_user.id != invoice['creator_id']:
        await callback.answer("Ошибка доступа.", show_alert=True); return
    db.delete_invoice(invoice_id)
    try: await callback.message.delete()
    except: pass
    await callback.answer("Счет удален.")

@dp.callback_query(lambda c: c.data.startswith("view_invoice_"))
async def view_invoice(callback: types.CallbackQuery):
    invoice_id = callback.data.replace("view_invoice_", "")
    await show_invoice_details(callback, invoice_id)

@dp.callback_query(lambda c: c.data.startswith("set_min_amount_"))
async def set_min_amount(callback: types.CallbackQuery):
    invoice_id = callback.data.replace("set_min_amount_", "")
    invoice = db.get_invoice(invoice_id)
    if not invoice or callback.from_user.id != invoice['creator_id']:
        await callback.answer("Это не ваш счет.", show_alert=True); return
    if not is_open_amount(invoice):
        await callback.answer("У этого счета фиксированная сумма.", show_alert=True); return

    user_id = callback.from_user.id
    user_states[user_id] = {'step': 'enter_min_amount', 'invoice_id': invoice_id}
    text = "Пришлите минимальную сумму одного платежа в USD."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="‹ Назад к счету", callback_data=f"view_invoice_{invoice_id}")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.message(lambda m: m.text and m.from_user.id in user_states and user_states[m.from_user.id].get('step') == 'enter_min_amount')
async def process_min_amount(message: types.Message):
    user_id = message.from_user.id
    state = user_states[user_id]
    invoice_id = state['invoice_id']
    try:
        min_amount = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Пожалуйста, введите корректную сумму (число)."); return
    if min_amount < 0.01:
        await message.answer("❌ Минимальная сумма не может быть меньше 0.01 USD. Попробуйте еще раз."); return

    invoice = db.get_invoice(invoice_id)
    if not invoice or invoice['creator_id'] != user_id:
        await message.answer("Счет не найден."); return

    min_amount = round(min_amount, 2)
    db.update_invoice_min_amount(invoice_id, min_amount)
    user_states.pop(user_id, None)
    await show_invoice_details(message, invoice_id)

INVOICES_PER_PAGE = 6

def format_invoice_list_label(inv):
    """Подпись счета в списке: 'Многоразовый' для многоразовых счетов, сумма
    (например '$1') для обычных активных счетов с фиксированной суммой,
    и отдельная пометка для счетов с открытой суммой (плательщик сам её вводит)."""
    if inv['invoice_type'] == 'multi':
        return "Многоразовый"
    if inv['amount_usd'] is None:
        return "Открытая сумма"
    return f"${format_balance(inv['amount_usd'])}"

def build_invoices_list_keyboard(invoices, page):
    """Клавиатура списка счетов с пагинацией: по INVOICES_PER_PAGE штук на страницу.
    Если страниц больше одной — снизу добавляется навигация: в начало / назад /
    номер страницы / вперед / сразу в конец."""
    total_pages = max(1, (len(invoices) + INVOICES_PER_PAGE - 1) // INVOICES_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * INVOICES_PER_PAGE
    page_invoices = invoices[start:start + INVOICES_PER_PAGE]

    keyboard_rows = []
    for inv in page_invoices:
        label = format_invoice_list_label(inv)
        keyboard_rows.append([InlineKeyboardButton(text=label, callback_data=f"view_invoice_{inv['invoice_id']}")])

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="« В начало", callback_data="view_invoices_page_0"))
            nav_row.append(InlineKeyboardButton(text="‹", callback_data=f"view_invoices_page_{page - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="invoices_page_noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="›", callback_data=f"view_invoices_page_{page + 1}"))
            nav_row.append(InlineKeyboardButton(text="В конец »", callback_data=f"view_invoices_page_{total_pages - 1}"))
        keyboard_rows.append(nav_row)

    keyboard_rows.append([InlineKeyboardButton(text="‹ Назад к счетам", callback_data="invoices")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

@dp.callback_query(lambda c: c.data == "view_invoices" or c.data.startswith("view_invoices_page_"))
async def view_all_invoices(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    invoices = db.get_active_invoices_for_list(user_id)
    if not invoices:
        await callback.answer("У вас нет активных счетов.", show_alert=True); return

    page = 0
    if callback.data.startswith("view_invoices_page_"):
        try:
            page = int(callback.data.replace("view_invoices_page_", ""))
        except ValueError:
            page = 0

    keyboard = build_invoices_list_keyboard(invoices, page)
    try:
        await callback.message.edit_text("Ваши активные счета:", reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.callback_query(lambda c: c.data == "invoices_page_noop")
async def invoices_page_noop(callback: types.CallbackQuery):
    """Кнопка с номером страницы ('2/5') — просто отображает текущую позицию,
    сама по себе никуда не ведет."""
    await callback.answer()

@dp.chosen_inline_result()
async def save_shared_invoice_message(chosen_result: types.ChosenInlineResult):
    """Запоминает inline_message_id расшаренного счета, чтобы потом отредактировать
    это сообщение (поставить отметку об оплате) прямо в чате, куда его переслали."""
    result_id = chosen_result.result_id
    if not result_id.startswith("IV") or not chosen_result.inline_message_id:
        return
    invoice = db.get_invoice(result_id)
    if not invoice:
        return
    db.add_invoice_message(result_id, chosen_result.inline_message_id)

@dp.inline_query()
async def inline_query_handler(query: types.InlineQuery):
    query_text = query.query.strip()
    user_id = query.from_user.id
    bot_username = (await bot.get_me()).username

    # --- Сценарий 1: "Поделиться счетом" — расшарить уже существующий счет по его ID ---
    if query_text.startswith("IV"):
        invoice = db.get_invoice(query_text)
        if not invoice or not invoice['is_active']: return
        if invoice['invoice_type'] == 'single' and invoice['is_paid']: return

        title_text = f"Многоразовый счет." if invoice['invoice_type'] == 'multi' else f"Счет на ${format_balance(invoice['amount_usd'])}"

        result = types.InlineQueryResultArticle(
            id=query_text, title="Поделиться счетом", description="Нажмите, чтобы поделиться этим счетом.",
            input_message_content=types.InputTextMessageContent(
                message_text=f"<tg-emoji emoji-id=\"5312043357311111246\">📥</tg-emoji> {title_text}", parse_mode="HTML"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Оплатить", url=f"https://t.me/{bot_username}?start={query_text}")]
            ])
        )
        await query.answer(results=[result], cache_time=0)
        return

    # --- Сценарий 2: ничего не введено — сразу создается многоразовый счет с открытой суммой ---
    # (плательщик сам укажет, сколько отправить, на шаге оплаты). Ведет себя как обычный
    # расшаренный счет: счет уже существует в БД, кнопка сразу "Оплатить".
    if not query_text:
        invoice_id = generate_invoice_id()
        db.create_invoice(
            invoice_id=invoice_id,
            creator_id=user_id,
            amount_usd=None,
            currencies=list(CURRENCY_ORDER),
            invoice_type='multi'
        )
        result = types.InlineQueryResultArticle(
            id=invoice_id,
            title="Создать счёт",
            description="Многоразовый счёт.",
            input_message_content=types.InputTextMessageContent(
                message_text=f"<tg-emoji emoji-id=\"5312043357311111246\">📥</tg-emoji> Многоразовый счёт.",
                parse_mode="HTML"
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Оплатить", url=f"https://t.me/{bot_username}?start={invoice_id}")]
            ])
        )
        await query.answer(results=[result], cache_time=0, is_personal=True)
        return

    # --- Сценарий 3: введена сумма — счет создается сразу, во всех валютах, одноразовый ---
    amount_text = query_text.replace(",", ".")
    try:
        amount = float(amount_text)
    except ValueError:
        await query.answer(results=[], cache_time=0)
        return
    if amount < 0.01:
        await query.answer(results=[], cache_time=0)
        return
    amount = round(amount, 2)

    invoice_id = generate_invoice_id()
    db.create_invoice(
        invoice_id=invoice_id,
        creator_id=user_id,
        amount_usd=amount,
        currencies=list(CURRENCY_ORDER),
        invoice_type='single'
    )

    amount_str = format_balance(amount)
    result = types.InlineQueryResultArticle(
        id=invoice_id,
        title=f"Создать счёт · ${amount_str}",
        description=f"Счёт на ${amount_str}.",
        input_message_content=types.InputTextMessageContent(
            message_text=f"<tg-emoji emoji-id=\"5312043357311111246\">📥</tg-emoji> Счет на ${amount_str}",
            parse_mode="HTML"
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Оплатить", url=f"https://t.me/{bot_username}?start={invoice_id}")]
        ])
    )
    await query.answer(results=[result], cache_time=0, is_personal=True)

def is_open_amount(invoice):
    """Счет с открытой суммой (плательщик сам вводит сумму) — amount_usd не задан."""
    return invoice['amount_usd'] is None

def build_currency_pick_view(invoice_id, invoice):
    if is_open_amount(invoice):
        text = f"Выберите монету для оплаты счета #{invoice_id}."
        make_btn_text = lambda currency: currency
    else:
        text = f"Выберите монету для оплаты счета #{invoice_id} на сумму ${format_balance(invoice['amount_usd'])}."
        def make_btn_text(currency):
            rate = USD_RATES.get(currency, 1)
            amount_in_currency = invoice['amount_usd'] / rate
            return f"{currency} · {format_balance(amount_in_currency)} {currency}"
    keyboard_rows = [
        [InlineKeyboardButton(text=make_btn_text(currency), callback_data=f"pay_invoice_{invoice_id}_{currency}")]
        for currency in invoice['currencies']
    ]
    return text, keyboard_rows

async def handle_invoice_payment_start(message, invoice_id):
    invoice = db.get_invoice(invoice_id)
    if not invoice or not invoice['is_active']:
        await message.answer("Счет не найден или не активен."); return
    if invoice['invoice_type'] == 'single' and invoice['is_paid']:
        await message.answer("Счет уже оплачен."); return
        
    text, keyboard_rows = build_currency_pick_view(invoice_id, invoice)
    keyboard_rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="back_to_main")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    await message.answer(text, reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("pay_invoice_"))
async def select_payment_currency_handler(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    invoice_id = parts[2]; currency = parts[3]
    await select_payment_currency(callback, invoice_id, currency, force_ask=True)

def build_enter_amount_view(invoice_id, invoice, currency, user_id):
    rate = USD_RATES.get(currency, 1)
    min_amount_usd = invoice.get('min_amount_usd', 0.01)
    min_amount = min_amount_usd / rate
    balance = db.get_balance(user_id, currency)
    text = (
        f"Пришлите сумму {currency} для оплаты счёта.\n\n"
        f"Минимум: {format_balance(min_amount)} {currency} (${format_usd(min_amount_usd)})\n"
        f"Ваш баланс: {format_balance(balance)} {currency} (${format_usd(balance * rate)})"
    )
    min_btn_text = f"Мин. · {format_balance(min_amount)} {currency} (${format_usd(min_amount_usd)})"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=min_btn_text, callback_data=f"pay_min_amount_{invoice_id}_{currency}")],
        [InlineKeyboardButton(text="‹ Изменить монету", callback_data=f"back_to_payment_select_{invoice_id}")]
    ])
    return text, keyboard

def build_confirm_payment_view(invoice_id, invoice, currency, amount_usd, is_anonymous, allow_anon, allow_comm):
    rate = USD_RATES.get(currency, 1)
    amount_in_currency = amount_usd / rate
    anon_btn_text = f"Оплатить анонимно: {'Да' if is_anonymous else 'Нет'}"
    if not allow_anon: anon_btn_text = "Анонимность запрещена"

    text = (
        f"<tg-emoji emoji-id=\"5312043357311111246\">📥</tg-emoji> <b>Подтвердите оплату счета #{invoice_id}</b>\n\n"
        f"<b>Отправляете:</b> <b>{format_balance(amount_in_currency)} {currency} (${format_balance(amount_usd)})</b>\n\n"
        f"Вы уверены, что хотите оплатить этот счет?"
    )
    kb_rows = [[InlineKeyboardButton(text=f"💳 Оплатить {format_balance(amount_in_currency)} {currency}", callback_data=f"process_payment_{invoice_id}_{currency}")]]
    if allow_anon: kb_rows.append([InlineKeyboardButton(text=anon_btn_text, callback_data=f"toggle_pay_anonymous_{invoice_id}")])
    if allow_comm: kb_rows.append([InlineKeyboardButton(text="Добавить коментарий", callback_data=f"add_comment_{invoice_id}")])
    if is_open_amount(invoice):
        kb_rows.append([InlineKeyboardButton(text="‹ Изменить сумму", callback_data=f"change_pay_amount_{invoice_id}_{currency}")])
    else:
        kb_rows.append([InlineKeyboardButton(text="‹ Назад к оплате", callback_data=f"back_to_payment_select_{invoice_id}")])
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)

@dp.callback_query(lambda c: c.data.startswith("change_pay_amount_"))
async def change_pay_amount(callback: types.CallbackQuery):
    payload = callback.data.replace("change_pay_amount_", "")
    invoice_id, currency = payload.rsplit("_", 1)
    user_id = callback.from_user.id
    invoice = db.get_invoice(invoice_id)
    if not invoice or not invoice['is_active']:
        await callback.answer("Счет не найден или не активен.", show_alert=True); return
    if invoice['invoice_type'] == 'single' and invoice['is_paid']:
        await callback.answer("Счет уже оплачен.", show_alert=True); return

    prev_state = user_states.get(user_id, {})
    user_states[user_id] = {
        'step': 'enter_pay_amount', 'invoice_id': invoice_id, 'currency': currency,
        'comment': prev_state.get('comment', ''),
        'is_anonymous': prev_state.get('is_anonymous', 0)
    }
    text, keyboard = build_enter_amount_view(invoice_id, invoice, currency, user_id)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

async def select_payment_currency(callback: types.CallbackQuery, invoice_id: str, currency: str, force_ask: bool = False):
    invoice = db.get_invoice(invoice_id)
    if not invoice or not invoice['is_active']:
        await callback.answer("Счет не найден или не активен.", show_alert=True); return
    if invoice['invoice_type'] == 'single' and invoice['is_paid']:
        await callback.answer("Счет уже оплачен.", show_alert=True); return

    user_id = callback.from_user.id
    have_state_for_this = (user_id in user_states and user_states[user_id].get('invoice_id') == invoice_id
                            and user_states[user_id].get('currency') == currency)

    if is_open_amount(invoice) and (force_ask or not (have_state_for_this and user_states[user_id].get('custom_amount_usd') is not None)):
        # Сумма не зафиксирована создателем счета — сначала спрашиваем у плательщика, сколько он хочет отправить.
        user_states[user_id] = {
            'step': 'enter_pay_amount', 'invoice_id': invoice_id, 'currency': currency,
            'comment': (user_states.get(user_id, {}).get('comment', '') if have_state_for_this else ''),
            'is_anonymous': (user_states.get(user_id, {}).get('is_anonymous', 0) if have_state_for_this else 0)
        }
        text, keyboard = build_enter_amount_view(invoice_id, invoice, currency, user_id)
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e): raise e
        await callback.answer()
        return

    if not have_state_for_this or user_states[user_id].get('step') != 'confirm_payment':
        user_states[user_id] = {
            'step': 'confirm_payment', 'invoice_id': invoice_id, 'currency': currency,
            'comment': '', 'is_anonymous': 0
        }
    state = user_states[user_id]
    amount_usd = invoice['amount_usd'] if not is_open_amount(invoice) else state['custom_amount_usd']

    text, keyboard = build_confirm_payment_view(
        invoice_id, invoice, currency, amount_usd,
        state.get('is_anonymous', 0), invoice['allow_anonymous'], invoice['allow_comments']
    )
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.message(lambda m: m.text and m.from_user.id in user_states and user_states[m.from_user.id].get('step') == 'enter_pay_amount')
async def process_pay_amount(message: types.Message):
    user_id = message.from_user.id
    state = user_states[user_id]
    currency = state['currency']
    invoice_id = state['invoice_id']
    invoice = db.get_invoice(invoice_id)
    if not invoice or not invoice['is_active']:
        await message.answer("Счет не найден или не активен."); return
    if invoice['invoice_type'] == 'single' and invoice['is_paid']:
        await message.answer("Счет уже оплачен."); return

    try:
        amount_in_currency = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Пожалуйста, введите корректную сумму (число)."); return

    rate = USD_RATES.get(currency, 1)
    min_amount_usd = invoice.get('min_amount_usd', 0.01)
    min_amount = min_amount_usd / rate
    if amount_in_currency < min_amount:
        await message.answer(f"❌ Минимальная сумма {format_balance(min_amount)} {currency} (${format_usd(min_amount_usd)}). Попробуйте еще раз."); return

    amount_in_currency = round(amount_in_currency, 8)
    amount_usd = round(amount_in_currency * rate, 2)
    state['custom_amount_usd'] = amount_usd
    state['step'] = 'confirm_payment'

    text, keyboard = build_confirm_payment_view(
        invoice_id, invoice, currency, amount_usd,
        state.get('is_anonymous', 0), invoice['allow_anonymous'], invoice['allow_comments']
    )
    await message.answer(text, parse_mode='HTML', reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("pay_min_amount_"))
async def process_pay_min_amount(callback: types.CallbackQuery):
    payload = callback.data.replace("pay_min_amount_", "")
    invoice_id, currency = payload.rsplit("_", 1)
    user_id = callback.from_user.id

    if user_id not in user_states or user_states[user_id].get('invoice_id') != invoice_id or user_states[user_id].get('currency') != currency:
        await callback.answer("Сессия оплаты устарела, выберите монету заново.", show_alert=True); return

    state = user_states[user_id]
    invoice = db.get_invoice(invoice_id)
    if not invoice or not invoice['is_active']:
        await callback.answer("Счет не найден или не активен.", show_alert=True); return
    if invoice['invoice_type'] == 'single' and invoice['is_paid']:
        await callback.answer("Счет уже оплачен.", show_alert=True); return

    rate = USD_RATES.get(currency, 1)
    amount_usd = round(invoice.get('min_amount_usd', 0.01), 2)
    state['custom_amount_usd'] = amount_usd
    state['step'] = 'confirm_payment'

    text, keyboard = build_confirm_payment_view(
        invoice_id, invoice, currency, amount_usd,
        state.get('is_anonymous', 0), invoice['allow_anonymous'], invoice['allow_comments']
    )
    try:
        await callback.message.edit_text(text, parse_mode='HTML', reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("toggle_pay_anonymous_"))
async def toggle_pay_anonymous(callback: types.CallbackQuery):
    invoice_id = callback.data.replace("toggle_pay_anonymous_", "")
    user_id = callback.from_user.id
    invoice = db.get_invoice(invoice_id)
    if not invoice or not invoice['allow_anonymous']:
        await callback.answer("Анонимная оплата запрещена создателем счета.", show_alert=True); return
    if user_id in user_states and user_states[user_id].get('step') == 'confirm_payment':
        current = user_states[user_id].get('is_anonymous', 0)
        user_states[user_id]['is_anonymous'] = 1 - current
        currency = user_states[user_id].get('currency')
        await select_payment_currency(callback, invoice_id, currency)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("add_comment_"))
async def add_comment(callback: types.CallbackQuery):
    invoice_id = callback.data.replace("add_comment_", "")
    user_id = callback.from_user.id
    invoice = db.get_invoice(invoice_id)
    if not invoice or not invoice['allow_comments']:
        await callback.answer("Комментарии запрещены создателем счета.", show_alert=True); return
    if user_id not in user_states or user_states[user_id].get('invoice_id') != invoice_id:
        await callback.answer("Сессия оплаты устарела, начните заново.", show_alert=True); return
    user_states[user_id]['step'] = 'enter_comment'
    user_states[user_id]['invoice_id'] = invoice_id
    text = "Пришлите комментарий к платежу, который будет виден в уведомлении об оплате (до 1024 символов)."
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="‹ Назад к оплате", callback_data=f"back_to_payment_select_{invoice_id}")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

@dp.message(lambda m: m.text and m.from_user.id in user_states and user_states[m.from_user.id].get('step') == 'enter_comment')
async def process_comment(message: types.Message):
    user_id = message.from_user.id; state = user_states[user_id]
    comment = message.text[:1024]
    state['comment'] = comment; state['step'] = 'confirm_payment'
    invoice_id = state['invoice_id']
    await select_payment_currency_by_data(message, invoice_id, state['currency'])

async def select_payment_currency_by_data(message, invoice_id, currency):
    invoice = db.get_invoice(invoice_id)
    if not invoice or not invoice['is_active']:
        await message.answer("Счет не найден или не активен."); return
    if invoice['invoice_type'] == 'single' and invoice['is_paid']:
        await message.answer("Счет уже оплачен."); return

    user_id = message.from_user.id
    state = user_states.get(user_id, {})
    is_anon_state = state.get('is_anonymous', 0) if state.get('step') == 'confirm_payment' else 0
    amount_usd = invoice['amount_usd'] if not is_open_amount(invoice) else state.get('custom_amount_usd')
    if amount_usd is None:
        # На всякий случай, если сумма для открытого счета почему-то потерялась — просим ввести заново.
        user_states[user_id] = {'step': 'enter_pay_amount', 'invoice_id': invoice_id, 'currency': currency,
                                 'comment': state.get('comment', ''), 'is_anonymous': is_anon_state}
        text, keyboard = build_enter_amount_view(invoice_id, invoice, currency, user_id)
        await message.answer(text, reply_markup=keyboard)
        return

    text, keyboard = build_confirm_payment_view(
        invoice_id, invoice, currency, amount_usd,
        is_anon_state, invoice['allow_anonymous'], invoice['allow_comments']
    )
    await message.answer(text, parse_mode='HTML', reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("back_to_payment_select_"))
async def back_to_payment_select(callback: types.CallbackQuery):
    invoice_id = callback.data.replace("back_to_payment_select_", "")
    invoice = db.get_invoice(invoice_id)
    if not invoice or not invoice['is_active']:
        await callback.answer("Счет не найден или не активен.", show_alert=True); return
    if invoice['invoice_type'] == 'single' and invoice['is_paid']:
        await callback.answer("Счет уже оплачен.", show_alert=True); return
        
    text, keyboard_rows = build_currency_pick_view(invoice_id, invoice)
    keyboard_rows.append([InlineKeyboardButton(text="‹ Назад", callback_data="back_to_main")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e): raise e
    await callback.answer()

async def update_paid_invoice_messages(invoice_id, amount_usd):
    """Помечает все расшаренные сообщения этого (одноразового) счета как оплаченные —
    редактирует их прямо в тех чатах, куда счет был переслан через инлайн.

    Кнопка остается такой же ссылкой на счет (url=...), как и до оплаты — просто
    меняется текст на "Оплачено". Раньше кнопка подменялась на callback_data,
    который вел в никуда (просто показывал алерт) — теперь ссылка сохраняется,
    и по ней по-прежнему можно открыть счет в боте."""
    inline_message_ids = db.get_invoice_messages(invoice_id)
    if not inline_message_ids:
        return
    bot_username = (await bot.get_me()).username
    header = f"<tg-emoji emoji-id=\"5312043357311111246\">📥</tg-emoji> Счет на ${format_balance(amount_usd)}."
    paid_line = f"✅ Оплачен {format_paid_datetime()}"
    text = f"{header}\n\n{paid_line}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Оплачено", url=f"https://t.me/{bot_username}?start={invoice_id}")]
    ])
    for inline_message_id in inline_message_ids:
        try:
            await bot.edit_message_text(text, inline_message_id=inline_message_id, parse_mode='HTML', reply_markup=keyboard)
        except TelegramBadRequest:
            pass
        except Exception as e:
            print(f"Не удалось обновить расшаренное сообщение счета {invoice_id}: {e}")

@dp.callback_query(lambda c: c.data.startswith("process_payment_"))
async def process_payment(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    invoice_id = parts[2]; currency = parts[3]
    user_id = callback.from_user.id
    invoice = db.get_invoice(invoice_id)
    if not invoice or not invoice['is_active']:
        await callback.answer("Счет не найден или не активен.", show_alert=True); return
    if invoice['invoice_type'] == 'single' and invoice['is_paid']:
        await callback.answer("Счет уже оплачен.", show_alert=True); return
        
    state = user_states.get(user_id, {})
    amount_usd = invoice['amount_usd'] if not is_open_amount(invoice) else state.get('custom_amount_usd')
    if amount_usd is None:
        await callback.answer("Сначала укажите сумму оплаты.", show_alert=True); return
    rate = USD_RATES.get(currency, 1)
    amount_in_currency = amount_usd / rate
    payer_balance = db.get_balance(user_id, currency)
    
    if payer_balance < amount_in_currency:
        text = "❌ Недостаточно средств."
        if is_open_amount(invoice):
            back_btn = InlineKeyboardButton(text="‹ Изменить сумму", callback_data=f"change_pay_amount_{invoice_id}_{currency}")
        else:
            back_btn = InlineKeyboardButton(text="‹ Назад", callback_data=f"back_to_payment_select_{invoice_id}")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[back_btn]])
        try: await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest: pass
        await callback.answer(); return
        
    is_anonymous = state.get('is_anonymous', 0); comment = state.get('comment', '')
    if invoice['allow_anonymous'] == 0 and is_anonymous == 1:
        await callback.answer("Создатель счета запретил анонимную оплату.", show_alert=True); return
    if invoice['allow_comments'] == 0 and comment:
        await callback.answer("Создатель счета запретил комментарии.", show_alert=True); return
        
    try:
        # Все 4 шага (списание, зачисление, отметка "оплачено", запись платежа)
        # выполняются одной транзакцией: либо применяются все, либо ни один
        # (см. Database.process_payment) — деньги гарантированно не "теряются" по пути.
        db.process_payment(
            invoice_id=invoice_id,
            payer_id=user_id,
            creator_id=invoice['creator_id'],
            currency=currency,
            amount=amount_in_currency,
            amount_usd=amount_usd,
            comment=comment,
            is_anonymous=is_anonymous,
            mark_single_paid=(invoice['invoice_type'] == 'single'),
        )
    except Exception as e:
        print(f"Error during payment transaction: {e}")
        await callback.answer("Произошла ошибка при обработке платежа. Средства не списаны.", show_alert=True); return
        
    if invoice['invoice_type'] == 'single':
        await update_paid_invoice_messages(invoice_id, amount_usd)

    try: await callback.message.delete()
    except: pass
    await callback.answer()
    
    ok_msg = await bot.send_message(user_id, "👌")
    await asyncio.sleep(2)
    safe_comment = html.escape(comment) if comment else ''
    payer_text = (f"<tg-emoji emoji-id=\"5312043357311111246\">📥</tg-emoji> Вы оплатили счёт #{invoice_id} "
                  f"на сумму <b>{format_balance(amount_in_currency)} {currency} (${format_balance(amount_usd)})</b>.")
    if safe_comment: payer_text += f"\n\n<tg-emoji emoji-id=\"5312103894875143512\">💬</tg-emoji> {safe_comment}"
    try: await bot.send_message(user_id, payer_text, parse_mode='HTML')
    except: pass
    
    if is_anonymous: payer_name = "Аноним"
    else:
        try:
            user = await bot.get_chat(user_id)
            payer_name = user.full_name or user.username or "Пользователь"
        except: payer_name = "Пользователь"
    payer_name = html.escape(payer_name)
        
    emoji_id = CRYPTO_EMOJIS.get(currency, "5310191758255099001")
    creator_text = (f"<b>{payer_name}</b> оплатил(а) ваш счет #{invoice_id}. "
                    f"Вы получили <tg-emoji emoji-id=\"{emoji_id}\">☺️</tg-emoji> <b>{format_balance(amount_in_currency)} {currency} (${format_balance(amount_usd)})</b>.")
    if safe_comment: creator_text += f"\n\n<tg-emoji emoji-id=\"5312103894875143512\">💬</tg-emoji> {safe_comment}"
    try: await bot.send_message(invoice['creator_id'], creator_text, parse_mode='HTML')
    except: pass

# ==========================================
# ЗАПУСК
# ==========================================
async def main():
    print("Бот запущен...")
    # Тянем реальные курсы перед стартом поллинга, чтобы бот не открылся
    # на резервных (потенциально устаревших) значениях из USD_RATES.
    await fetch_crypto_rates()
    refresh_task = asyncio.create_task(rates_refresh_loop())
    try:
        await dp.start_polling(bot)
    finally:
        refresh_task.cancel()
        db.close()

if __name__ == '__main__':
    asyncio.run(main())
