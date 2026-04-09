"""Полная обёртка для Marzban REST API с кэшем токена и авто-реаутентификацией."""

import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MARZBAN_BASE_URL = os.getenv("MARZBAN_BASE_URL", "http://marzban:8000")
MARZBAN_ADMIN_USER = os.getenv("MARZBAN_ADMIN_USER", "admin")
MARZBAN_ADMIN_PASS = os.getenv("MARZBAN_ADMIN_PASS", "admin")

GB = 1_073_741_824  # байт в гигабайте


# ── Исключения ──


class MarzbanError(Exception):
    """Базовая ошибка Marzban API."""

    def __init__(self, message: str, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


class MarzbanAuthError(MarzbanError):
    """Ошибка аутентификации к Marzban."""


class MarzbanNotFoundError(MarzbanError):
    """Пользователь не найден."""


class MarzbanConflictError(MarzbanError):
    """Конфликт (пользователь уже существует)."""


# ── API-клиент ──


class MarzbanAPI:
    """Асинхронный клиент Marzban с кэшем JWT-токена и авто-retry на 401."""

    TOKEN_LIFETIME = 3500  # ~58 минут (токен Marzban живёт 1 час)

    def __init__(
        self,
        base_url: str = MARZBAN_BASE_URL,
        admin_user: str = MARZBAN_ADMIN_USER,
        admin_pass: str = MARZBAN_ADMIN_PASS,
    ):
        self.base_url = base_url.rstrip("/")
        self._admin_user = admin_user
        self._admin_pass = admin_pass
        self._token: str | None = None
        self._token_expires: float = 0.0

    # ── Аутентификация ──

    async def authenticate(self) -> str:
        """Получить JWT-токен от Marzban (кэшируется в памяти)."""
        if self._token and time.time() < self._token_expires:
            return self._token

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{self.base_url}/api/admin/token",
                    data={
                        "username": self._admin_user,
                        "password": self._admin_pass,
                    },
                )
        except httpx.HTTPError as e:
            raise MarzbanError(f"Не удалось подключиться к Marzban: {e}") from e
        if resp.status_code == 401 or resp.status_code == 422:
            raise MarzbanAuthError(
                "Неверные учётные данные Marzban", resp.status_code
            )
        if resp.status_code != 200:
            raise MarzbanError(
                f"Ошибка аутентификации: {resp.status_code} {resp.text}",
                resp.status_code,
            )

        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + self.TOKEN_LIFETIME
        logger.debug("Marzban: токен получен, истекает через %ds", self.TOKEN_LIFETIME)
        return self._token

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expires = 0.0

    # ── Базовый HTTP-запрос с авто-retry на 401 ──

    async def _request(
        self,
        method: str,
        path: str,
        *,
        _retry: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        token = await self.authenticate()
        headers = {"Authorization": f"Bearer {token}"}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    **kwargs,
                )
        except httpx.HTTPError as e:
            raise MarzbanError(f"Ошибка соединения с Marzban: {e}") from e

        if resp.status_code == 401 and _retry:
            logger.info("Marzban: 401, переаутентификация...")
            self._invalidate_token()
            return await self._request(method, path, _retry=False, **kwargs)

        return resp

    async def _json_request(
        self, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Запрос, возвращающий JSON. Бросает MarzbanError при ошибке."""
        resp = await self._request(method, path, **kwargs)

        if resp.status_code == 404:
            raise MarzbanNotFoundError(
                f"Не найдено: {path}", resp.status_code
            )
        if resp.status_code == 409:
            raise MarzbanConflictError(
                f"Конфликт: {path}", resp.status_code
            )
        if resp.status_code == 401:
            raise MarzbanAuthError(
                "Аутентификация провалилась", resp.status_code
            )
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise MarzbanError(
                f"Marzban {resp.status_code}: {detail}", resp.status_code
            )

        return resp.json() if resp.content else {}

    # ── Пользователи ──

    async def get_user(self, username: str) -> dict[str, Any]:
        """Получить данные пользователя: traffic, limit, expiry, status, links."""
        return await self._json_request("GET", f"/api/user/{username}")

    async def create_user(
        self,
        username: str,
        data_limit_gb: float = 0,
        expire_days: int = 0,
        protocol: str = "vless",
    ) -> dict[str, Any]:
        """Создать пользователя в Marzban.

        Args:
            username: имя VPN-аккаунта
            data_limit_gb: лимит трафика в ГБ (0 = безлимит)
            expire_days: через сколько дней истекает (0 = бессрочно)
            protocol: протокол (vless, vmess, trojan, shadowsocks)
        """
        inbounds = await self.get_inbounds()

        proxies: dict[str, dict] = {}
        inbounds_map: dict[str, list[str]] = {}

        if protocol in inbounds:
            proxies[protocol] = {}
            inbounds_map[protocol] = [ib["tag"] for ib in inbounds[protocol]]
        else:
            # Если запрошенный протокол недоступен — подключаем все
            for proto, ib_list in inbounds.items():
                proxies[proto] = {}
                inbounds_map[proto] = [ib["tag"] for ib in ib_list]

        payload: dict[str, Any] = {
            "username": username,
            "proxies": proxies,
            "inbounds": inbounds_map,
            "status": "active",
        }
        if data_limit_gb > 0:
            payload["data_limit"] = int(data_limit_gb * GB)
        if expire_days > 0:
            expire_ts = datetime.utcnow() + timedelta(days=expire_days)
            payload["expire"] = int(expire_ts.timestamp())

        return await self._json_request("POST", "/api/user", json=payload)

    async def update_user(self, username: str, **kwargs: Any) -> dict[str, Any]:
        """Обновить параметры пользователя.

        Kwargs:
            data_limit_gb (float): новый лимит (0 = безлимит)
            expire_days (int): продлить на N дней от сейчас (0 = бессрочно)
            status (str): 'active', 'disabled', 'limited', 'expired'
        """
        payload: dict[str, Any] = {}

        if "data_limit_gb" in kwargs:
            gb = kwargs["data_limit_gb"]
            payload["data_limit"] = int(gb * GB) if gb > 0 else 0

        if "expire_days" in kwargs:
            days = kwargs["expire_days"]
            if days > 0:
                expire_ts = datetime.utcnow() + timedelta(days=days)
                payload["expire"] = int(expire_ts.timestamp())
            else:
                payload["expire"] = 0

        if "status" in kwargs:
            payload["status"] = kwargs["status"]

        return await self._json_request(
            "PUT", f"/api/user/{username}", json=payload
        )

    async def delete_user(self, username: str) -> dict[str, Any]:
        """Удалить пользователя из Marzban."""
        return await self._json_request("DELETE", f"/api/user/{username}")

    async def reset_user_traffic(self, username: str) -> dict[str, Any]:
        """Сбросить счётчик трафика пользователя."""
        return await self._json_request(
            "POST", f"/api/user/{username}/reset"
        )

    async def get_all_users(
        self, offset: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Получить список всех пользователей (для админ-панели)."""
        result = await self._json_request(
            "GET", "/api/users", params={"offset": offset, "limit": limit}
        )
        return result.get("users", [])

    # ── Подписки / ссылки ──

    async def get_subscription_links(self, username: str) -> dict[str, Any]:
        """Получить vless://, vmess:// ссылки + URL подписки.

        Returns:
            {"links": [...], "subscription_url": "..."}
        """
        user_data = await self.get_user(username)
        links = user_data.get("links", [])
        sub_url = f"{self.base_url}/sub/{username}"

        return {
            "links": links,
            "subscription_url": sub_url,
        }

    # ── Системная информация ──

    async def get_inbounds(self) -> dict[str, Any]:
        """Получить доступные inbound-протоколы."""
        return await self._json_request("GET", "/api/inbounds")

    async def get_node_stats(self) -> dict[str, Any]:
        """Получить статистику сервера/нод (для админ-панели)."""
        return await self._json_request("GET", "/api/system")

    async def get_node_usage(self) -> list[dict[str, Any]]:
        """Статистика использования по нодам."""
        try:
            result = await self._json_request("GET", "/api/nodes/usage")
            return result if isinstance(result, list) else result.get("usages", [])
        except MarzbanNotFoundError:
            return []


marzban = MarzbanAPI()
