import asyncio
import json
import logging
import os
from pathlib import Path
from typing import List, Dict, Any, Optional

from aiohttp import ClientSession, CookieJar, web
from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

# Загрузка настроек
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
SITE_URL = os.getenv("SITE_URL", "https://ischia-shop.ischia-ft.workers.dev").rstrip("/")
DEFAULT_MANAGER_USER = os.getenv("ADMIN_USERNAME", "Darko")
DEFAULT_MANAGER_PASS = os.getenv("ADMIN_PASSWORD", "Zanda2011@")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "10"))

DATA_DIR = Path(__file__).parent
USERS_DB_FILE = DATA_DIR / "users_db.json"
SEEN_FILE = DATA_DIR / "seen_tickets.json"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("ischia_bot")

STATUS_MAP = {
    "open": "🟡 Открыт",
    "processing": "🔵 В очереди",
    "awaiting_payment": "🟠 Ожидает чек (Kaspi)",
    "completed": "🟢 Выдан (Выполнен)",
    "cancelled": "🔴 Отменён",
}

RANKS = [
    {"name": "Новичок", "min": 0, "next": 3},
    {"name": "Боец", "min": 3, "next": 7},
    {"name": "Элита", "min": 7, "next": 15},
    {"name": "VIP", "min": 15, "next": None},
]


def get_rank(completed_orders: int) -> str:
    current = "Новичок"
    for r in RANKS:
        if completed_orders >= r["min"]:
            current = r["name"]
    return current


# ==========================================================
# База данных пользователей Telegram (users_db.json)
# ==========================================================
class UserDB:
    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.data: Dict[str, Dict[str, Any]] = self._load()

        # Если в базе нет дефолтного админа, добавляем
        if ADMIN_CHAT_ID and ADMIN_CHAT_ID not in self.data and DEFAULT_MANAGER_USER:
            self.data[ADMIN_CHAT_ID] = {
                "username": DEFAULT_MANAGER_USER,
                "password": DEFAULT_MANAGER_PASS,
                "role": "manager",
                "known_tickets": {},
            }
            self._save()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if self.filepath.exists():
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Ошибка загрузки users_db: {e}")
        return {}

    def _save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения users_db: {e}")

    def get_user(self, chat_id: int | str) -> Optional[Dict[str, Any]]:
        return self.data.get(str(chat_id))

    def set_user(self, chat_id: int | str, user_dict: Dict[str, Any]):
        cid = str(chat_id)
        if cid not in self.data:
            self.data[cid] = {}
        self.data[cid].update(user_dict)
        if "known_tickets" not in self.data[cid]:
            self.data[cid]["known_tickets"] = {}
        self._save()

    def remove_user(self, chat_id: int | str):
        cid = str(chat_id)
        if cid in self.data:
            del self.data[cid]
            self._save()

    def all_users(self) -> Dict[str, Dict[str, Any]]:
        return self.data


db = UserDB(USERS_DB_FILE)


# ==========================================================
# API Клиент сайта Ischia Shop
# ==========================================================
class IschiaClient:
    def __init__(self, site_url: str):
        self.site_url = site_url

    async def _make_session(self) -> ClientSession:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
        }
        return ClientSession(cookie_jar=CookieJar(), headers=headers)

    async def login(self, username: str, password: str) -> Dict[str, Any]:
        async with await self._make_session() as session:
            url = f"{self.site_url}/api/auth/login"
            payload = {"username": username, "password": password}
            try:
                async with session.post(url, json=payload, timeout=12) as resp:
                    data = await resp.json()
                    if resp.status == 200 and data.get("user"):
                        return {"success": True, "user": data["user"]}
                    return {"success": False, "error": data.get("error", f"Ошибка {resp.status}")}
            except Exception as e:
                return {"success": False, "error": f"Ошибка соединения: {e}"}

    async def register(self, username: str, password: str) -> Dict[str, Any]:
        async with await self._make_session() as session:
            url = f"{self.site_url}/api/auth/register"
            payload = {"username": username, "password": password}
            try:
                async with session.post(url, json=payload, timeout=12) as resp:
                    data = await resp.json()
                    if resp.status in (200, 201) and data.get("user"):
                        return {"success": True, "user": data["user"]}
                    return {"success": False, "error": data.get("error", f"Ошибка {resp.status}")}
            except Exception as e:
                return {"success": False, "error": f"Ошибка соединения: {e}"}

    async def get_player_profile_and_tickets(self, username: str, password: str) -> Dict[str, Any]:
        async with await self._make_session() as session:
            login_url = f"{self.site_url}/api/auth/login"
            try:
                async with session.post(login_url, json={"username": username, "password": password}, timeout=10) as r:
                    if r.status != 200:
                        return {"success": False, "error": "Неверный логин или пароль"}
                    user_data = (await r.json()).get("user", {})

                tickets_url = f"{self.site_url}/api/tickets"
                async with session.get(tickets_url, timeout=10) as tr:
                    if tr.status == 200:
                        t_data = await tr.json()
                        return {"success": True, "user": user_data, "tickets": t_data.get("tickets", [])}
                    return {"success": False, "error": "Не удалось загрузить тикеты"}
            except Exception as e:
                return {"success": False, "error": str(e)}

    async def get_manager_tickets(self, username: str, password: str) -> Dict[str, Any]:
        async with await self._make_session() as session:
            login_url = f"{self.site_url}/api/auth/login"
            try:
                async with session.post(login_url, json={"username": username, "password": password}, timeout=10) as r:
                    if r.status != 200:
                        return {"success": False, "error": "Не удалось войти в аккаунт менеджера"}

                mgr_url = f"{self.site_url}/api/manager/tickets"
                async with session.get(mgr_url, timeout=10) as mr:
                    if mr.status == 200:
                        data = await mr.json()
                        return {"success": True, "tickets": data.get("tickets", [])}
                    return {"success": False, "error": f"Ошибка доступа к тикетам менеджера: {mr.status}"}
            except Exception as e:
                return {"success": False, "error": str(e)}


api_client = IschiaClient(SITE_URL)


# ==========================================================
# FSM Состояния для диалогов Входа и Регистрации
# ==========================================================
class AuthStates(StatesGroup):
    waiting_login_username = State()
    waiting_login_password = State()
    waiting_reg_username = State()
    waiting_reg_password = State()


# ==========================================================
# Клавиатуры (Меню для Игрока и Менеджера)
# ==========================================================
def get_main_keyboard(role: Optional[str] = None):
    builder = ReplyKeyboardBuilder()

    if role in ("manager", "admin"):
        builder.button(text="🛡️ Все тикеты (Очередь)")
        builder.button(text="🔔 Проверить обновления")
        builder.button(text="👤 Профиль менеджера")
        builder.button(text="🌐 Открыть панель сайта")
        builder.button(text="🚪 Выйти из аккаунта")
        builder.adjust(2, 2, 1)
    elif role == "user":
        builder.button(text="📋 Мои тикеты / заказы")
        builder.button(text="🔔 Проверить статус")
        builder.button(text="👤 Мой профиль / Роль")
        builder.button(text="🌐 Сайт Ischia Shop")
        builder.button(text="🚪 Выйти")
        builder.adjust(2, 2, 1)
    else:
        builder.button(text="🔑 Войти в аккаунт")
        builder.button(text="📝 Зарегистрироваться")
        builder.button(text="ℹ️ О магазине Ischia")
        builder.button(text="🌐 Открыть сайт")
        builder.adjust(2, 2)

    return builder.as_markup(resize_keyboard=True)


# ==========================================================
# Инициализация бота
# ==========================================================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())


# ==========================================================
# Хэндлеры бота
# ==========================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    chat_id = str(message.chat.id)
    user_record = db.get_user(chat_id)

    if user_record:
        role = user_record.get("role", "user")
        role_title = "🛡️ Менеджер" if role in ("manager", "admin") else "🎮 Игрок (Покупатель)"
        await message.answer(
            f"👋 <b>С возвращением, {user_record.get('username')}!</b>\n\n"
            f"Ваша роль: <b>{role_title}</b>\n"
            f"Сайт: <code>{SITE_URL}</code>\n\n"
            f"Выберите нужное действие в меню ниже 👇",
            reply_markup=get_main_keyboard(role)
        )
    else:
        await message.answer(
            "👋 <b>Добро пожаловать в Ischia Shop Bot!</b>\n\n"
            "Здесь вы можете отслеживать свои заказы монет, инвентарей и софта на Minecraft FunTime, "
            "получать уведомления о выдаче товара и писать оператору.\n\n"
            "Войдите в существующий профиль или зарегистрируйтесь 👇",
            reply_markup=get_main_keyboard(None)
        )


# --- Авторизация ---
@dp.message(F.text == "🔑 Войти в аккаунт")
@dp.message(Command("login"))
async def cmd_login_start(message: types.Message, state: FSMContext):
    await message.answer("✍️ Введите ваш <b>логин (никнейм)</b> на сайте Ischia Shop:")
    await state.set_state(AuthStates.waiting_login_username)


@dp.message(AuthStates.waiting_login_username)
async def process_login_username(message: types.Message, state: FSMContext):
    username = message.text.strip()
    if len(username) < 3:
        await message.answer("❌ Логин должен содержать от 3 символов. Попробуйте снова:")
        return
    await state.update_data(username=username)
    await message.answer(f"🔒 Теперь введите <b>пароль</b> для аккаунта <code>{username}</code>:")
    await state.set_state(AuthStates.waiting_login_password)


@dp.message(AuthStates.waiting_login_password)
async def process_login_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    username = data["username"]
    await state.clear()

    await message.answer("🔄 Проверяю данные на сайте...")
    res = await api_client.login(username, password)

    if res.get("success"):
        user_info = res["user"]
        role = user_info.get("role", "user")
        role_label = "🛡️ Менеджер" if role in ("manager", "admin") else "🎮 Игрок (Покупатель)"

        db.set_user(message.chat.id, {
            "username": username,
            "password": password,
            "role": role,
            "user_id": user_info.get("id"),
        })

        await message.answer(
            f"✅ <b>Успешный вход!</b>\n\n"
            f"👤 Аккаунт: <code>{username}</code>\n"
            f"🏷️ Роль: <b>{role_label}</b>\n"
            f"💰 Кешбэк: <b>{user_info.get('bonusBalanceKzt', 0)} ₸</b>\n\n"
            "Теперь бот будет уведомлять вас обо всех обновлениях!",
            reply_markup=get_main_keyboard(role)
        )
    else:
        await message.answer(
            f"❌ <b>Ошибка входа:</b> {res.get('error', 'Неверный логин или пароль')}\n\n"
            "Попробуйте нажать кнопку «🔑 Войти в аккаунт» снова или зарегистрируйтесь.",
            reply_markup=get_main_keyboard(None)
        )


# --- Регистрация ---
@dp.message(F.text == "📝 Зарегистрироваться")
@dp.message(Command("register"))
async def cmd_register_start(message: types.Message, state: FSMContext):
    await message.answer(
        "📝 <b>Регистрация в Ischia Shop</b>\n\n"
        "Придумайте <b>логин</b> (3–24 символа, латиница, цифры, _ -):"
    )
    await state.set_state(AuthStates.waiting_reg_username)


@dp.message(AuthStates.waiting_reg_username)
async def process_reg_username(message: types.Message, state: FSMContext):
    username = message.text.strip()
    if len(username) < 3 or len(username) > 24:
        await message.answer("❌ Логин должен содержать от 3 до 24 символов. Введите другой:")
        return
    await state.update_data(username=username)
    await message.answer(f"🔒 Придумайте <b>пароль</b> для <code>{username}</code> (минимум 6 символов):")
    await state.set_state(AuthStates.waiting_reg_password)


@dp.message(AuthStates.waiting_reg_password)
async def process_reg_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    if len(password) < 6:
        await message.answer("❌ Пароль должен быть минимум 6 символов. Попробуйте снова:")
        return

    data = await state.get_data()
    username = data["username"]
    await state.clear()

    await message.answer("🔄 Создаю аккаунт на сайте Ischia Shop...")
    res = await api_client.register(username, password)

    if res.get("success"):
        user_info = res["user"]
        role = user_info.get("role", "user")

        db.set_user(message.chat.id, {
            "username": username,
            "password": password,
            "role": role,
            "user_id": user_info.get("id"),
        })

        await message.answer(
            f"🎉 <b>Аккаунт успешно создан!</b>\n\n"
            f"👤 Никнейм: <code>{username}</code>\n"
            f"🏷️ Роль: <b>🎮 Игрок</b>\n"
            f"🎖️ Ранг: <b>Новичок</b>\n"
            f"💰 Бонусный баланс: <b>0 ₸</b>\n\n"
            "Вы можете создавать заказы на сайте и проверять их статус прямо здесь!",
            reply_markup=get_main_keyboard(role)
        )
    else:
        await message.answer(
            f"❌ <b>Ошибка регистрации:</b> {res.get('error')}\n"
            "Попробуйте выбрать другой логин.",
            reply_markup=get_main_keyboard(None)
        )


# --- Профиль и Роль ---
@dp.message(F.text.in_(["👤 Мой профиль / Роль", "👤 Профиль менеджера"]))
@dp.message(Command("profile"))
async def cmd_profile(message: types.Message):
    user_record = db.get_user(message.chat.id)
    if not user_record:
        await message.answer("Сначала войдите в аккаунт через кнопку «🔑 Войти в аккаунт».")
        return

    res = await api_client.get_player_profile_and_tickets(user_record["username"], user_record["password"])
    if not res.get("success"):
        await message.answer(f"❌ Ошибка загрузки профиля: {res.get('error')}")
        return

    u = res["user"]
    role = u.get("role", user_record.get("role", "user"))
    role_str = "🛡️ <b>Менеджер магазина</b>" if role in ("manager", "admin") else "🎮 <b>Игрок (Покупатель)</b>"
    orders_cnt = u.get("completedOrders", 0)
    rank_name = get_rank(orders_cnt)

    text = [
        "══════════════════════",
        "         <b>ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ</b>",
        "══════════════════════\n",
        f"👤 <b>Логин:</b> <code>{u.get('username')}</code>",
        f"🏷️ <b>Ваша роль:</b> {role_str}",
        f"🎖️ <b>Ранг:</b> {rank_name}",
        f"💰 <b>Баланс кешбэка:</b> {u.get('bonusBalanceKzt', 0)} ₸",
        f"📦 <b>Завершённых покупок:</b> {orders_cnt}",
    ]

    if role in ("manager", "admin"):
        text.append("\n<i>Вам доступны функции оператора: просмотр всех входящих заявок покупателей.</i>")
    else:
        text.append("\n<i>За каждую покупку монет от 50 кк начисляется 3% кешбэка на ваш счёт!</i>")

    await message.answer("\n".join(text), reply_markup=get_main_keyboard(role))


# --- Тикеты игрока ---
@dp.message(F.text == "📋 Мои тикеты / заказы")
@dp.message(Command("tickets"))
async def cmd_my_tickets(message: types.Message):
    user_record = db.get_user(message.chat.id)
    if not user_record:
        await message.answer("Сначала войдите в аккаунт через кнопку «🔑 Войти в аккаунт».")
        return

    # Если менеджер нажал эту команду, отправляем его в менеджерскую ветку
    if user_record.get("role") in ("manager", "admin"):
        await cmd_manager_tickets(message)
        return

    await message.answer("🔄 Запрашиваю ваши тикеты с сайта...")
    res = await api_client.get_player_profile_and_tickets(user_record["username"], user_record["password"])
    if not res.get("success"):
        await message.answer(f"❌ Ошибка: {res.get('error')}")
        return

    tickets = res.get("tickets", [])
    if not tickets:
        kb = InlineKeyboardBuilder()
        kb.button(text="🛒 Перейти в Ischia Shop", url=SITE_URL)
        await message.answer(
            "📭 <b>У вас пока нет активных заказов.</b>\n\n"
            "Перейдите на сайт Ischia Shop, выберите нужное количество монет или инвентарь и оформите тикет!",
            reply_markup=kb.as_markup()
        )
        return

    for t in tickets[:5]:
        status_key = t.get("status", "open")
        status_label = STATUS_MAP.get(status_key, status_key)
        item = t.get("itemLabel") or f"Заказ ({t.get('mode', 'coins')})"
        fiat = f"{t.get('fiatAmount', 0)} ₸" if not t.get("isNegotiated") else "В ЛС"

        msg = [
            f"🎫 <b>Тикет #{t.get('id')}</b>",
            f"Статус: <b>{status_label}</b>",
            f"Товар: <b>{item}</b>",
            f"Сумма: <b>{fiat}</b>",
            f"Ник в игре: <code>{t.get('playerId', '—')}</code>",
        ]
        if t.get("queuePos"):
            msg.append(f"Очередь: #{t.get('queuePos')}")

        kb = InlineKeyboardBuilder()
        kb.button(text="🌐 Открыть тикет на сайте", url=f"{SITE_URL}#tickets")
        await message.answer("\n".join(msg), reply_markup=kb.as_markup())


# --- Тикеты менеджера ---
@dp.message(F.text == "🛡️ Все тикеты (Очередь)")
async def cmd_manager_tickets(message: types.Message):
    user_record = db.get_user(message.chat.id)
    if not user_record or user_record.get("role") not in ("manager", "admin"):
        await message.answer("❌ Доступно только менеджерам.")
        return

    await message.answer("🔄 Загружаю очередь заказов...")
    res = await api_client.get_manager_tickets(user_record["username"], user_record["password"])
    if not res.get("success"):
        await message.answer(f"❌ Ошибка: {res.get('error')}")
        return

    tickets = res.get("tickets", [])
    active = [t for t in tickets if t.get("status") not in ("completed", "cancelled")]
    if not active:
        await message.answer("📭 Активных заказов в очереди сейчас нет.")
        return

    for t in active[:5]:
        status_label = STATUS_MAP.get(t.get("status"), t.get("status"))
        price = "В ЛС" if t.get("isNegotiated") else f"{t.get('kztFinal', 0)} ₸"

        text = [
            f"⚡ <b>Заказ #{t.get('id')}</b> [{status_label}]",
            f"Игрок: <code>{t.get('playerId')}</code>",
            f"TG: {t.get('contact')}",
            f"Товар: {t.get('itemLabel')}",
            f"К оплате: <b>{price}</b>",
        ]
        if t.get("note"):
            text.append(f"Примечание: <i>{t.get('note')}</i>")

        kb = InlineKeyboardBuilder()
        contact = str(t.get("contact", "")).strip().lstrip("@")
        if contact and not contact.startswith("+"):
            kb.button(text="💬 Написать клиенту", url=f"https://t.me/{contact}")
        kb.button(text="🛡️ Панель менеджера", url=f"{SITE_URL}/manager")
        kb.adjust(1)

        await message.answer("\n".join(text), reply_markup=kb.as_markup())


# --- Проверка уведомлений вручную ---
@dp.message(F.text.in_(["🔔 Проверить статус", "🔔 Проверить обновления"]))
@dp.message(Command("check"))
async def cmd_check_updates(message: types.Message):
    chat_id = str(message.chat.id)
    user_record = db.get_user(chat_id)
    if not user_record:
        await message.answer("Войдите в аккаунт, чтобы проверить обновления.")
        return

    await message.answer("🔍 Проверяю статус ваших заявок на сайте...")
    new_notifs = await check_user_notifications(chat_id, user_record)
    if not new_notifs:
        await message.answer("✅ Все статусы актуальны, новых уведомлений нет.")


# --- Ссылки и Инфо ---
@dp.message(F.text.in_(["🌐 Сайт Ischia Shop", "🌐 Открыть сайт", "🌐 Открыть панель сайта"]))
async def cmd_site_link(message: types.Message):
    user_record = db.get_user(message.chat.id)
    is_mgr = user_record and user_record.get("role") in ("manager", "admin")
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Магазин Ischia Shop", url=SITE_URL)
    if is_mgr:
        kb.button(text="🛡️ Панель менеджера", url=f"{SITE_URL}/manager")
    kb.adjust(1)
    await message.answer("Официальный сайт Ischia Shop (Minecraft FunTime):", reply_markup=kb.as_markup())


@dp.message(F.text == "ℹ️ О магазине Ischia")
async def cmd_about(message: types.Message):
    await message.answer(
        "💎 <b>Ischia Shop — Minecraft FunTime</b>\n\n"
        "• Продажа монет FunTime (от 1 кк)\n"
        "• Готовые боевые сеты и инвентари (Бестия, Хаос, Крушитель, Гидра)\n"
        "• Приватный софт и конфиги (Delta, Velka, System)\n"
        "• Оплата Kaspi.kz с кешбэком до 3%\n"
        "• Среднее время выдачи заказа: 4–5 минут\n\n"
        f"🔗 Сайт: <code>{SITE_URL}</code>"
    )


# --- Выход ---
@dp.message(F.text.in_(["🚪 Выйти", "🚪 Выйти из аккаунта"]))
@dp.message(Command("logout"))
async def cmd_logout(message: types.Message, state: FSMContext):
    await state.clear()
    db.remove_user(message.chat.id)
    await message.answer(
        "👋 Вы успешно вышли из аккаунта. Бот больше не будет присылать персональные уведомления.",
        reply_markup=get_main_keyboard(None)
    )


# ==========================================================
# Фоновый мониторинг (для Игроков и Менеджеров)
# ==========================================================
async def check_user_notifications(chat_id: str, urec: Dict[str, Any]) -> int:
    username = urec.get("username")
    password = urec.get("password")
    role = urec.get("role", "user")
    known_tickets = urec.setdefault("known_tickets", {})
    count = 0

    if role in ("manager", "admin"):
        # Для менеджера: уведомления о НОВЫХ поступивших заказах
        m_res = await api_client.get_manager_tickets(username, password)
        if m_res.get("success"):
            for t in m_res.get("tickets", []):
                tid = str(t.get("id"))
                if not tid:
                    continue
                if tid not in known_tickets:
                    known_tickets[tid] = {"status": t.get("status")}
                    count += 1
                    status_label = STATUS_MAP.get(t.get("status"), t.get("status"))
                    price = "В ЛС" if t.get("isNegotiated") else f"{t.get('kztFinal', 0)} ₸"
                    text = (
                        f"⚡ <b>Новый заказ #{tid}!</b> [{status_label}]\n\n"
                        f"🎮 <b>Игрок:</b> <code>{t.get('playerId')}</code>\n"
                        f"💬 <b>Telegram:</b> {t.get('contact')}\n"
                        f"📦 <b>Товар:</b> {t.get('itemLabel')}\n"
                        f"💰 <b>Сумма:</b> {price}\n"
                        + (f"📝 <b>Примечание:</b> {t.get('note')}\n" if t.get("note") else "")
                    )
                    kb = InlineKeyboardBuilder()
                    contact = str(t.get("contact", "")).strip().lstrip("@")
                    if contact and not contact.startswith("+"):
                        kb.button(text="💬 Написать клиенту", url=f"https://t.me/{contact}")
                    kb.button(text="🛡️ Панель менеджера", url=f"{SITE_URL}/manager")
                    kb.adjust(1)
                    try:
                        await bot.send_message(chat_id, text, reply_markup=kb.as_markup())
                    except Exception as e:
                        logger.error(f"Не удалось отправить менеджеру {chat_id}: {e}")
    else:
        # Для игрока: уведомления о СМЕНЕ СТАТУСА его заказа или сообщениях оператора
        p_res = await api_client.get_player_profile_and_tickets(username, password)
        if p_res.get("success"):
            for t in p_res.get("tickets", []):
                tid = str(t.get("id"))
                current_status = t.get("status", "open")
                prev_info = known_tickets.get(tid)

                if prev_info is None:
                    # Первый раз запоминаем текущее состояние
                    known_tickets[tid] = {"status": current_status}
                elif prev_info.get("status") != current_status:
                    # Статус изменился!
                    prev_info["status"] = current_status
                    count += 1
                    status_label = STATUS_MAP.get(current_status, current_status)
                    item = t.get("itemLabel") or "Товар FunTime"

                    text = (
                        f"🔔 <b>Обновление статуса заказа #{tid}!</b>\n\n"
                        f"📦 Товар: <b>{item}</b>\n"
                        f"Новый статус: <b>{status_label}</b>\n\n"
                    )
                    if current_status == "awaiting_payment":
                        text += "👉 <i>Оператор ожидает подтверждение оплаты или чек Kaspi.kz.</i>"
                    elif current_status == "completed":
                        text += "🎉 <i>Ваш заказ успешно выдан в игре! Спасибо за покупку в Ischia Shop!</i>"
                    elif current_status == "processing":
                        text += f"⏱ <i>Заказ принят в работу. Ваше место в очереди: #{t.get('queuePos', 1)}</i>"

                    kb = InlineKeyboardBuilder()
                    kb.button(text="🌐 Открыть тикет на сайте", url=f"{SITE_URL}#tickets")
                    try:
                        await bot.send_message(chat_id, text, reply_markup=kb.as_markup())
                    except Exception as e:
                        logger.error(f"Не удалось отправить игроку {chat_id}: {e}")

    if count > 0:
        db._save()
    return count


async def poll_all_users_loop():
    logger.info("Запуск фонового мониторинга для всех пользователей...")
    while True:
        try:
            users = db.all_users()
            for chat_id, urec in list(users.items()):
                try:
                    await check_user_notifications(chat_id, urec)
                except Exception as ex:
                    logger.warning(f"Ошибка проверки для chat_id {chat_id}: {ex}")
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"Ошибка в цикле мониторинга: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


# ==========================================================
# Встроенный HTTP Сервер для Render.com
# ==========================================================
async def handle_health(request):
    return web.Response(text="OK: Ischia Support Bot with Player & Manager Menus is active!", content_type="text/plain")


async def start_web_server():
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный веб-сервер запущен на порту {port}")


async def keep_alive_ping():
    ext_url = os.getenv("RENDER_EXTERNAL_URL")
    if not ext_url:
        return
    logger.info(f"Запущен keep-alive пинг для: {ext_url}")
    while True:
        await asyncio.sleep(600)  # каждые 10 минут
        try:
            async with ClientSession() as s:
                async with s.get(ext_url, timeout=10) as r:
                    logger.info(f"Keep-alive пинг: статус {r.status}")
        except Exception as e:
            logger.warning(f"Ошибка keep-alive: {e}")


async def main():
    logger.info("Запуск Ischia Support Bot (Multi-role)...")
    await start_web_server()
    asyncio.create_task(keep_alive_ping())
    asyncio.create_task(poll_all_users_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
