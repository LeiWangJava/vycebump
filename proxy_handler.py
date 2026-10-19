#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Parse PROXY_URL and generate a restrictive sing-box HTTP proxy config."""

import base64
import binascii
import json
import os
import re
import stat
import tempfile
from urllib.parse import parse_qs, unquote, urlparse

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 8080
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$")


class ProxyConfigError(ValueError):
    """A safe, user-actionable proxy configuration error."""


def _required(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ProxyConfigError(f"missing {label}")
    return value.strip()


def _host(parsed):
    try:
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ProxyConfigError("invalid proxy host or port") from exc
    if not host:
        raise ProxyConfigError("missing proxy host")
    if port is not None and not 1 <= port <= 65535:
        raise ProxyConfigError("proxy port out of range")
    return host, port


def _port(parsed, default):
    _host(parsed)
    try:
        port = parsed.port or default
    except ValueError as exc:
        raise ProxyConfigError("invalid proxy port") from exc
    if not 1 <= port <= 65535:
        raise ProxyConfigError("proxy port out of range")
    return port


def _tls(params, *, required=True):
    tls = {"enabled": required}
    sni = params.get("sni", [""])[0]
    if sni:
        tls["server_name"] = sni
    fp = params.get("fp", params.get("client-fingerprint", [""]))[0]
    if fp:
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    alpn = params.get("alpn", [""])[0]
    if alpn:
        tls["alpn"] = [part for part in alpn.split(",") if part]
    insecure = params.get("insecure", params.get("allowInsecure", ["0"]))[0]
    if insecure not in ("0", "1"):
        raise ProxyConfigError("insecure must be 0 or 1")
    if insecure == "1":
        if os.environ.get("ALLOW_INSECURE_PROXY", "").lower() not in ("1", "true", "yes"):
            raise ProxyConfigError("insecure proxy requires ALLOW_INSECURE_PROXY=1")
        tls["insecure"] = True
    return tls


def parse_socks5(parsed):
    host, _ = _host(parsed)
    outbound = {"type": "socks", "tag": "proxy", "server": host, "server_port": _port(parsed, 1080), "version": "5"}
    if parsed.username is not None:
        outbound["username"] = _required(unquote(parsed.username), "SOCKS username")
    if parsed.password is not None:
        outbound["password"] = _required(unquote(parsed.password), "SOCKS password")
    return outbound


def parse_http(parsed):
    host, _ = _host(parsed)
    outbound = {"type": "http", "tag": "proxy", "server": host, "server_port": _port(parsed, 8080)}
    if parsed.username is not None:
        outbound["username"] = _required(unquote(parsed.username), "HTTP username")
    if parsed.password is not None:
        outbound["password"] = _required(unquote(parsed.password), "HTTP password")
    if parsed.scheme == "https":
        outbound["tls"] = _tls({}, required=True)
    return outbound


def parse_vless(parsed, params):
    host, _ = _host(parsed)
    uuid = _required(unquote(parsed.username or ""), "VLESS UUID")
    if not _UUID_RE.fullmatch(uuid):
        raise ProxyConfigError("invalid VLESS UUID")
    outbound = {"type": "vless", "tag": "proxy", "server": host, "server_port": _port(parsed, 443), "uuid": uuid}
    flow = params.get("flow", [""])[0]
    if flow:
        outbound["flow"] = flow
    security = params.get("security", [""])[0]
    if security in ("tls", "reality"):
        tls = _tls(params)
        if security == "reality":
            pbk = _required(params.get("pbk", [""])[0], "Reality public key")
            tls["reality"] = {"enabled": True, "public_key": pbk}
            sid = params.get("sid", [""])[0]
            if sid:
                tls["reality"]["short_id"] = sid
        outbound["tls"] = tls
    elif security not in ("", "none"):
        raise ProxyConfigError("unsupported VLESS security")
    _add_transport(outbound, params)
    return outbound


def _add_transport(outbound, params):
    net_type = params.get("type", [""])[0]
    if net_type == "ws":
        transport = {"type": "ws"}
        if params.get("path", [""])[0]:
            transport["path"] = unquote(params["path"][0])
        if params.get("host", [""])[0]:
            transport["headers"] = {"Host": params["host"][0]}
        outbound["transport"] = transport
    elif net_type == "grpc":
        transport = {"type": "grpc"}
        if params.get("serviceName", [""])[0]:
            transport["service_name"] = params["serviceName"][0]
        outbound["transport"] = transport
    elif net_type in ("http", "h2"):
        transport = {"type": "http"}
        if params.get("path", [""])[0]:
            transport["path"] = unquote(params["path"][0])
        if params.get("host", [""])[0]:
            transport["host"] = [params["host"][0]]
        outbound["transport"] = transport
    elif net_type:
        raise ProxyConfigError("unsupported proxy transport")


def parse_vmess(url_str):
    encoded = url_str[len("vmess://"):]
    try:
        decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True).decode("utf-8")
        cfg = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProxyConfigError("invalid VMess payload") from exc
    if not isinstance(cfg, dict):
        raise ProxyConfigError("invalid VMess schema")
    host = _required(cfg.get("add"), "VMess server")
    uuid = _required(cfg.get("id"), "VMess UUID")
    if not _UUID_RE.fullmatch(uuid):
        raise ProxyConfigError("invalid VMess UUID")
    try:
        port = int(cfg.get("port", 443))
        alter_id = int(cfg.get("aid", 0))
    except (TypeError, ValueError) as exc:
        raise ProxyConfigError("invalid VMess numeric field") from exc
    if not 1 <= port <= 65535 or alter_id < 0:
        raise ProxyConfigError("invalid VMess numeric field")
    outbound = {"type": "vmess", "tag": "proxy", "server": host, "server_port": port, "uuid": uuid, "security": cfg.get("scy", "auto"), "alter_id": alter_id}
    if cfg.get("tls") == "tls":
        tls = {"enabled": True}
        if cfg.get("sni") or cfg.get("host"):
            tls["server_name"] = cfg.get("sni") or cfg.get("host")
        if cfg.get("alpn"):
            tls["alpn"] = str(cfg["alpn"]).split(",")
        outbound["tls"] = tls
    _add_transport(outbound, {"type": [cfg.get("net", "tcp")], "path": [cfg.get("path", "")], "host": [cfg.get("host", "")], "serviceName": [cfg.get("path", "")]})
    return outbound


def _parse_password_tls(parsed, params, protocol):
    host, _ = _host(parsed)
    password = _required(unquote(parsed.username or ""), f"{protocol} password")
    outbound = {"type": protocol, "tag": "proxy", "server": host, "server_port": _port(parsed, 443), "password": password, "tls": _tls(params)}
    return outbound


def parse_hysteria2(parsed, params):
    outbound = _parse_password_tls(parsed, params, "hysteria2")
    obfs = params.get("obfs", [""])[0]
    if obfs:
        outbound["obfs"] = {"type": obfs, "password": _required(params.get("obfs-password", [""])[0], "obfs password")}
    return outbound


def parse_anytls(parsed, params):
    return _parse_password_tls(parsed, params, "anytls")


def parse_trojan(parsed, params):
    outbound = _parse_password_tls(parsed, params, "trojan")
    _add_transport(outbound, params)
    return outbound


def parse_tuic(parsed, params):
    host, _ = _host(parsed)
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    if not password and ":" in user:
        user, password = user.split(":", 1)
    uuid = _required(user, "TUIC UUID")
    password = _required(password, "TUIC password")
    if not _UUID_RE.fullmatch(uuid):
        raise ProxyConfigError("invalid TUIC UUID")
    outbound = {"type": "tuic", "tag": "proxy", "server": host, "server_port": _port(parsed, 443), "uuid": uuid, "password": password, "congestion_control": params.get("congestion_control", ["bbr"])[0], "tls": _tls(params)}
    return outbound


def _parse_proxy_url(proxy_url):
    if not isinstance(proxy_url, str) or "://" not in proxy_url:
        raise ProxyConfigError("proxy URI must include a scheme")
    scheme = proxy_url.split("://", 1)[0].lower()
    if scheme == "vmess":
        return scheme, parse_vmess(proxy_url)
    parsed = urlparse(proxy_url)
    if parsed.scheme.lower() != scheme:
        raise ProxyConfigError("invalid proxy URI")
    params = parse_qs(parsed.query, keep_blank_values=True)
    parsers = {
        "socks5": parse_socks5,
        "http": parse_http,
        "https": parse_http,
        "vless": parse_vless,
        "hy2": parse_hysteria2,
        "hysteria2": parse_hysteria2,
        "trojan": parse_trojan,
        "anytls": parse_anytls,
        "tuic": parse_tuic,
    }
    parser = parsers.get(scheme)
    if not parser:
        raise ProxyConfigError(f"unsupported proxy protocol: {scheme}")
    return scheme, parser(parsed, params) if scheme in ("vless", "hy2", "hysteria2", "trojan", "anytls", "tuic") else parser(parsed)


def _write_config(config, path="config.json"):
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, temporary = tempfile.mkstemp(prefix=".config.", suffix=".tmp", dir=directory, text=True)
    try:
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main():
    proxy_url = os.environ.get("PROXY_URL", "").strip()
    if not proxy_url:
        print("PROXY_URL is empty, skipping sing-box config generation.")
        return 0
    try:
        scheme, outbound = _parse_proxy_url(proxy_url)
        outbounds = [outbound, {"type": "direct", "tag": "direct"}]
        chain_url = os.environ.get("PROXY_CHAIN_URL", "").strip()
        if chain_url:
            chain_scheme, chain = _parse_proxy_url(chain_url)
            outbound["tag"] = "dialer"
            chain["tag"] = "proxy"
            chain["detour"] = "dialer"
            outbounds = [outbound, chain, {"type": "direct", "tag": "direct"}]
            print(f"Chain mode: {scheme} -> {chain_scheme}")
        config = {
            "log": {"level": "info", "timestamp": True},
            "inbounds": [{"type": "http", "tag": "http-in", "listen": LISTEN_HOST, "listen_port": LISTEN_PORT}],
            "outbounds": outbounds,
            "route": {"final": "proxy"},
        }
        _write_config(config)
        print(f"sing-box config generated ({scheme}); inbound http://{LISTEN_HOST}:{LISTEN_PORT}")
        return 0
    except ProxyConfigError as exc:
        print(f"Invalid proxy configuration: {exc}")
        return 1
    except OSError as exc:
        print(f"Unable to write proxy configuration: {exc.__class__.__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
