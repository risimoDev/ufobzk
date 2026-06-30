"""MTProto-прокси для Telegram (mtg, FakeTLS).

Отдельный демон `mtg` (nineseconds/mtg:2) — нативный MTProto-inbound вырезан из
Xray-core, поэтому переиспользовать xray-контейнер нельзя. Архитектура зеркалит
xray.py: приложение владеет конфигом в общем docker-volume, контейнер `mtg`
ждёт файл и читает его, приложение перезапускает контейнер через docker-socket.

Секрет — один общий FakeTLS ee-secret на инстанс (mtg = один секрет на процесс),
генерируется в Python и хранится в AppSetting. Понятия персонального ключа с
учётом трафика, как у VLESS (UUID + Xray stats), у MTProto нет.
"""

import logging
import os
from pathlib import Path
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from app.models import get_setting
# Переиспользуем docker-socket рестарт из xray.py — там же, где reload_xray.
from app.xray import DOMAIN, NL_SERVER_IP, _docker_restart_via_socket

logger = logging.getLogger(__name__)

# Путь конфига внутри общего volume mtg-config (см. docker-compose.yml).
MTG_CONFIG_PATH = os.getenv("MTG_CONFIG_PATH", "/etc/mtg/config.toml")
# Публикуемый host-порт MTProto (контейнер слушает 3128, наружу — этот порт).
MTPROTO_PORT = os.getenv("MTPROTO_PORT", "8765")
# Имя контейнера mtg для рестарта через docker-socket.
MTG_CONTAINER_NAME = os.getenv("MTG_CONTAINER_NAME", "ufobzk-mtg")

# Домен FakeTLS по умолчанию — реальный достижимый HTTPS-хост, под который
# маскируется прокси. Должен быть не заблокирован и отвечать по TLS.
DEFAULT_FAKETLS_DOMAIN = "www.cloudflare.com"


def generate_mtproto_secret(domain: str = DEFAULT_FAKETLS_DOMAIN) -> str:
    """Сгенерировать FakeTLS ee-secret.

    Формат: префикс "ee" + 16 случайных байт (hex) + домен (hex).
    Telegram-клиент по этому секрету понимает, что нужно маскировать трафик под
    TLS-хендшейк к указанному домену.
    """
    domain = (domain or DEFAULT_FAKETLS_DOMAIN).strip()
    return "ee" + os.urandom(16).hex() + domain.encode("utf-8").hex()


def build_mtproto_links(host: str, port: str | int, secret: str) -> dict[str, str]:
    """Построить ссылки подключения для Telegram.

    Возвращает {"tg": "tg://proxy?...", "https": "https://t.me/proxy?..."}.
    """
    query = urlencode({"server": host, "port": str(port), "secret": secret})
    return {
        "tg": f"tg://proxy?{query}",
        "https": f"https://t.me/proxy?{query}",
    }


def get_mtproto_config(db: Session) -> dict[str, object]:
    """Прочитать настройки MTProto из БД и собрать готовые ссылки.

    host: настройка mtproto_host → NL_SERVER_IP → DOMAIN.
    port: настройка mtproto_port → env MTPROTO_PORT.
    Ссылки заполняются только если прокси включён и секрет задан.
    """
    enabled = get_setting(db, "mtproto_enabled", "0") == "1"
    secret = get_setting(db, "mtproto_secret", "")
    domain = get_setting(db, "mtproto_domain", DEFAULT_FAKETLS_DOMAIN)
    host = get_setting(db, "mtproto_host", "") or NL_SERVER_IP or DOMAIN
    port = get_setting(db, "mtproto_port", "") or MTPROTO_PORT

    cfg: dict[str, object] = {
        "enabled": enabled,
        "secret": secret,
        "domain": domain,
        "host": host,
        "port": port,
        "tg_link": "",
        "https_link": "",
    }
    if enabled and secret and host:
        links = build_mtproto_links(host, port, secret)
        cfg["tg_link"] = links["tg"]
        cfg["https_link"] = links["https"]
    return cfg


def write_mtg_config(db: Session) -> bool:
    """Записать config.toml для mtg в общий volume.

    Возвращает True если содержимое изменилось (нужен рестарт контейнера),
    False если идентично текущему файлу. Если прокси выключен или нет секрета —
    конфиг не пишется (контейнер mtg продолжит ждать файл).
    """
    secret = get_setting(db, "mtproto_secret", "")
    enabled = get_setting(db, "mtproto_enabled", "0") == "1"
    if not (enabled and secret):
        logger.debug("MTProto выключен или нет секрета — конфиг mtg не пишется")
        return False

    new_content = f'secret = "{secret}"\nbind-to = "0.0.0.0:3128"\n'
    config_path = Path(MTG_CONFIG_PATH)
    config_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        if config_path.exists() and config_path.read_text(encoding="utf-8") == new_content:
            logger.debug("mtg config не изменился — перезапись пропущена")
            return False
    except Exception:
        pass

    config_path.write_text(new_content, encoding="utf-8")
    logger.info("mtg config записан: %s", MTG_CONFIG_PATH)
    return True


def reload_mtg() -> bool:
    """Перезапустить контейнер mtg через Docker socket API.

    Как и xray, mtg читает конфиг только при старте — применяем новый секрет
    полным рестартом контейнера.
    """
    docker_sock = "/var/run/docker.sock"
    if not os.path.exists(docker_sock):
        logger.debug("docker.sock недоступен — рестарт mtg пропущен")
        return False
    if _docker_restart_via_socket(MTG_CONTAINER_NAME):
        logger.info("mtg перезапущен через Docker API (socket)")
        return True
    logger.warning("Не удалось перезапустить контейнер mtg (%s)", MTG_CONTAINER_NAME)
    return False


def sync_mtg(db: Session) -> bool:
    """Переписать конфиг mtg и перезапустить контейнер (только если изменился)."""
    changed = write_mtg_config(db)
    if not changed:
        logger.debug("mtg конфиг не изменился — рестарт не нужен")
        return True
    return reload_mtg()
