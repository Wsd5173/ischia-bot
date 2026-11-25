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
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramBadRequest
from dotenv import load_dotenv

# Загрузка настроек из .env
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
SITE_URL = os.getenv("SITE_URL", "https://ischia-shop.ischia-ft.workers.dev").rstrip("/")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "10"))

SEEN_FILE = Path(__file__).parent / "seen_tickets.json"
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


class TicketWatcher:
    def __init__(self, site_url: str, username: str, password: str):
        self.site_url = site_url
        self.username = username
        self.password = password
        self.session: Optional[ClientSession] = None
        self.seen_ids = self._load_seen_ids()
        self.is_logged_in = False

    def _load_seen_ids(self) -> set:
        if SEEN_FILE.exists():
            try:
                with open(SEEN_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return set(data)
            except Exception as e:
                logger.warning(f"Не удалось прочитать seen_tickets.json: {e}")
        return set()

    def _save_seen_ids(self):
        try:
            with open(SEEN_FILE, "w", encoding="utf-8") as f:
                json.dump(list(self.seen_ids), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения seen_tickets.json: {e}")

    async def get_session(self) -> ClientSession:
        if self.session is None or self.session.closed:
            headers = {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
            }
            self.session = ClientSession(cookie_jar=CookieJar(), headers=headers)
        return self.session

    async def login(self) -> bool:
        session = await self.get_session()
        login_url = f"{self.site_url}/api/auth/login"
        payload = {
            "username": self.username,
            "password": self.password
        }
        try:
            async with session.post(login_url, json=payload, timeout=12) as resp:
                data = await resp.json()
                if resp.status == 200 and data.get("user"):
                    user = data["user"]
                    self.is_logged_in = True
                    logger.info(f"Успешная авторизация на сайте: {user.get('username')} (роль: {user.get('role')})")
                    return True
                else:
                    self.is_logged_in = False
                    logger.error(f"Ошибка входа: {data.get('error', resp.status)}")
                    return False
        except Exception as e:
            self.is_logged_in = False
            logger.error(f"Ошибка соединения при авторизации: {e}")
            return False

    async def fetch_manager_tickets(self, retry_on_auth_fail=True) -> Optional[List[Dict[str, Any]]]:
        session = await self.get_session()
        url = f"{self.site_url}/api/manager/tickets"

        try:
            async with session.get(url, timeout=12) as resp:
                if (resp.status in (401, 403)) and retry_on_auth_fail:
                    logger.info("Сессия требует обновления, авторизуемся...")
                    if await self.login():
                        return await self.fetch_manager_tickets(retry_on_auth_fail=False)
                    return None

                if resp.status == 200:
                    data = await resp.json()
                    return data.get("tickets", [])
                else:
                    text = await resp.text()
                    logger.warning(f"Ответ API {resp.status}: {text}")
                    return None
        except Exception as e:
            logger.error(f"Ошибка при запросе тикетов менеджера: {e}")
            return None

    async def update_ticket_status(self, ticket_id: str, new_status: str) -> bool:
        session = await self.get_session()
        url = f"{self.site_url}/api/manager/tickets/{ticket_id}"
        try:
            async with session.patch(url, json={"status": new_status}, timeout=10) as resp:
                return resp.status == 200
        except Exception as e:
            logger.error(f"Ошибка обновления статуса {ticket_id}: {e}")
            return False

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


def format_ticket_message(t: Dict[str, Any]) -> str:
    ticket_id = t.get("id", "N/A")
    account_user = t.get("accountUsername") or "Гость"
    player_id = t.get("playerId", "Не указан")
    region = t.get("region", "Обычная Анархия")
    contact = t.get("contact", "Не указан")
    item_label = t.get("itemLabel") or "Товар FunTime"
    kzt_final = t.get("kztFinal", 0)
    note = t.get("note")
    status = t.get("status", "open")
    status_label = STATUS_MAP.get(status, status)

    price_str = f"<b>{kzt_final} ₸</b>" if not t.get("isNegotiated") else "<b>В ЛС (Договорная)</b>"

    msg = [
        f"⚡ <b>Тикет {ticket_id}</b> [{status_label}]\n",
        f"🎮 <b>Игрок (FunTime):</b> <code>{player_id}</code> ({region})",
        f"👤 <b>Аккаунт на сайте:</b> <code>{account_user}</code>",
        f"💬 <b>Telegram покупателя:</b> {contact}",
        f"📦 <b>Заказ:</b> {item_label}",
        f"💰 <b>К оплате:</b> {price_str}",
    ]

    if note:
        msg.append(f"📝 <b>Примечание:</b> <i>{note}</i>")

    return "\n".join(msg)


def build_ticket_keyboard(ticket: Dict[str, Any], site_url: str):
    builder = InlineKeyboardBuilder()
    contact = str(ticket.get("contact", "")).strip()

    # Ссылка на диалог с покупателем
    clean_username = contact.lstrip("@").split("/")[0] if contact else ""
    if clean_username and not clean_username.startswith("+"):
        builder.button(
            text="💬 Написать покупателю в TG",
            url=f"https://t.me/{clean_username}"
        )

    # Быстрый переход в панель менеджера
    builder.button(
        text="🛡️ Панель менеджера на сайте",
        url=f"{site_url}/manager"
    )

    builder.adjust(1)
    return builder.as_markup()


# Инициализация бота
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
watcher = TicketWatcher(SITE_URL, ADMIN_USERNAME, ADMIN_PASSWORD)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    global ADMIN_CHAT_ID
    ADMIN_CHAT_ID = str(message.chat.id)

    status_str = "🟢 Подключён" if watcher.is_logged_in else "🟡 Ожидает входа"

    await message.answer(
        "👋 <b>Добро пожаловать в Ischia Support Bot!</b>\n\n"
        f"🛡️ <b>Менеджер:</b> <code>{ADMIN_USERNAME}</code>\n"
        f"🌐 <b>Сайт:</b> <code>{SITE_URL}</code>\n"
        f"Статус связи с сайтом: {status_str}\n\n"
        "🔔 <b>Уведомления активированы!</b>\n"
        "Как только покупатель создаст новый тикет на сайте, он мгновенно появится в этом чате.\n\n"
        "<b>Доступные команды:</b>\n"
        "• /tickets — Список активных тикетов\n"
        "• /check — Проверить новые заявки прямо сейчас\n"
        "• /manager — Ссылка на веб-панель менеджера"
    )


@dp.message(Command("manager"))
async def cmd_manager(message: types.Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="🛡️ Открыть панель менеджера", url=f"{SITE_URL}/manager")
    await message.answer(
        "Панель управления очередью и заказами на сайте:",
        reply_markup=kb.as_markup()
    )


@dp.message(Command("tickets"))
async def cmd_tickets(message: types.Message):
    await message.answer("🔄 Запрашиваю список тикетов с сайта...")
    tickets = await watcher.fetch_manager_tickets()
    if tickets is None:
        await message.answer("❌ Ошибка соединения с сайтом. Проверьте статус панели.")
        return

    # Фильтруем открытые/активные
    active = [t for t in tickets if t.get("status") not in ("completed", "cancelled")]
    if not active:
        await message.answer("📭 Активных тикетов сейчас нет.")
        return

    for t in active[:5]:
        text = format_ticket_message(t)
        kb = build_ticket_keyboard(t, SITE_URL)
        await message.answer(text, reply_markup=kb)


@dp.message(Command("check"))
async def cmd_check(message: types.Message):
    await message.answer("🔍 Проверяю сайт на наличие новых тикетов...")
    new_count = await check_new_tickets()
    if new_count == 0:
        await message.answer("✅ Новых тикетов нет.")


async def check_new_tickets() -> int:
    tickets = await watcher.fetch_manager_tickets()
    if tickets is None:
        return 0

    new_count = 0
    # Проверяем тикеты снизу вверх (от старых к новым)
    for t in reversed(tickets):
        t_id = str(t.get("id"))
        if not t_id or t_id in watcher.seen_ids:
            continue

        watcher.seen_ids.add(t_id)
        new_count += 1

        text = format_ticket_message(t)
        kb = build_ticket_keyboard(t, SITE_URL)

        if ADMIN_CHAT_ID:
            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=text,
                    reply_markup=kb
                )
            except TelegramBadRequest as e:
                if "chat not found" in str(e).lower():
                    logger.warning(f"Чат {ADMIN_CHAT_ID} не найден. Пользователю нужно нажать /start в @IschiSupBot")
                else:
                    logger.error(f"Ошибка отправки в Telegram: {e}")
            except Exception as e:
                logger.error(f"Не удалось отправить уведомление о тикете {t_id}: {e}")

    if new_count > 0:
        watcher._save_seen_ids()
        logger.info(f"Обработано {new_count} новых тикетов.")

    return new_count


async def poll_tickets_loop():
    logger.info("Запуск цикла мониторинга тикетов менеджера...")
    await watcher.login()

    # Запоминаем текущие тикеты при старте, чтобы не спамить старыми
    initial_tickets = await watcher.fetch_manager_tickets()
    if initial_tickets and not watcher.seen_ids:
        for t in initial_tickets:
            t_id = str(t.get("id"))
            if t_id:
                watcher.seen_ids.add(t_id)
        watcher._save_seen_ids()
        logger.info(f"Инициализировано {len(watcher.seen_ids)} существующих тикетов.")

    while True:
        try:
            await check_new_tickets()
        except Exception as e:
            logger.error(f"Ошибка в цикле мониторинга: {e}")
        await asyncio.sleep(CHECK_INTERVAL)


async def handle_health(request):
    return web.Response(text="OK: Ischia Support Bot is alive and running!", content_type="text/plain")

async def start_web_server():
    port = int(os.getenv("PORT", "8080"))
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Встроенный веб-сервер запущен на порту {port} (для Render.com)")

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
                    logger.info(f"Keep-alive пинг отправлен: статус {r.status}")
        except Exception as e:
            logger.warning(f"Ошибка keep-alive пинга: {e}")

async def main():
    logger.info("Запуск Ischia Support Bot...")
    await start_web_server()
    asyncio.create_task(keep_alive_ping())
    asyncio.create_task(poll_tickets_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
