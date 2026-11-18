import base64
import json
from app.models import clean_key
from app.xray import (
    build_xray_config,
    build_vless_reality_link,
    build_vless_xhttp_link,
    build_vless_ws_link,
    build_vless_grpc_link,
    build_hysteria2_link,
    build_tuic_link,
    get_user_links,
    get_server_links,
    get_all_links,
    get_subscription_content,
    get_subscription_json,
    _parse_xray_stats,
)


def test_clean_key():
    assert clean_key(' "my_key", ') == "my_key"
    assert clean_key("'short_id'") == "short_id"
    assert clean_key(None) is None
    assert clean_key("") is None


def test_build_xray_config(db_session, sample_user):
    config = build_xray_config(db_session)
    assert "inbounds" in config
    assert "outbounds" in config
    assert "routing" in config

    # Check VLESS-REALITY inbound
    tags = [i.get("tag") for i in config["inbounds"]]
    assert "VLESS-REALITY" in tags
    assert "VLESS-WS" in tags
    assert "VLESS-XHTTP" in tags
    assert "VLESS-gRPC" in tags

    reality_inbound = next(i for i in config["inbounds"] if i.get("tag") == "VLESS-REALITY")
    assert reality_inbound["port"] == 443
    stream = reality_inbound["streamSettings"]
    assert stream["security"] == "reality"
    assert stream["realitySettings"]["minClientVer"] == "1.0.0"

    # Check client UUID is included
    user_key = sample_user.vpn_keys[0]
    client_ids = [c["id"] for c in reality_inbound["settings"]["clients"]]
    assert user_key.uuid in client_ids

    # Check safe domains rule exists
    rules = config["routing"]["rules"]
    direct_domains = []
    for r in rules:
        if r.get("outboundTag") == "DIRECT" and "domain" in r:
            direct_domains.extend(r["domain"])
    assert "geosite:youtube" in direct_domains
    assert "geosite:google" in direct_domains


def test_link_generators(sample_user):
    key = sample_user.vpn_keys[0]

    # Reality link
    r_link = build_vless_reality_link(key, "193.47.60.232", 443, "NL-Reality")
    assert r_link.startswith(f"vless://{key.uuid}@193.47.60.232:443?")
    assert "security=reality" in r_link
    assert "flow=xtls-rprx-vision" in r_link
    assert "pbk=" in r_link
    assert "#NL-Reality" in r_link

    # XHTTP link
    x_link = build_vless_xhttp_link(key, "ufolabs.online", 443, "NL-XHTTP")
    assert x_link.startswith(f"vless://{key.uuid}@ufolabs.online:443?")
    assert "type=xhttp" in x_link

    # WS link
    ws_link = build_vless_ws_link(key, "ufolabs.online", 443, "NL-WS")
    assert ws_link.startswith(f"vless://{key.uuid}@ufolabs.online:443?")
    assert "type=ws" in ws_link

    # gRPC link
    grpc_link = build_vless_grpc_link(key, "ufolabs.online", 443, "NL-gRPC")
    assert grpc_link.startswith(f"vless://{key.uuid}@ufolabs.online:443?")
    assert "type=grpc" in grpc_link

    # Hysteria 2 link
    hy_link = build_hysteria2_link(key, "95.217.100.100", 8443, "bing.com", "FI-Hy2")
    assert hy_link.startswith(f"hysteria2://{key.uuid}@95.217.100.100:8443")
    assert "sni=bing.com" in hy_link

    # TUIC link
    tuic_link = build_tuic_link(key, "95.217.100.100", 8444, "bing.com", "FI-TUIC")
    assert tuic_link.startswith(f"tuic://{key.uuid}:{key.uuid}@95.217.100.100:8444")
    assert "alpn=h3" in tuic_link


def test_get_user_and_server_links(sample_user, sample_server, db_session):
    key = sample_user.vpn_keys[0]

    user_links = get_user_links(key)
    assert len(user_links) >= 1
    assert any(l["type"] == "vless-reality" for l in user_links)

    srv_links = get_server_links(key, sample_server)
    types = [l["type"] for l in srv_links]
    assert "vless-reality" in types
    assert "hysteria2" in types
    assert "tuic" in types

    all_links = get_all_links(db_session, key)
    assert len(all_links) == len(user_links) + len(srv_links)


def test_cascade_server_excluded_from_client_links(sample_user, sample_server):
    key = sample_user.vpn_keys[0]
    sample_server.role = "cascade"
    assert get_server_links(key, sample_server) == []
    sample_server.role = "ru"
    assert get_server_links(key, sample_server) == []


def test_subscription_content(sample_user, db_session):
    key = sample_user.vpn_keys[0]
    content = get_subscription_content([key], db=db_session)
    assert isinstance(content, str)
    decoded = base64.b64decode(content.encode()).decode()
    lines = [l for l in decoded.splitlines() if l.strip()]
    assert len(lines) > 0
    assert any(l.startswith("vless://") for l in lines)


def test_subscription_json(sample_user, db_session):
    key = sample_user.vpn_keys[0]
    sub_json = get_subscription_json([key], db=db_session)
    assert "outbounds" in sub_json
    assert "routing" in sub_json
    assert "dns" in sub_json
    assert len(sub_json["outbounds"]) > 0


def test_parse_xray_stats():
    # Modern JSON format
    json_stats = json.dumps({
        "stat": [
            {"name": "user>>>11111111-1111-1111-1111-111111111111>>>traffic>>>uplink", "value": "1000"},
            {"name": "user>>>11111111-1111-1111-1111-111111111111>>>traffic>>>downlink", "value": "4000"},
            {"name": "user>>>22222222-2222-2222-2222-222222222222>>>traffic>>>uplink", "value": "500"},
        ]
    })
    res = _parse_xray_stats(json_stats)
    assert res["11111111-1111-1111-1111-111111111111"] == 5000
    assert res["22222222-2222-2222-2222-222222222222"] == 500

    # Text format
    text_stats = """
    stat {
      name: "user>>>11111111-1111-1111-1111-111111111111>>>traffic>>>uplink"
      value: 1200
    }
    stat {
      name: "user>>>11111111-1111-1111-1111-111111111111>>>traffic>>>downlink"
      value: 3800
    }
    """
    res_text = _parse_xray_stats(text_stats)
    assert res_text["11111111-1111-1111-1111-111111111111"] == 5000

    # Empty
    assert _parse_xray_stats("") == {}
    assert _parse_xray_stats(None) == {}
