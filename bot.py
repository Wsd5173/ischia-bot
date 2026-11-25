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
    "awaiting_payment": "🟠 Ожидает чек",
    "completed": "🟢 Выдан",
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


def is_manager_or_higher(role: Optional[str]) -> bool:
    if not role:
        return False
    return role.lower() in ("manager", "admin", "owner", "moderator")


# ==========================================================
# База данных пользователей Telegram (users_db.json)
# ==========================================================
class UserDB:
    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.data: Dict[str, Dict[str, Any]] = self._load()

        # Инициализируем менеджера по умолчанию из .env
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

    async def verify_user_on_site(self, username: str, password: str) -> Dict[str, Any]:
        """Проверяет актуальную роль и профиль напрямую с сайта"""
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

    async def get_player_tickets(self, username: str, password: str) -> Dict[str, Any]:
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
                        return {"success": False, "error": "Не удалось войти под аккаунтом менеджера"}
                    user_info = (await r.json()).get("user", {})

                mgr_url = f"{self.site_url}/api/manager/tickets"
                async with session.get(mgr_url, timeout=10) as mr:
                    if mr.status == 200:
                        data = await mr.json()
                        return {"success": True, "user": user_info, "tickets": data.get("tickets", [])}
                    return {"success": False, "error": f"Ошибка доступа к панели менеджера: {mr.status}"}
            except Exception as e:
                return {"success": False, "error": str(e)}

    async def set_ticket_status(self, username: str, password: str, ticket_id: str, new_status: str) -> bool:
        async with await self._make_session() as session:
            login_url = f"{self.site_url}/api/auth/login"
            try:
                async with session.post(login_url, json={"username": username, "password": password}, timeout=10) as r:
                    if r.status != 200:
                        return False

                patch_url = f"{self.site_url}/api/manager/tickets/{ticket_id}"
                async with session.patch(patch_url, json={"status": new_status}, timeout=10) as pr:
                    return pr.status == 200
            except Exception as e:
                logger.error(f"Ошибка изменения статуса {ticket_id}: {e}")
                return False


api_client = IschiaClient(SITE_URL)


# ==========================================================
# FSM Состояния для диалогов
# ==========================================================
class AuthStates(StatesGroup):
    waiting_login_username = State()
    waiting_login_password = State()
    waiting_reg_username = State()
    waiting_reg_password = State()


# ==========================================================
# Клавиатуры Меню
# ==========================================================
def get_main_keyboard(role: Optional[str] = None):
    builder = ReplyKeyboardBuilder()

    if is_manager_or_higher(role):
        # Панель менеджера / администратора
        builder.button(text="🛡️ Панель менеджера")
        builder.button(text="📋 Очередь тикетов (Все)")
        builder.button(text="🔔 Проверить новые заявки")
        builder.button(text="👤 Моя роль и профиль")
        builder.button(text="🌐 Открыть панель на сайте")
        builder.button(text="🚪 Выйти из аккаунта")
        builder.adjust(2, 2, 2)
    elif role == "user":
        # Отдельное меню для игрока
        builder.button(text="📋 Мои тикеты / заказы")
        builder.button(text="🔔 Проверить статус")
        builder.button(text="👤 Мой профиль / Роль")
        builder.button(text="🛒 Купить в Ischia Shop")
        builder.button(text="🚪 Выйти")
        builder.adjust(2, 2, 1)
    else:
        # Для неавторизованных
        builder.button(text="🔑 Войти в аккаунт")
        builder.button(text="📝 Зарегистрироваться")
        builder.button(text="ℹ️ О магазине Ischia")
        builder.button(text="🌐 Открыть сайт")
        builder.adjust(2, 2)

    return builder.as_markup(resize_keyboard=True)


def build_manager_ticket_inline(ticket: Dict[str, Any]):
    """Инлайн-кнопки управления тикетом для менеджера"""
    builder = InlineKeyboardBuilder()
    tid = str(ticket.get("id"))
    contact = str(ticket.get("contact", "")).strip().lstrip("@")

    # Кнопки быстрой смены статуса
    builder.button(text="🔵 В очередь", callback_data=f"st:{tid}:processing")
    builder.button(text="🟠 Чек Kaspi", callback_data=f"st:{tid}:awaiting_payment")
    builder.button(text="🟢 Выдан", callback_data=f"st:{tid}:completed")
    builder.button(text="🔴 Отменить", callback_data=f"st:{tid}:cancelled")

    if contact and not contact.startswith("+"):
        builder.button(text="💬 Написать покупателю в TG", url=f"https://t.me/{contact}")

    builder.button(text="🌐 Открыть на сайте", url=f"{SITE_URL}/manager")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


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
        # Проверяем роль в реальном времени на сайте
        fresh = await api_client.verify_user_on_site(user_record["username"], user_record["password"])
        if fresh.get("success"):
            fresh_role = fresh["user"].get("role", "user")
            user_record["role"] = fresh_role
            db.set_user(chat_id, user_record)

        role = user_record.get("role", "user")

        if is_manager_or_higher(role):
            await message.answer(
                f"🛡️ <b>ВЫ АВТОРИЗОВАНЫ КАК МЕНЕДЖЕР!</b>\n\n"
                f"👤 Логин: <code>{user_record.get('username')}</code>\n"
                f"Роль на сайте: <b>{role.upper()}</b>\n"
                f"Вам выдана <b>Панель управления очередью и заказами</b>.\n\n"
                "Вы можете управлять статусами тикетов и отвечать клиентам прямо отсюда 👇",
                reply_markup=get_main_keyboard(role)
            )
        else:
            await message.answer(
                f"👋 <b>С возвращением, {user_record.get('username')}!</b>\n\n"
                f"Роль на сайте: <b>🎮 Игрок (Покупатель)</b>\n"
                f"Сайт: <code>{SITE_URL}</code>\n\n"
                "Ваше меню игрока готово к работе 👇",
                reply_markup=get_main_keyboard(role)
            )
    else:
        await message.answer(
            "👋 <b>Добро пожаловать в Ischia Shop Bot!</b>\n\n"
            "Здесь можно отслеживать покупки FunTime, получать уведомления о выдаче товара, "
            "а для менеджеров доступна панель обработки заказов.\n\n"
            "Войдите в аккаунт или зарегистрируйтесь:",
            reply_markup=get_main_keyboard(None)
        )


# --- Авторизация с моментальной проверкой роли ---
@dp.message(F.text == "🔑 Войти в аккаунт")
@dp.message(Command("login"))
async def cmd_login_start(message: types.Message, state: FSMContext):
    await message.answer("✍️ Введите ваш <b>логин (никнейм)</b> на сайте Ischia Shop:")
    await state.set_state(AuthStates.waiting_login_username)


@dp.message(AuthStates.waiting_login_username)
async def process_login_username(message: types.Message, state: FSMContext):
    username = message.text.strip()
    if len(username) < 3:
        await message.answer("❌ Логин должен быть от 3 символов. Попробуйте снова:")
        return
    await state.update_data(username=username)
    await message.answer(f"🔒 Введите <b>пароль</b> для аккаунта <code>{username}</code>:")
    await state.set_state(AuthStates.waiting_login_password)


@dp.message(AuthStates.waiting_login_password)
async def process_login_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    username = data["username"]
    await state.clear()

    await message.answer("🔍 Проверяю логин, пароль и роль на сайте...")
    res = await api_client.verify_user_on_site(username, password)

    if res.get("success"):
        user_info = res["user"]
        role = user_info.get("role", "user")

        db.set_user(message.chat.id, {
            "username": username,
            "password": password,
            "role": role,
            "user_id": user_info.get("id"),
        })

        if is_manager_or_higher(role):
            await message.answer(
                f"✅ <b>ВХОД ВЫПОЛНЕН: РОЛЬ МЕНЕДЖЕР!</b>\n\n"
                f"👤 Пользователь: <code>{username}</code>\n"
                f"🛡️ Роль на сайте: <b>{role.upper()}</b>\n\n"
                "🔥 <b>Вам выдана панель менеджера!</b>\n"
                "Все новые входящие заказы будут приходить вам с кнопками прямого управления статусом.",
                reply_markup=get_main_keyboard(role)
            )
        else:
            await message.answer(
                f"✅ <b>Вход выполнен!</b>\n\n"
                f"👤 Игрок: <code>{username}</code>\n"
                f"🏷️ Роль: <b>🎮 Игрок (Покупатель)</b>\n"
                f"💰 Бонусный кешбэк: <b>{user_info.get('bonusBalanceKzt', 0)} ₸</b>\n\n"
                "Вам выдано персональное меню игрока 👇",
                reply_markup=get_main_keyboard(role)
            )
    else:
        await message.answer(
            f"❌ <b>Ошибка входа:</b> {res.get('error', 'Неверный логин или пароль')}\n"
            "Проверьте данные и попробуйте снова.",
            reply_markup=get_main_keyboard(None)
        )


# --- Регистрация ---
@dp.message(F.text == "📝 Зарегистрироваться")
@dp.message(Command("register"))
async def cmd_register_start(message: types.Message, state: FSMContext):
    await message.answer(
        "📝 <b>Регистрация нового аккаунта</b>\n\n"
        "Придумайте логин (3–24 символа, латиница, цифры, _ -):"
    )
    await state.set_state(AuthStates.waiting_reg_username)


@dp.message(AuthStates.waiting_reg_username)
async def process_reg_username(message: types.Message, state: FSMContext):
    username = message.text.strip()
    if len(username) < 3 or len(username) > 24:
        await message.answer("❌ Логин должен содержать от 3 до 24 символов. Введите другой:")
        return
    await state.update_data(username=username)
    await message.answer(f"🔒 Придумайте пароль для <code>{username}</code> (минимум 6 символов):")
    await state.set_state(AuthStates.waiting_reg_password)


@dp.message(AuthStates.waiting_reg_password)
async def process_reg_password(message: types.Message, state: FSMContext):
    password = message.text.strip()
    if len(password) < 6:
        await message.answer("❌ Пароль должен быть от 6 символов. Попробуйте снова:")
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
            f"🎉 <b>Аккаунт зарегистрирован!</b>\n\n"
            f"👤 Никнейм: <code>{username}</code>\n"
            f"🏷️ Ваша роль: <b>🎮 Игрок</b>\n\n"
            "Вам выдано персональное меню игрока. Создавайте заказы на сайте и следите за статусом прямо здесь!",
            reply_markup=get_main_keyboard(role)
        )
    else:
        await message.answer(
            f"❌ <b>Ошибка регистрации:</b> {res.get('error')}",
            reply_markup=get_main_keyboard(None)
        )


# --- Панель Менеджера (Только для role >= manager) ---
@dp.message(F.text.in_(["🛡️ Панель менеджера", "📋 Очередь тикетов (Все)"]))
@dp.message(Command("panel"))
async def cmd_manager_panel(message: types.Message):
    user_record = db.get_user(message.chat.id)
    if not user_record:
        await message.answer("Сначала войдите в аккаунт через кнопку «🔑 Войти в аккаунт».")
        return

    # Динамическая проверка роли на сайте
    fresh = await api_client.verify_user_on_site(user_record["username"], user_record["password"])
    if fresh.get("success"):
        user_record["role"] = fresh["user"].get("role", "user")
        db.set_user(message.chat.id, user_record)

    role = user_record.get("role", "user")
    if not is_manager_or_higher(role):
        await message.answer(
            f"⛔ <b>Доступ ограничен!</b>\n\n"
            f"Ваша роль на сайте: <b>🎮 Игрок</b>.\n"
            "Панель управления очередью тикетов выдаётся только менеджерам и администраторам сайта.",
            reply_markup=get_main_keyboard("user")
        )
        return

    await message.answer("🔄 Загружаю актуальную очередь тикетов...")
    res = await api_client.get_manager_tickets(user_record["username"], user_record["password"])
    if not res.get("success"):
        await message.answer(f"❌ Ошибка загрузки: {res.get('error')}")
        return

    tickets = res.get("tickets", [])
    active = [t for t in tickets if t.get("status") not in ("completed", "cancelled")]

    header = (
        "═════════════════════════\n"
        "       🛡️ <b>ПАНЕЛЬ МЕНЕДЖЕРА</b>\n"
        "═════════════════════════\n"
        f"Оператор: <code>{user_record['username']}</code> (Роль: <b>{role.upper()}</b>)\n"
        f"Активных тикетов в очереди: <b>{len(active)}</b> (Всего: {len(tickets)})\n"
    )
    await message.answer(header)

    if not active:
        await message.answer("✅ В очереди нет ожидающих заказов. Все заявки выданы!")
        return

    for t in active[:6]:
        status_label = STATUS_MAP.get(t.get("status"), t.get("status"))
        price = "В ЛС" if t.get("isNegotiated") else f"{t.get('kztFinal', 0)} ₸"

        text = [
            f"🎫 <b>Тикет #{t.get('id')}</b> [{status_label}]",
            f"🎮 Игрок FunTime: <code>{t.get('playerId')}</code> ({t.get('region', 'Анархия')})",
            f"👤 Покупатель: <code>{t.get('accountUsername', '—')}</code>",
            f"💬 TG: {t.get('contact')}",
            f"📦 Товар: <b>{t.get('itemLabel')}</b>",
            f"💰 Сумма: <b>{price}</b>",
        ]
        if t.get("note"):
            text.append(f"📝 Комментарий: <i>{t.get('note')}</i>")

        reply_markup = build_manager_ticket_inline(t)
        await message.answer("\n".join(text), reply_markup=reply_markup)


# --- Смена статуса тикета менеджером по Inline-кнопке ---
@dp.callback_query(F.data.startswith("st:"))
async def callback_change_ticket_status(callback: types.CallbackQuery):
    user_record = db.get_user(callback.from_user.id)
    if not user_record or not is_manager_or_higher(user_record.get("role")):
        await callback.answer("❌ У вас нет прав менеджера для изменения статуса.", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 3:
        return

    _, ticket_id, new_status = parts
    status_label = STATUS_MAP.get(new_status, new_status)

    ok = await api_client.set_ticket_status(
        user_record["username"],
        user_record["password"],
        ticket_id,
        new_status
    )

    if ok:
        await callback.answer(f"✅ Статус тикета #{ticket_id} изменён на: {status_label}", show_alert=True)
        # Обновляем текст сообщения
        try:
            old_text = callback.message.html_text if callback.message else ""
            if old_text:
                updated_text = old_text + f"\n\n<i>[Статус обновлён на: {status_label}]</i>"
                await callback.message.edit_text(updated_text, reply_markup=callback.message.reply_markup)
        except Exception:
            pass
    else:
        await callback.answer(f"❌ Не удалось изменить статус тикета #{ticket_id}", show_alert=True)


# --- Профиль и Роль ---
@dp.message(F.text.in_(["👤 Мой профиль / Роль", "👤 Моя роль и профиль"]))
@dp.message(Command("profile"))
async def cmd_profile(message: types.Message):
    user_record = db.get_user(message.chat.id)
    if not user_record:
        await message.answer("Сначала войдите в аккаунт.")
        return

    # Всегда запрашиваем свежие данные с сайта
    fresh = await api_client.verify_user_on_site(user_record["username"], user_record["password"])
    if not fresh.get("success"):
        await message.answer(f"❌ Ошибка: {fresh.get('error')}")
        return

    u = fresh["user"]
    role = u.get("role", "user")
    user_record["role"] = role
    db.set_user(message.chat.id, user_record)

    is_mgr = is_manager_or_higher(role)
    role_title = f"🛡️ <b>Менеджер магазина ({role.upper()})</b>" if is_mgr else "🎮 <b>Игрок (Покупатель)</b>"
    orders_cnt = u.get("completedOrders", 0)

    text = [
        "═════════════════════════",
        "        <b>ВАШ ПРОФИЛЬ НА САЙТЕ</b>",
        "═════════════════════════\n",
        f"👤 <b>Логин:</b> <code>{u.get('username')}</code>",
        f"🏷️ <b>Роль на сайте:</b> {role_title}",
    ]

    if is_mgr:
        text.append("⚡ <b>Доступ:</b> Выдана панель менеджера и обработка заказов.")
    else:
        text.append(f"🎖️ <b>Ранг игрока:</b> {get_rank(orders_cnt)}")
        text.append(f"💰 <b>Баланс кешбэка:</b> {u.get('bonusBalanceKzt', 0)} ₸")
        text.append(f"📦 <b>Завершённых заказов:</b> {orders_cnt}")

    await message.answer("\n".join(text), reply_markup=get_main_keyboard(role))


# --- Тикеты игрока ---
@dp.message(F.text == "📋 Мои тикеты / заказы")
@dp.message(Command("tickets"))
async def cmd_my_tickets(message: types.Message):
    user_record = db.get_user(message.chat.id)
    if not user_record:
        await message.answer("Сначала войдите в аккаунт.")
        return

    if is_manager_or_higher(user_record.get("role")):
        await cmd_manager_panel(message)
        return

    await message.answer("🔄 Запрашиваю ваши тикеты с сайта...")
    res = await api_client.get_player_tickets(user_record["username"], user_record["password"])
    if not res.get("success"):
        await message.answer(f"❌ Ошибка: {res.get('error')}")
        return

    tickets = res.get("tickets", [])
    if not tickets:
        kb = InlineKeyboardBuilder()
        kb.button(text="🛒 Перейти в Ischia Shop", url=SITE_URL)
        await message.answer(
            "📭 <b>У вас пока нет заказов.</b>\n"
            "Перейдите в магазин, выберите монеты или инвентарь и создайте заявку!",
            reply_markup=kb.as_markup()
        )
        return

    for t in tickets[:5]:
        status_label = STATUS_MAP.get(t.get("status"), t.get("status"))
        item = t.get("itemLabel") or "Заказ FunTime"
        fiat = f"{t.get('fiatAmount', 0)} ₸" if not t.get("isNegotiated") else "В ЛС"

        msg = [
            f"🎫 <b>Тикет #{t.get('id')}</b>",
            f"Статус: <b>{status_label}</b>",
            f"Товар: <b>{item}</b>",
            f"Сумма: <b>{fiat}</b>",
            f"Ник: <code>{t.get('playerId', '—')}</code>",
        ]
        if t.get("queuePos"):
            msg.append(f"Очередь: #{t.get('queuePos')}")

        kb = InlineKeyboardBuilder()
        kb.button(text="🌐 Открыть тикет на сайте", url=f"{SITE_URL}#tickets")
        await message.answer("\n".join(msg), reply_markup=kb.as_markup())


# --- Проверка уведомлений ---
@dp.message(F.text.in_(["🔔 Проверить статус", "🔔 Проверить новые заявки"]))
@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    user_record = db.get_user(message.chat.id)
    if not user_record:
        await message.answer("Войдите в аккаунт.")
        return

    await message.answer("🔍 Проверяю статус на сайте...")
    new_notifs = await check_user_notifications(str(message.chat.id), user_record)
    if not new_notifs:
        await message.answer("✅ Новых изменений нет, все статусы актуальны.")


# --- Ссылки ---
@dp.message(F.text.in_(["🛒 Купить в Ischia Shop", "🌐 Открыть сайт", "🌐 Открыть панель на сайте"]))
async def cmd_links(message: types.Message):
    user_record = db.get_user(message.chat.id)
    is_mgr = user_record and is_manager_or_higher(user_record.get("role"))
    kb = InlineKeyboardBuilder()
    kb.button(text="🛒 Магазин Ischia Shop", url=SITE_URL)
    if is_mgr:
        kb.button(text="🛡️ Веб-панель менеджера", url=f"{SITE_URL}/manager")
    kb.adjust(1)
    await message.answer("Ссылки на сервис Ischia Shop:", reply_markup=kb.as_markup())


# --- Выход ---
@dp.message(F.text.in_(["🚪 Выйти", "🚪 Выйти из аккаунта"]))
@dp.message(Command("logout"))
async def cmd_logout(message: types.Message, state: FSMContext):
    await state.clear()
    db.remove_user(message.chat.id)
    await message.answer(
        "👋 Вы вышли из профиля. Бот больше не присылает персональные тикеты.",
        reply_markup=get_main_keyboard(None)
    )


# ==========================================================
# Фоновый мониторинг
# ==========================================================
async def check_user_notifications(chat_id: str, urec: Dict[str, Any]) -> int:
    username = urec.get("username")
    password = urec.get("password")
    role = urec.get("role", "user")
    known_tickets = urec.setdefault("known_tickets", {})
    count = 0

    if is_manager_or_higher(role):
        # Менеджер: новые заявки
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
                        f"⚡ <b>НОВЫЙ ЗАКАЗ #{tid}!</b> [{status_label}]\n\n"
                        f"🎮 <b>Игрок:</b> <code>{t.get('playerId')}</code>\n"
                        f"💬 <b>Telegram:</b> {t.get('contact')}\n"
                        f"📦 <b>Товар:</b> {t.get('itemLabel')}\n"
                        f"💰 <b>Сумма:</b> {price}\n"
                        + (f"📝 <b>Примечание:</b> {t.get('note')}\n" if t.get("note") else "")
                    )
                    reply_markup = build_manager_ticket_inline(t)
                    try:
                        await bot.send_message(chat_id, text, reply_markup=reply_markup)
                    except Exception as e:
                        logger.error(f"Не удалось отправить менеджеру {chat_id}: {e}")
    else:
        # Игрок: изменение статуса заказа
        p_res = await api_client.get_player_tickets(username, password)
        if p_res.get("success"):
            for t in p_res.get("tickets", []):
                tid = str(t.get("id"))
                current_status = t.get("status", "open")
                prev_info = known_tickets.get(tid)

                if prev_info is None:
                    known_tickets[tid] = {"status": current_status}
                elif prev_info.get("status") != current_status:
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
                        text += "👉 <i>Менеджер ожидает оплату Kaspi.kz или отправку чека.</i>"
                    elif current_status == "completed":
                        text += "🎉 <i>Ваш заказ успешно выдан в игре! Приятной игры на FunTime!</i>"
                    elif current_status == "processing":
                        text += f"⏱ <i>Заказ взят в работу. Ваше место в очереди: #{t.get('queuePos', 1)}</i>"

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
    logger.info("Запуск фонового мониторинга...")
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
    return web.Response(text="OK: Ischia Support Bot with Realtime Role Check is active!", content_type="text/plain")


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
        await asyncio.sleep(600)
        try:
            async with ClientSession() as s:
                async with s.get(ext_url, timeout=10) as r:
                    logger.info(f"Keep-alive пинг: статус {r.status}")
        except Exception as e:
            logger.warning(f"Ошибка keep-alive: {e}")


async def main():
    logger.info("Запуск Ischia Support Bot с динамической проверкой ролей...")
    await start_web_server()
    asyncio.create_task(keep_alive_ping())
    asyncio.create_task(poll_all_users_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
