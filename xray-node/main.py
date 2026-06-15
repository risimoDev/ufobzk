"""Xray Node — минимальный микросервис для remote серверов.

Получает config.json от Main Server и применяет его к локальному Xray.
"""

import http.client
import logging
import os
import socket
import subprocess
import urllib.parse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

XRAY_NODE_TOKEN = os.getenv("XRAY_NODE_TOKEN", "")
XRAY_CONFIG_PATH = os.getenv("XRAY_CONFIG_PATH", "/etc/xray/config.json")
XRAY_CONTAINER_NAME = os.getenv("XRAY_CONTAINER_NAME", "xray")
DOCKER_SOCK = "/var/run/docker.sock"


def _verify_token(authorization: str | None) -> None:
    if not XRAY_NODE_TOKEN:
        raise HTTPException(status_code=500, detail="XRAY_NODE_TOKEN not configured")
    expected = f"Bearer {XRAY_NODE_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid token")


class _UnixSocketConn(http.client.HTTPConnection):
    """HTTP-соединение через Unix socket (Docker API без docker CLI)."""
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(DOCKER_SOCK)


def _restart_xray_via_socket(container_name: str, stop_timeout: int = 5) -> bool:
    """Перезапустить xray через Docker REST API (Unix socket).

    Работает без установленного docker CLI в контейнере.
    """
    try:
        enc = urllib.parse.quote(container_name, safe="")
        conn = _UnixSocketConn("localhost", timeout=20)
        conn.request(
            "POST",
            f"/containers/{enc}/restart?t={stop_timeout}",
            headers={"Content-Length": "0", "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        resp.read()
        conn.close()
        if resp.status == 204:
            logger.info("Xray restarted via Docker socket API: %s", container_name)
            return True
        logger.warning("Docker socket API returned HTTP %d for %s", resp.status, container_name)
        return False
    except Exception as e:
        logger.warning("Docker socket restart failed for %s: %s", container_name, e)
        return False


app = FastAPI(title="Xray Node")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/config")
async def apply_config(request: Request, authorization: str | None = Header(None)):
    """Получить config.json и применить к локальному Xray.

    КРИТИЧНО: рестарт Xray выполняется ТОЛЬКО если конфиг реально изменился.
    Основной сервер пушит конфиг каждые 5 минут безусловно; без этой проверки
    Xray на ноде перезапускался бы каждые 5 минут, обрывая ВСЕ соединения
    пользователей (главная причина «то работает, то нет»).
    """
    _verify_token(authorization)
    config = await request.json()
    import json as _json

    new_content = _json.dumps(config, indent=2, ensure_ascii=False, sort_keys=True)

    # ── Детект изменений: если конфиг идентичен — НЕ трогаем Xray ──
    try:
        with open(XRAY_CONFIG_PATH, encoding="utf-8") as f:
            current = f.read()
        # Нормализуем текущий файл к тому же виду (sort_keys) для честного сравнения
        try:
            current_norm = _json.dumps(_json.loads(current), indent=2,
                                       ensure_ascii=False, sort_keys=True)
        except Exception:
            current_norm = current
        if current_norm == new_content:
            logger.info("Config unchanged — restart skipped")
            return JSONResponse({"ok": True, "restarted": False, "unchanged": True})
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("Config compare failed (will rewrite+restart): %s", e)

    # Записываем конфиг (он изменился)
    with open(XRAY_CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(new_content)
    logger.info("Config changed → written to %s", XRAY_CONFIG_PATH)

    restart_via = None
    error_msg = None

    # ── Попытка 1: Docker REST API через Unix socket (без docker CLI) ──
    if os.path.exists(DOCKER_SOCK):
        if _restart_xray_via_socket(XRAY_CONTAINER_NAME):
            restart_via = "docker-socket"
        else:
            # ── Попытка 2: docker CLI (если вдруг установлен) ──
            try:
                subprocess.run(
                    ["docker", "restart", XRAY_CONTAINER_NAME],
                    capture_output=True, text=True, timeout=30, check=True
                )
                restart_via = "docker-cli"
                logger.info("Xray restarted via docker CLI: %s", XRAY_CONTAINER_NAME)
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                err_text = getattr(e, "stderr", "") or str(e)
                logger.warning("Docker CLI restart failed: %s", err_text)
                error_msg = f"docker-socket: failed; docker-cli: {err_text}"

    # ── Попытка 3: systemctl (bare metal без Docker) ──
    if not restart_via:
        try:
            subprocess.run(
                ["systemctl", "restart", "xray"],
                capture_output=True, text=True, timeout=15, check=True
            )
            restart_via = "systemctl"
            logger.info("Xray restarted via systemctl")
        except Exception as e2:
            prev = error_msg or ""
            error_msg = f"{prev}; systemctl: {e2}".lstrip("; ")
            logger.error("All restart methods failed: %s", error_msg)

    if restart_via:
        return JSONResponse({"ok": True, "restarted": True, "via": restart_via})
    else:
        return JSONResponse({"ok": True, "restarted": False, "error": error_msg}, status_code=202)
