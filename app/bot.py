"""Telegram-бот (aiogram 3) — регистрация по инвайт-ключу, выдача кодов для входа."""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import CommandStart

from app.auth import generate_code, mark_invite_used, use_invite_key
from app.marzban import MarzbanError, marzban
from app.models import SUPERADMIN_TELEGRAM_ID, SessionLocal, User

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "yourbotname")

router = Router()
logger = logging.getLogger(__name__)


def _send_login_message(code: str) -> str:
    return (
        "🔑 Ваш одноразовый код для входа:\n\n"
        f"<code>{code}</code>\n\n"
        f"Введите его на сайте: {WEBAPP_URL}/login\n"
        "Код действует 5 минут."
    )


@router.message(CommandStart(deep_link=True))
async def cmd_start_with_key(message: types.Message) -> None:
    """/start <key> — регистрация по инвайт-ключу или вход для зарегистрированных."""
    if not message.from_user:
        return

    telegram_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    key = args[1].strip() if len(args) > 1 else ""

    db = SessionLocal()
    try:
        # Уже зарегистрирован → просто отправляем код
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user:
            if not user.is_active:
                await message.answer("🚫 Ваш аккаунт заблокирован.", parse_mode="HTML")
                return
            code = generate_code(telegram_id)
            await message.answer(_send_login_message(code), parse_mode="HTML")
            return

        # Не зарегистрирован — нужен валидный ключ
        if not key:
            await message.answer(
                "⛔ Для регистрации необходим инвайт-ключ.\n"
                "Отправьте: <code>/start ВАШ_КЛЮЧ</code>",
                parse_mode="HTML",
            )
            return

        invite = use_invite_key(db, key)
        if not invite:
            await message.answer("❌ Недействительный или уже использованный ключ.", parse_mode="HTML")
            return

        # Регистрация
        is_admin = telegram_id == SUPERADMIN_TELEGRAM_ID
        new_user = User(
            telegram_id=telegram_id,
            telegram_username=message.from_user.username,
            display_name=message.from_user.full_name,
            is_admin=is_admin,
            is_active=True,
        )
        db.add(new_user)
        db.flush()
        mark_invite_used(db, invite, new_user.id)

        # Автоматическое создание VPN-аккаунта
        vpn_username = f"vpn_{telegram_id}"
        try:
            await marzban.create_user(username=vpn_username)
            new_user.marzban_username = vpn_username
            db.commit()
            logger.info("VPN-аккаунт %s создан для tg=%d", vpn_username, telegram_id)
        except MarzbanError as e:
            db.commit()  # user сохраняем даже если VPN не создался
            logger.error("Не удалось создать VPN для tg=%d: %s", telegram_id, e)

        code = generate_code(telegram_id)
        await message.answer(
            "✅ Регистрация прошла успешно!\n\n" + _send_login_message(code),
            parse_mode="HTML",
        )
    finally:
        db.close()


@router.message(CommandStart(deep_link=False))
async def cmd_start_no_key(message: types.Message) -> None:
    """/start без ключа — вход для зарегистрированных, подсказка для новых."""
    if not message.from_user:
        return

    telegram_id = message.from_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user:
            if not user.is_active:
                await message.answer("🚫 Ваш аккаунт заблокирован.", parse_mode="HTML")
                return
            code = generate_code(telegram_id)
            await message.answer(_send_login_message(code), parse_mode="HTML")
        else:
            await message.answer(
                "⛔ Для регистрации необходим инвайт-ключ.\n"
                "Отправьте: <code>/start ВАШ_КЛЮЧ</code>",
                parse_mode="HTML",
            )
    finally:
        db.close()


@router.message(F.text)
async def fallback_handler(message: types.Message) -> None:
    """Ответ на любое текстовое сообщение, кроме /start."""
    await message.answer(
        "👁️ <b>НИИ АЯ — Бот авторизации</b>\n\n"
        "Доступные команды:\n"
        "• /start — получить код для входа\n"
        "• /start <code>КЛЮЧ</code> — регистрация по инвайт-ключу\n\n"
        f"Сайт: {WEBAPP_URL}",
        parse_mode="HTML",
    )


async def start_bot() -> None:
    """Запуск бота (вызывается из main.py через lifespan)."""
    if not BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN не задан — бот не запущен.")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Telegram-бот запущен.")
    await dp.start_polling(bot)
