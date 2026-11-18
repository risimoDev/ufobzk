from app.remote_xray import _build_singbox_remote_config, _build_remote_config
from app.node_installer import generate_install_script


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


def test_generate_install_script_syntax(sample_server):
    script = generate_install_script(sample_server, "https://ufolabs.online")
    assert script.startswith("#!/usr/bin/env bash")
    assert "AGENT_EOF" in script

    # Check tr -d is not broken across lines
    assert 'tr -d \'", \\r\\n\')"' in script

    # Extract the embedded Python agent script
    marker_start = 'cat << \'AGENT_EOF\' > "$WORK_DIR/api/main.py"\n'
    marker_end = "\nAGENT_EOF"
    start_idx = script.find(marker_start) + len(marker_start)
    end_idx = script.find(marker_end, start_idx)
    assert start_idx > len(marker_start)
    assert end_idx > start_idx

    agent_py = script[start_idx:end_idx]

    # Must compile without SyntaxError (unterminated string literal on err_clean)
    compiled = compile(agent_py, "main.py", "exec")
    assert compiled is not None
    assert 'err_clean = "\\n".join(lines).strip()' in agent_py

