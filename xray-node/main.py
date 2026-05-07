"""Xray Node — минимальный микросервис для remote серверов.

Получает config.json от Main Server и применяет его к локальному Xray.
"""

import logging
import os
import subprocess

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

XRAY_NODE_TOKEN = os.getenv("XRAY_NODE_TOKEN", "")
XRAY_CONFIG_PATH = os.getenv("XRAY_CONFIG_PATH", "/etc/xray/config.json")
XRAY_CONTAINER_NAME = os.getenv("XRAY_CONTAINER_NAME", "xray")

def _verify_token(authorization: str | None) -> None:
    if not XRAY_NODE_TOKEN:
        raise HTTPException(status_code=500, detail="XRAY_NODE_TOKEN not configured")
    expected = f"Bearer {XRAY_NODE_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Invalid token")

app = FastAPI(title="Xray Node")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/config")
async def apply_config(request: Request, authorization: str | None = Header(None)):
    """Получить config.json и применить к локальному Xray."""
    _verify_token(authorization)
    config = await request.json()
    import json as _json

    # Записываем конфиг
    with open(XRAY_CONFIG_PATH, "w", encoding="utf-8") as f:
        _json.dump(config, f, indent=2, ensure_ascii=False)
    logger.info("Config written to %s", XRAY_CONFIG_PATH)

    # Перезагружаем Xray: сначала Docker, потом systemctl fallback
    restart_via = None
    error_msg = None

    try:
        subprocess.run(
            ["docker", "restart", XRAY_CONTAINER_NAME],
            capture_output=True, text=True, timeout=30, check=True
        )
        restart_via = "docker"
        logger.info("Xray container restarted: %s", XRAY_CONTAINER_NAME)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        stderr = getattr(e, "stderr", "")
        err_text = stderr or str(e)
        logger.warning("Docker restart failed (%s), trying systemctl fallback...", err_text)
        try:
            subprocess.run(
                ["systemctl", "restart", "xray"],
                capture_output=True, text=True, timeout=15, check=True
            )
            restart_via = "systemctl"
            logger.info("Xray restarted via systemctl")
        except Exception as e2:
            error_msg = f"docker: {err_text}; systemctl: {e2}"
            logger.error("Failed to restart Xray: %s", error_msg)

    if restart_via:
        return JSONResponse({"ok": True, "restarted": True, "via": restart_via})
    else:
        return JSONResponse({"ok": True, "restarted": False, "error": error_msg}, status_code=202)
