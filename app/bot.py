"""Telegram-бот (aiogram 3) — регистрация по инвайт-ключу, выдача кодов для входа."""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import CommandStart

from app.auth import generate_code, mark_invite_used, use_invite_key
from app.models import SUPERADMIN_TELEGRAM_ID, SessionLocal, User, VPNKey
from app.xray import generate_uuid, sync_and_reload

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
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user:
            if not user.is_active:
                await message.answer("🚫 Ваш аккаунт заблокирован.", parse_mode="HTML")
                return
            code = generate_code(telegram_id)
            await message.answer(_send_login_message(code), parse_mode="HTML")
            return

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

        # Создание VPN-ключа
        vpn_key = VPNKey(
            user_id=new_user.id,
            name="default",
            uuid=generate_uuid(),
            protocol="vless",
        )
        db.add(vpn_key)
        db.commit()

        try:
            sync_and_reload(db)
        except Exception as e:
            logger.error("Не удалось синхронизировать Xray для tg=%d: %s", telegram_id, e)

        code = generate_code(telegram_id)
        await message.answer(
            "✅ Регистрация прошла успешно!\n\n" + _send_login_message(code),
            parse_mode="HTML",
        )
    finally:
        db.close()


@router.message(CommandStart(deep_link=False))
async def cmd_start_no_key(message: types.Message) -> None:
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
    await message.answer(
        "🛡️ <b>Каскадный VPN — Бот авторизации</b>\n\n"
        "Доступные команды:\n"
        "• /start — получить код для входа\n"
        "• /start <code>КЛЮЧ</code> — регистрация по инвайт-ключу\n\n"
        f"Сайт: {WEBAPP_URL}",
        parse_mode="HTML",
    )


async def start_bot() -> None:
    if not BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN не задан — бот не запущен.")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Telegram-бот запущен.")
    await dp.start_polling(bot)
