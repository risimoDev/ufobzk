"""Защита от брутфорса: блокировка IP по количеству неудачных попыток."""

import logging
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Настройки ──

MAX_ATTEMPTS = 5  # макс. неудачных попыток до бана
BAN_DURATION = 900  # 15 минут бана
ATTEMPT_WINDOW = 300  # окно 5 минут для подсчёта попыток
CLEANUP_INTERVAL = 60  # очистка устаревших записей раз в 60 сек


@dataclass
class _IPRecord:
    attempts: list[float] = field(default_factory=list)
    banned_until: float = 0.0


class BruteForceGuard:
    """In-memory brute-force guard. Thread-safe."""

    def __init__(
        self,
        max_attempts: int = MAX_ATTEMPTS,
        ban_duration: int = BAN_DURATION,
        attempt_window: int = ATTEMPT_WINDOW,
    ):
        self._max_attempts = max_attempts
        self._ban_duration = ban_duration
        self._window = attempt_window
        self._records: dict[str, _IPRecord] = {}
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()

    def _cleanup(self) -> None:
        """Удалить устаревшие записи (вызывается под _lock)."""
        now = time.monotonic()
        if now - self._last_cleanup < CLEANUP_INTERVAL:
            return
        self._last_cleanup = now
        stale = [
            ip for ip, rec in self._records.items()
            if rec.banned_until < now and not rec.attempts
        ]
        for ip in stale:
            del self._records[ip]

    def is_blocked(self, ip: str) -> bool:
        """Проверить, заблокирован ли IP."""
        with self._lock:
            self._cleanup()
            rec = self._records.get(ip)
            if rec is None:
                return False
            if rec.banned_until > time.monotonic():
                return True
            return False

    def record_failure(self, ip: str) -> bool:
        """Записать неудачную попытку. Возвращает True если IP забанен."""
        now = time.monotonic()
        with self._lock:
            self._cleanup()
            rec = self._records.get(ip)
            if rec is None:
                rec = _IPRecord()
                self._records[ip] = rec

            # Уже забанен?
            if rec.banned_until > now:
                return True

            # Добавить попытку, убрать старые
            rec.attempts.append(now)
            rec.attempts = [t for t in rec.attempts if now - t < self._window]

            if len(rec.attempts) >= self._max_attempts:
                rec.banned_until = now + self._ban_duration
                rec.attempts.clear()
                logger.warning(
                    "IP %s заблокирован на %d сек после %d неудачных попыток",
                    ip, self._ban_duration, self._max_attempts,
                )
                return True
            return False

    def record_success(self, ip: str) -> None:
        """Сбросить счётчик после успешного входа."""
        with self._lock:
            rec = self._records.get(ip)
            if rec:
                rec.attempts.clear()

    def unban(self, ip: str) -> bool:
        """Ручная разблокировка IP. Возвращает True если был забанен."""
        with self._lock:
            rec = self._records.get(ip)
            if rec and rec.banned_until > time.monotonic():
                rec.banned_until = 0.0
                rec.attempts.clear()
                logger.info("IP %s разблокирован вручную", ip)
                return True
            return False

    def get_ban_remaining(self, ip: str) -> int:
        """Сколько секунд до разблокировки (0 если не забанен)."""
        with self._lock:
            rec = self._records.get(ip)
            if rec is None:
                return 0
            remaining = rec.banned_until - time.monotonic()
            return max(0, int(remaining))

    def get_stats(self) -> dict:
        """Статистика для мониторинга."""
        now = time.monotonic()
        with self._lock:
            banned = sum(1 for r in self._records.values() if r.banned_until > now)
            tracked = len(self._records)
        return {"tracked_ips": tracked, "banned_ips": banned}


# ── Глобальные экземпляры ──

login_guard = BruteForceGuard(max_attempts=5, ban_duration=900, attempt_window=300)
admin_guard = BruteForceGuard(max_attempts=3, ban_duration=1800, attempt_window=300)
api_guard = BruteForceGuard(max_attempts=10, ban_duration=600, attempt_window=60)
