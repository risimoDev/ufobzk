from app.remote_xray import _build_singbox_remote_config, _build_remote_config


def test_build_singbox_remote_config(sample_user, sample_server):
    keys = sample_user.vpn_keys
    cfg = _build_singbox_remote_config(sample_server, keys)

    assert "inbounds" in cfg
    assert "outbounds" in cfg
    assert "route" in cfg

    types = [i["type"] for i in cfg["inbounds"]]
    assert "vless" in types
    assert "hysteria2" in types
    assert "tuic" in types

    # Hysteria2 inbound checks
    hy = next(i for i in cfg["inbounds"] if i["type"] == "hysteria2")
    assert hy["listen_port"] == 8443
    assert hy["tls"]["alpn"] == ["h3"]
    assert hy["users"][0]["password"] == keys[0].uuid

    # TUIC inbound checks
    tuic = next(i for i in cfg["inbounds"] if i["type"] == "tuic")
    assert tuic["listen_port"] == 8444
    assert tuic["tls"]["alpn"] == ["h3"]
    assert tuic["congestion_control"] == "bbr"
    assert tuic["users"][0]["uuid"] == keys[0].uuid


def test_build_remote_config_xray_fallback(sample_user, sample_server):
    sample_server.core_type = "xray"
    keys = sample_user.vpn_keys
    cfg = _build_remote_config(sample_server, keys)

    assert "inbounds" in cfg
    tags = [i.get("tag") for i in cfg["inbounds"]]
    assert "VLESS-REALITY" in tags
