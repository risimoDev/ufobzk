import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.cheburcheck import (
    probe_server_ports,
    _update_blocked_flags,
    check_target,
    run_cheburcheck_audit,
)
from app.models import get_setting


@pytest.mark.asyncio
async def test_probe_server_ports_only_probes_tcp_reality(db_session, sample_server):
    """Проверяет, что probe_server_ports опрашивает ТОЛЬКО reality_port (TCP),
    и НЕ опрашивает hysteria_port/tuic_port (UDP), что предотвращает ложное
    блокирование ноды."""
    with patch("app.cheburcheck.probe_tcp", new_callable=AsyncMock) as mock_probe:
        mock_probe.return_value = (True, "")
        res = await probe_server_ports(db_session)

        # Должен быть вызван только для порта 443 (reality_port)
        assert mock_probe.call_count == 1
        args, _ = mock_probe.call_args
        assert args[0] == sample_server.host
        assert args[1] == 443  # reality_port, not 8443 or 8444!

        assert sample_server.host in res
        assert 443 in res[sample_server.host]
        assert res[sample_server.host][443]["ok"] is True


def test_update_blocked_flags(db_session, sample_server):
    summary = {
        "results": [
            {"target": sample_server.host, "blocked": True}
        ]
    }
    _update_blocked_flags(db_session, summary)
    db_session.refresh(sample_server)
    assert sample_server.is_blocked_ru is True

    # Now unblock
    summary_clean = {
        "results": [
            {"target": sample_server.host, "blocked": False}
        ]
    }
    _update_blocked_flags(db_session, summary_clean)
    db_session.refresh(sample_server)
    assert sample_server.is_blocked_ru is False


@pytest.mark.asyncio
async def test_check_target_string_call():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "blocked": False,
        "blocked_subnets": [],
        "geo": {"country_code": "FI", "asn": "AS1234", "organisation": "Hetzner"}
    }

    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_resp)

    res = await check_target(mock_client, "95.217.100.100")
    assert res["target"] == "95.217.100.100"
    assert res["blocked"] is False
    assert res["status"] == "clean"
    assert res["country"] == "FI"


@pytest.mark.asyncio
async def test_run_cheburcheck_audit(db_session, sample_server):
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get, \
         patch("app.cheburcheck.probe_tcp", new_callable=AsyncMock) as mock_probe:
        
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"blocked": False, "blocked_subnets": [], "geo": {}}
        mock_get.return_value = mock_resp
        mock_probe.return_value = (True, "")

        summary = await run_cheburcheck_audit(db_session, notify=False)
        assert summary["total"] >= 1
        assert summary["has_blocked"] is False
        assert summary["blocked_count"] == 0

        # Verify saved in app_settings
        saved = get_setting(db_session, "cheburcheck_last_audit")
        assert saved != ""
        assert "checked_at" in saved
