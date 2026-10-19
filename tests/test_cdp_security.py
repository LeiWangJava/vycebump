#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CDP 安全边界与受限提取单元测试"""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app


class TestCdpEndpointValidation(unittest.TestCase):
    def test_loopback_endpoint_accepted(self):
        valid = app._loopback_cdp_endpoint("http://127.0.0.1:9222")
        self.assertIsNotNone(valid)
        self.assertEqual(valid.hostname, "127.0.0.1")
        self.assertEqual(valid.port, 9222)

        valid_v6 = app._loopback_cdp_endpoint("http://[::1]:9333")
        self.assertIsNotNone(valid_v6)
        self.assertEqual(valid_v6.hostname, "::1")
        self.assertEqual(valid_v6.port, 9333)

    def test_unsafe_endpoints_rejected(self):
        unsafe = [
            "http://192.168.1.50:9222",
            "http://evil.com:9222",
            "https://127.0.0.1:9222",
            "http://user:pass@127.0.0.1:9222",
            "http://127.0.0.1:70000",
            "http://127.0.0.1:-1",
            "http://127.0.0.1:9222/extra/path",
            "not-a-url",
            "",
        ]
        for url in unsafe:
            with self.subTest(url=url):
                self.assertIsNone(app._loopback_cdp_endpoint(url))


class TestCdpTargetFiltering(unittest.TestCase):
    @patch("app.http_request")
    def test_rejects_external_or_subdomain_tabs(self, mock_http):
        mock_http.return_value = (200, {}, json.dumps([
            {
                "type": "page",
                "url": "https://vyce.cloud.attacker.com/",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/abc",
            },
            {
                "type": "page",
                "url": "http://vyce.cloud/",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/abc",
            },
            {
                "type": "other",
                "url": "https://vyce.cloud/",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/abc",
            },
        ]))
        self.assertIsNone(app.get_cookie_from_cdp("http://127.0.0.1:9222"))

    @patch("app.http_request")
    def test_rejects_cross_host_debugger_url(self, mock_http):
        mock_http.return_value = (200, {}, json.dumps([
            {
                "type": "page",
                "url": "https://vyce.cloud/servers",
                "webSocketDebuggerUrl": "ws://attacker.com:9222/devtools/page/abc",
            },
            {
                "type": "page",
                "url": "https://vyce.cloud/servers",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9999/devtools/page/abc",
            },
            {
                "type": "page",
                "url": "https://vyce.cloud/servers",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/other/path/abc",
            },
        ]))
        self.assertIsNone(app.get_cookie_from_cdp("http://127.0.0.1:9222"))

    @patch("subprocess.run")
    @patch("app.http_request")
    def test_successful_cdp_extraction(self, mock_http, mock_subp):
        mock_http.return_value = (200, {}, json.dumps([
            {
                "type": "page",
                "url": "https://vyce.cloud/servers",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/valid-tab-123",
            }
        ]))
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "__Host-vyce_sid=session_token_xyz"
        mock_subp.return_value = mock_proc

        result = app.get_cookie_from_cdp("http://127.0.0.1:9222")
        self.assertEqual(result, "__Host-vyce_sid=session_token_xyz")


if __name__ == "__main__":
    unittest.main()
