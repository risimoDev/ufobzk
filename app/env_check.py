"""Валидация переменных окружения при старте приложения."""

import logging
import os

logger = logging.getLogger(__name__)

REQUIRED_VARS = [
    "SECRET_KEY",
    "DOMAIN",
    "NL_SERVER_IP",
    "RU_SERVER_IP",
]

RECOMMENDED_VARS = [
    "ADMIN_PASSWORD",
    "SUPERADMIN_TELEGRAM_ID",
    "REALITY_PRIVATE_KEY",
    "REALITY_PUBLIC_KEY",
    "XRAY_CONTAINER_NAME",
]

SENSITIVE_DEFAULTS = {
    "SECRET_KEY": ("change-me", "insecure-default"),
    "ADMIN_PASSWORD": ("admin", "password", "1234", "changeme"),
}


def validate_env() -> list[str]:
    """Проверяет переменные окружения. Возвращает список предупреждений."""
    warnings: list[str] = []

    for var in REQUIRED_VARS:
        val = os.getenv(var, "").strip()
        if not val:
            warnings.append(f"Обязательная переменная {var} не задана")

    for var in RECOMMENDED_VARS:
        val = os.getenv(var, "").strip()
        if not val:
            warnings.append(f"Рекомендуемая переменная {var} не задана")

    for var, bad_vals in SENSITIVE_DEFAULTS.items():
        val = os.getenv(var, "").strip()
        if val and val.lower() in bad_vals:
            warnings.append(f"{var} содержит небезопасное значение по умолчанию — замените его")

    # Проверка формата домена
    domain = os.getenv("DOMAIN", "").strip()
    if domain and not domain.replace("-", "").replace(".", "").isalnum():
        warnings.append(f"DOMAIN имеет подозрительный формат: {domain}")

    return warnings


def check_env_on_startup() -> None:
    """Логирует предупреждения при старте. Не бросает исключения."""
    warnings = validate_env()
    for w in warnings:
        logger.warning("ENV: %s", w)
    if not warnings:
        logger.info("ENV: все переменные в порядке")
