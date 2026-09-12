"""Telegram-бот — авторизация, подписки, статус и привязка аккаунтов."""

import asyncio
import logging
import os
from datetime import datetime, timezone
from urllib.parse import quote

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import Command, CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.auth import (
    create_magic_token,
    generate_code,
    load_tg_link_token,
    mark_invite_used,
    use_invite_key,
    verify_tg_link_code,
)
from app.dependencies import _format_bytes
from app.models import SUPERADMIN_TELEGRAM_ID, SessionLocal, User, VPNKey, _gen_uuid
from app.xray import generate_uuid, sync_and_reload

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "http://localhost:8000")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME", "yourbotname")

router = Router()
logger = logging.getLogger(__name__)


def _run_bg_sync() -> None:
    """Безопасная фоновая синхронизация с собственной сессией БД."""
    db = SessionLocal()
    try:
        sync_and_reload(db)
    except Exception as e:
        logger.error("Ошибка синхронизации Xray из бота: %s", e)
    finally:
        db.close()


def get_main_menu_keyboard(user: User) -> InlineKeyboardMarkup:
    """Главное интерактивное меню пользователя."""
    magic_token = create_magic_token(user.id)
    login_url = f"{WEBAPP_URL}/login/magic?token={magic_token}"
    buttons = [
        [InlineKeyboardButton(text="🌐 Войти в личный кабинет", url=login_url)],
        [
            InlineKeyboardButton(text="📋 Моя подписка", callback_data="btn_sub"),
            InlineKeyboardButton(text="📊 Мой статус", callback_data="btn_status"),
        ],
        [
            InlineKeyboardButton(text="📱 Инструкции", callback_data="btn_guides"),
            InlineKeyboardButton(text="🔑 Одноразовый код", callback_data="btn_code"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_sub_apps_keyboard(sub_url: str) -> InlineKeyboardMarkup:
    """Клавиатура быстрого импорта подписки в популярные клиенты."""
    enc_url = quote(sub_url, safe="")
    buttons = [
        [
            InlineKeyboardButton(text="📥 Karing", url=f"karing://install-sub?url={enc_url}"),
            InlineKeyboardButton(text="📥 Hiddify", url=f"hiddify://install-sub?url={enc_url}"),
        ],
        [
            InlineKeyboardButton(text="📥 Streisand", url=f"streisand://import/{enc_url}"),
            InlineKeyboardButton(text="📥 Sing-box", url=f"sing-box://import-remote-profile?url={enc_url}#Profile"),
        ],
        [
            InlineKeyboardButton(text="📥 Clash / Mihomo", url=f"clash://install-config?url={enc_url}"),
            InlineKeyboardButton(text="📥 V2RayNG", url=f"v2rayng://install-config?url={enc_url}"),
        ],
        [
            InlineKeyboardButton(text="« Назад в меню", callback_data="btn_menu"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_guides_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура выбора инструкций по платформе."""
    buttons = [
        [
            InlineKeyboardButton(text="🍏 iOS", callback_data="guide_ios"),
            InlineKeyboardButton(text="🤖 Android", callback_data="guide_android"),
        ],
        [
            InlineKeyboardButton(text="🪟 Windows", callback_data="guide_windows"),
            InlineKeyboardButton(text="🍎 macOS", callback_data="guide_macos"),
        ],
        [
            InlineKeyboardButton(text="« Назад в меню", callback_data="btn_menu"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(CommandStart(deep_link=True))
async def cmd_start_deep_link(message: types.Message) -> None:
    """Обработка /start с аргументом (инвайт-ключ или привязка Telegram)."""
    if not message.from_user:
        return

    telegram_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    param = args[1].strip() if len(args) > 1 else ""

    db = SessionLocal()
    try:
        # ── 1. Привязка Telegram по deep-link (start=link_<token>) ──
        if param.startswith("link_"):
            token = param[5:]
            user_id = load_tg_link_token(token)
            if not user_id:
                await message.answer(
                    "❌ Ссылка для привязки устарела или недействительна.\n"
                    "Сгенерируйте новую ссылку в личном кабинете на сайте.",
                )
                return

            user = db.query(User).filter(User.id == user_id, User.is_active == True).first()  # noqa: E712
            if not user:
                await message.answer("❌ Пользователь не найден или заблокирован.")
                return

            # Проверяем, не занят ли уже этот telegram_id другим аккаунтом
            conflict = db.query(User).filter(User.telegram_id == telegram_id, User.id != user.id).first()
            if conflict:
                await message.answer(
                    f"⚠️ Этот Telegram-аккаунт уже привязан к пользователю <b>{conflict.display_name or conflict.username}</b>.\n"
                    "Если хотите изменить привязку, обратитесь к администратору.",
                    parse_mode="HTML",
                )
                return

            user.telegram_id = telegram_id
            user.telegram_username = message.from_user.username
            if not user.display_name:
                user.display_name = message.from_user.full_name
            db.commit()

            await message.answer(
                f"✅ <b>Telegram успешно привязан</b> к аккаунту <code>{user.display_name or user.username}</code>!\n\n"
                "Теперь вы можете управлять доступом и входить в личный кабинет прямо отсюда.",
                parse_mode="HTML",
                reply_markup=get_main_menu_keyboard(user),
            )
            return

        # ── 2. Проверка зарегистрированного пользователя ──
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user:
            if not user.is_active:
                await message.answer("🚫 Доступ к учетной записи приостановлен.")
                return
            await message.answer(
                f"Здравствуйте, <b>{user.display_name or user.username or 'пользователь'}</b>.\n"
                "Выберите нужное действие в меню ниже:",
                parse_mode="HTML",
                reply_markup=get_main_menu_keyboard(user),
            )
            return

        # ── 3. Регистрация по инвайт-ключу ──
        if not param:
            await message.answer(
                "Для регистрации необходим ключ доступа.\n"
                "Отправьте команду: <code>/start ВАШ_КЛЮЧ</code>\n"
                "Либо привяжите существующий аккаунт: <code>/link КОД</code>",
                parse_mode="HTML",
            )
            return

        invite = use_invite_key(db, param)
        if not invite:
            await message.answer("❌ Недействительный или уже использованный ключ доступа.")
            return

        is_admin = telegram_id == SUPERADMIN_TELEGRAM_ID
        new_user = User(
            telegram_id=telegram_id,
            telegram_username=message.from_user.username,
            display_name=message.from_user.full_name,
            is_admin=is_admin,
            is_active=True,
            sub_token=_gen_uuid(),
        )
        db.add(new_user)
        db.flush()
        mark_invite_used(db, invite, new_user.id)

        vpn_key = VPNKey(
            user_id=new_user.id,
            name="default",
            uuid=generate_uuid(),
            protocol="vless",
        )
        db.add(vpn_key)
        db.commit()

        # Безопасная фоновая синхронизация ядра
        try:
            await asyncio.to_thread(_run_bg_sync)
        except Exception as e:
            logger.error("Ошибка фоновой синхронизации: %s", e)

        sub_url = f"{WEBAPP_URL}/sub/{new_user.sub_token}"
        await message.answer(
            "✅ <b>Регистрация успешно завершена!</b>\n\n"
            f"Ваша ссылка подписки:\n<code>{sub_url}</code>\n\n"
            "Нажмите кнопку ниже для импорта в приложение или входа в кабинет:",
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard(new_user),
        )
    finally:
        db.close()


@router.message(CommandStart(deep_link=False))
async def cmd_start_simple(message: types.Message) -> None:
    """Команда /start без параметров."""
    if not message.from_user:
        return

    telegram_id = message.from_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id).first()
        if user:
            if not user.is_active:
                await message.answer("🚫 Доступ к учетной записи приостановлен.")
                return
            await message.answer(
                f"Здравствуйте, <b>{user.display_name or user.username or 'пользователь'}</b>.\n"
                "Выберите нужное действие в меню ниже:",
                parse_mode="HTML",
                reply_markup=get_main_menu_keyboard(user),
            )
        else:
            await message.answer(
                "Для регистрации необходим ключ доступа.\n\n"
                "• Отправьте: <code>/start ВАШ_КЛЮЧ</code> для регистрации\n"
                "• Или: <code>/link КОД</code> для привязки существующего аккаунта с сайта",
                parse_mode="HTML",
            )
    finally:
        db.close()


@router.message(Command("link"))
async def cmd_link_code(message: types.Message) -> None:
    """Привязка Telegram через 6-значный код (/link 123456)."""
    if not message.from_user:
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await message.answer(
            "Укажите 6-значный код привязки из личного кабинета:\n"
            "Пример: <code>/link 123456</code>",
            parse_mode="HTML",
        )
        return

    code = parts[1].strip()
    user_id = verify_tg_link_code(code)
    if not user_id:
        await message.answer(
            "❌ Неверный или устаревший код привязки.\n"
            "Получите свежий код в личном кабинете на сайте.",
        )
        return

    telegram_id = message.from_user.id
    db = SessionLocal()
    try:
        conflict = db.query(User).filter(User.telegram_id == telegram_id, User.id != user_id).first()
        if conflict:
            await message.answer(
                f"⚠️ Этот Telegram-аккаунт уже привязан к <b>{conflict.display_name or conflict.username}</b>.",
                parse_mode="HTML",
            )
            return

        user = db.query(User).filter(User.id == user_id, User.is_active == True).first()  # noqa: E712
        if not user:
            await message.answer("❌ Аккаунт не найден или заблокирован.")
            return

        user.telegram_id = telegram_id
        user.telegram_username = message.from_user.username
        if not user.display_name:
            user.display_name = message.from_user.full_name
        db.commit()

        await message.answer(
            f"✅ <b>Telegram успешно привязан!</b>\n"
            f"Аккаунт: <code>{user.display_name or user.username}</code>",
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard(user),
        )
    finally:
        db.close()


# ── Callback-обработчики интерактивного меню ──


@router.callback_query(F.data == "btn_menu")
async def cb_menu(callback: types.CallbackQuery) -> None:
    telegram_id = callback.from_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id, User.is_active == True).first()  # noqa: E712
        if not user:
            await callback.answer("Учетная запись не найдена.", show_alert=True)
            return
        await callback.message.edit_text(
            f"Здравствуйте, <b>{user.display_name or user.username or 'пользователь'}</b>.\n"
            "Выберите нужное действие в меню ниже:",
            parse_mode="HTML",
            reply_markup=get_main_menu_keyboard(user),
        )
        await callback.answer()
    finally:
        db.close()


@router.callback_query(F.data == "btn_code")
async def cb_code(callback: types.CallbackQuery) -> None:
    telegram_id = callback.from_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id, User.is_active == True).first()  # noqa: E712
        if not user:
            await callback.answer("Учетная запись не найдена.", show_alert=True)
            return
        code = generate_code(telegram_id)
        text = (
            f"🔑 <b>Ваш одноразовый код для входа:</b>\n\n"
            f"<code>{code}</code>\n\n"
            f"Введите его на странице: {WEBAPP_URL}/login\n"
            "Код действует 5 минут."
        )
        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="« Назад в меню", callback_data="btn_menu")]]
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_kb)
        await callback.answer()
    finally:
        db.close()


@router.callback_query(F.data == "btn_sub")
async def cb_subscription(callback: types.CallbackQuery) -> None:
    telegram_id = callback.from_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id, User.is_active == True).first()  # noqa: E712
        if not user:
            await callback.answer("Учетная запись не найдена.", show_alert=True)
            return

        if not user.sub_token:
            user.sub_token = _gen_uuid()
            db.commit()

        sub_url = f"{WEBAPP_URL}/sub/{user.sub_token}"
        text = (
            "📋 <b>Ваша ссылка подписки:</b>\n\n"
            f"<code>{sub_url}</code>\n\n"
            "Нажмите на кнопку вашего приложения ниже для импорта в 1 клик:"
        )
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_sub_apps_keyboard(sub_url))
        await callback.answer()
    finally:
        db.close()


@router.callback_query(F.data == "btn_status")
async def cb_status(callback: types.CallbackQuery) -> None:
    telegram_id = callback.from_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id, User.is_active == True).first()  # noqa: E712
        if not user:
            await callback.answer("Учетная запись не найдена.", show_alert=True)
            return

        now = datetime.now(timezone.utc)
        lines = [f"📊 <b>Статус учетной записи:</b>\n"]
        lines.append(f"Пользователь: <b>{user.display_name or user.username or 'Пользователь'}</b>")

        active_keys = [k for k in user.vpn_keys if k.status == "active"]
        lines.append(f"Активных ключей: <b>{len(active_keys)}</b> / {len(user.vpn_keys)}")

        total_used = sum(k.data_used or 0 for k in user.vpn_keys)
        has_limit = any(k.data_limit and k.data_limit > 0 for k in user.vpn_keys)
        total_limit = sum(k.data_limit for k in user.vpn_keys if k.data_limit)

        if has_limit and total_limit > 0:
            lines.append(f"Трафик: <b>{_format_bytes(total_used)}</b> из {_format_bytes(total_limit)}")
        else:
            lines.append(f"Трафик: <b>{_format_bytes(total_used)}</b> (безлимит)")

        # Срок действия
        expire_dates = [k.expire_at for k in user.vpn_keys if k.expire_at]
        if expire_dates:
            min_exp = min(expire_dates)
            min_exp_aware = min_exp if min_exp.tzinfo else min_exp.replace(tzinfo=timezone.utc)
            days = max((min_exp_aware - now).days, 0)
            lines.append(f"Срок действия: до <b>{min_exp_aware.strftime('%d.%m.%Y')}</b> (осталось {days} дн.)")
        else:
            lines.append("Срок действия: <b>бессрочно</b>")

        back_kb = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="« Назад в меню", callback_data="btn_menu")]]
        )
        await callback.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=back_kb)
        await callback.answer()
    finally:
        db.close()


@router.callback_query(F.data == "btn_guides")
async def cb_guides(callback: types.CallbackQuery) -> None:
    text = "📱 <b>Выберите вашу платформу для просмотра инструкции:</b>"
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_guides_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("guide_"))
async def cb_guide_platform(callback: types.CallbackQuery) -> None:
    platform = callback.data.split("_")[1]
    guides_map = {
        "ios": (
            "🍏 <b>Инструкция для iOS (iPhone / iPad)</b>\n\n"
            "1. Установите <b>Karing</b> или <b>Hiddify</b> или <b>Streisand</b> из App Store.\n"
            "2. В боте нажмите <b>«Моя подписка»</b>.\n"
            "3. Нажмите кнопку нужного приложения — профиль импортируется автоматически.\n"
            "4. Включите переключатель подключения в приложении.\n\n"
            "💡 <i>Совет: Для стабильной работы Instagram и YouTube в настройках приложения выберите режим маршрутизации «Проксировать всё» (Global).</i>"
        ),
        "android": (
            "🤖 <b>Инструкция для Android</b>\n\n"
            "1. Установите <b>Karing</b>, <b>Hiddify</b> или <b>v2rayNG</b> из Google Play / GitHub.\n"
            "2. В боте нажмите <b>«Моя подписка»</b>.\n"
            "3. Нажмите кнопку установленного приложения для импорта.\n"
            "4. Выберите профиль и нажмите кнопку старта.\n\n"
            "💡 <i>Совет: В настройках маршрутизации выберите «Проксировать всё» (Global / Bypass disabled) для максимальной скорости видео.</i>"
        ),
        "windows": (
            "🪟 <b>Инструкция для Windows</b>\n\n"
            "1. Скачайте <b>Karing</b> или <b>Hiddify</b> или <b>v2rayN</b>.\n"
            "2. Скопируйте ссылку подписки из раздела «Моя подписка».\n"
            "3. В приложении нажмите «Новый профиль» → «Добавить по ссылке».\n"
            "4. Подключитесь к любому доступному узлу."
        ),
        "macos": (
            "🍎 <b>Инструкция для macOS</b>\n\n"
            "1. Установите <b>Karing</b> или <b>Hiddify</b> или <b>Sing-box</b>.\n"
            "2. Импортируйте ссылку подписки через раздел «Моя подписка».\n"
            "3. Разрешите создание системной конфигурации и подключитесь."
        ),
    }
    content = guides_map.get(platform, "Инструкция недоступна.")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="« К выбору платформы", callback_data="btn_guides")],
            [InlineKeyboardButton(text="« В главное меню", callback_data="btn_menu")],
        ]
    )
    await callback.message.edit_text(content, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.message(F.text)
async def fallback_text_handler(message: types.Message) -> None:
    """Обработчик любого текста."""
    if not message.from_user:
        return

    telegram_id = message.from_user.id
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.telegram_id == telegram_id, User.is_active == True).first()  # noqa: E712
        if user:
            await message.answer(
                "Используйте меню ниже для навигации:",
                reply_markup=get_main_menu_keyboard(user),
            )
        else:
            await message.answer(
                "Команда не распознана.\n\n"
                "• Отправьте <code>/start КЛЮЧ</code> для регистрации\n"
                "• Или <code>/link КОД</code> для привязки аккаунта",
                parse_mode="HTML",
            )
    finally:
        db.close()


async def start_bot() -> None:
    if not BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN не задан — бот не запущен.")
        return

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Telegram-бот запущен.")
    await dp.start_polling(bot)
