#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""proxy_handler 单元测试集 (全协议解析与安全校验)"""

import base64
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from proxy_handler import (
    _parse_proxy_url,
    _write_config,
    ProxyConfigError,
    main,
)


class TestProtocolParsers(unittest.TestCase):
    def test_socks5_parsing(self):
        scheme, ob = _parse_proxy_url("socks5://user:pass@192.0.2.1:1080")
        self.assertEqual(scheme, "socks5")
        self.assertEqual(ob["type"], "socks")
        self.assertEqual(ob["server"], "192.0.2.1")
        self.assertEqual(ob["server_port"], 1080)
        self.assertEqual(ob["username"], "user")
        self.assertEqual(ob["password"], "pass")

    def test_http_and_https_parsing(self):
        scheme, ob = _parse_proxy_url("http://192.0.2.1:8080")
        self.assertEqual(scheme, "http")
        self.assertEqual(ob["type"], "http")
        self.assertNotIn("tls", ob)

        scheme_s, ob_s = _parse_proxy_url("https://user:pass@192.0.2.1:8443")
        self.assertEqual(scheme_s, "https")
        self.assertEqual(ob_s["type"], "http")
        self.assertTrue(ob_s["tls"]["enabled"])

    def test_vless_tls_and_reality(self):
        valid_uuid = "eace8427-7b0b-4b2d-80ee-cba050946c00"
        url = f"vless://{valid_uuid}@192.0.2.1:443?security=tls&sni=node.example.com&type=ws&path=%2Fws"
        scheme, ob = _parse_proxy_url(url)
        self.assertEqual(scheme, "vless")
        self.assertEqual(ob["uuid"], valid_uuid)
        self.assertEqual(ob["tls"]["server_name"], "node.example.com")
        self.assertEqual(ob["transport"]["type"], "ws")
        self.assertEqual(ob["transport"]["path"], "/ws")

        # Reality
        url_reality = f"vless://{valid_uuid}@192.0.2.1:443?security=reality&sni=test.com&pbk=myPublicKey123&sid=1a2b"
        _, ob_r = _parse_proxy_url(url_reality)
        self.assertTrue(ob_r["tls"]["reality"]["enabled"])
        self.assertEqual(ob_r["tls"]["reality"]["public_key"], "myPublicKey123")
        self.assertEqual(ob_r["tls"]["reality"]["short_id"], "1a2b")

    def test_vmess_parsing(self):
        valid_uuid = "eace8427-7b0b-4b2d-80ee-cba050946c00"
        vmess_data = {
            "add": "192.0.2.1",
            "port": 443,
            "id": valid_uuid,
            "aid": 0,
            "scy": "auto",
            "net": "ws",
            "tls": "tls",
            "sni": "vmess.example.com",
            "path": "/vpath"
        }
        encoded = base64.b64encode(json.dumps(vmess_data).encode("utf-8")).decode("utf-8")
        scheme, ob = _parse_proxy_url(f"vmess://{encoded}")
        self.assertEqual(scheme, "vmess")
        self.assertEqual(ob["type"], "vmess")
        self.assertEqual(ob["server"], "192.0.2.1")
        self.assertEqual(ob["server_port"], 443)
        self.assertEqual(ob["uuid"], valid_uuid)
        self.assertEqual(ob["transport"]["type"], "ws")
        self.assertEqual(ob["transport"]["path"], "/vpath")

    def test_hysteria2_anytls_trojan_tuic(self):
        valid_uuid = "eace8427-7b0b-4b2d-80ee-cba050946c00"
        _, hy2 = _parse_proxy_url("hy2://secretpass@192.0.2.1:443?sni=hy2.example.com")
        self.assertEqual(hy2["type"], "hysteria2")
        self.assertEqual(hy2["password"], "secretpass")

        _, anytls = _parse_proxy_url("anytls://secretpass@192.0.2.1:443?sni=any.example.com")
        self.assertEqual(anytls["type"], "anytls")
        self.assertEqual(anytls["password"], "secretpass")

        _, trojan = _parse_proxy_url("trojan://secretpass@192.0.2.1:443?sni=tr.example.com")
        self.assertEqual(trojan["type"], "trojan")
        self.assertEqual(trojan["password"], "secretpass")

        _, tuic = _parse_proxy_url(f"tuic://{valid_uuid}:secretpass@192.0.2.1:443?sni=tuic.example.com")
        self.assertEqual(tuic["type"], "tuic")
        self.assertEqual(tuic["uuid"], valid_uuid)
        self.assertEqual(tuic["password"], "secretpass")


class TestValidationErrors(unittest.TestCase):
    def test_missing_or_invalid_scheme(self):
        for bad in ["not-a-url", "ftp://host:21", ""]:
            with self.subTest(url=bad), self.assertRaises(ProxyConfigError):
                _parse_proxy_url(bad)

    def test_port_out_of_range(self):
        with self.assertRaises(ProxyConfigError):
            _parse_proxy_url("socks5://192.0.2.1:70000")

    def test_invalid_uuid_rejected(self):
        with self.assertRaises(ProxyConfigError):
            _parse_proxy_url("vless://invalid-uuid-string@192.0.2.1:443")

    def test_insecure_requires_explicit_opt_in(self):
        with patch.dict(os.environ, {"ALLOW_INSECURE_PROXY": ""}):
            with self.assertRaises(ProxyConfigError):
                _parse_proxy_url("hy2://pass@192.0.2.1:443?insecure=1")

        with patch.dict(os.environ, {"ALLOW_INSECURE_PROXY": "1"}):
            scheme, ob = _parse_proxy_url("hy2://pass@192.0.2.1:443?insecure=1")
            self.assertTrue(ob["tls"]["insecure"])

    def test_invalid_vmess_payload(self):
        with self.assertRaises(ProxyConfigError):
            _parse_proxy_url("vmess://!!!not-base64!!!")


class TestFileWritingAndCli(unittest.TestCase):
    def test_write_config_sets_0600_permissions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "config.json")
            test_config = {"log": {"level": "info"}}
            _write_config(test_config, path=out_path)
            self.assertTrue(os.path.exists(out_path))
            mode = stat.S_IMODE(os.stat(out_path).st_mode)
            self.assertEqual(mode, 0o600)

    @patch.dict(os.environ, {"PROXY_URL": ""})
    def test_main_empty_proxy_url_exits_0(self):
        ret = main()
        self.assertEqual(ret, 0)

    @patch.dict(os.environ, {"PROXY_URL": "invalid-url"})
    def test_main_invalid_proxy_url_returns_1(self):
        ret = main()
        self.assertEqual(ret, 1)


if __name__ == "__main__":
    unittest.main()
