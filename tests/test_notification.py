#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知层测试: 消息构建 / 渠道分发 / 邮件 SMTP 边界 (原生 unittest，零第三方依赖)"""

import email
import os
import smtplib
import ssl
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app


class TestBuildNotification(unittest.TestCase):
    def test_plain_and_html_share_same_fields(self):
        content = app.build_notification(
            "🎉",
            "Vyce VPS 续期成功",
            "someone@example.com",
            details={
                "server_name": "vps-mini",
                "plan_id": "free",
                "os": "debian",
                "time_left_str": "1d 12h",
                "next_renewal_str": "2026-10-21 00:27:23",
            },
        )
        for keyword in ("vps-mini", "free", "1d 12h", "2026-10-21 00:27:23", "so****ne@example.com"):
            self.assertIn(keyword, content["plain"])
            self.assertIn(keyword, content["html"])

    def test_error_message_is_html_escaped(self):
        content = app.build_notification("❌", "失败", "user", error_msg="<script>alert(1)</script>")
        self.assertNotIn("<script>", content["html"])
        self.assertIn("&lt;script&gt;", content["html"])
        # 纯文本保持原样
        self.assertIn("<script>alert(1)</script>", content["plain"])

    def test_missing_details_only_render_account_and_time(self):
        content = app.build_notification("❌", "Cookie 失效", "user")
        self.assertNotIn("服务器", content["plain"])
        self.assertIn("报告时间", content["plain"])


class TestNotificationDispatch(unittest.TestCase):
    def test_dispatch_fans_out_to_both_channels(self):
        with patch("app.send_tg_message") as mock_tg, patch("app.send_email_message") as mock_email:
            app.send_notification("🎉", "续期成功", "user", details={"server_name": "vps-mini"}, proxy="http://127.0.0.1:8080")
        mock_tg.assert_called_once_with("🎉", "续期成功", "user", {"server_name": "vps-mini"}, "", "http://127.0.0.1:8080")
        mock_email.assert_called_once_with("🎉", "续期成功", "user", {"server_name": "vps-mini"}, "", "http://127.0.0.1:8080")

    def test_process_account_uses_unified_entrypoint(self):
        # 保证未来的续期分支不会被改回单渠道调用
        import inspect
        source = inspect.getsource(app.process_account)
        self.assertNotIn("send_tg_message(", source)
        self.assertNotIn("send_email_message(", source)
        self.assertIn("send_notification(", source)


class TestEmailChannel(unittest.TestCase):
    def setUp(self):
        # 每个用例在干净的配置下运行，避免宿主环境变量串扰
        for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD", "EMAIL_FROM", "EMAIL_TO", "EMAIL_SUBJECT_PREFIX"):
            self.addCleanup(restore_env, name, os.environ.get(name))
            os.environ.pop(name, None)
        reload_app_config()

    def test_skipped_without_host_or_recipient(self):
        with patch("app.smtplib.SMTP") as mock_smtp:
            app.send_email_message("🎉", "续期成功", "user")  # SMTP_HOST 为空
            mock_smtp.assert_not_called()

        os.environ["SMTP_HOST"] = "smtp.example.com"
        with patch("app.smtplib.SMTP") as mock_smtp:
            app.send_email_message("🎉", "续期成功", "user")  # EMAIL_TO 为空
            mock_smtp.assert_not_called()

    def test_skipped_with_username_but_no_password(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "notify@mangoqwq.com",
            "EMAIL_TO": "notify@mangoqwq.com",
        })
        reload_app_config()
        with patch("app.smtplib.SMTP") as mock_smtp:
            app.send_email_message("🎉", "续期成功", "user")
            mock_smtp.assert_not_called()

    def test_starttls_on_default_port_587(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "notify@mangoqwq.com",
            "SMTP_PASSWORD": "secret",
            "EMAIL_TO": "a@example.com, b@example.com",
        })
        reload_app_config()
        with patch("app.smtplib.SMTP") as mock_smtp_cls:
            instance = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = instance
            app.send_email_message("🎉", "续期成功", "someone@example.com")
        mock_smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=15)
        instance.starttls.assert_called_once()
        instance.login.assert_called_once_with("notify@mangoqwq.com", "secret")
        sent = instance.send_message.call_args[0][0]
        self.assertEqual(sent["To"], "a@example.com, b@example.com")
        self.assertIn("so****ne@example.com", sent["Subject"])

    def test_implicit_tls_on_port_465(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "465",
            "SMTP_USERNAME": "notify@mangoqwq.com",
            "SMTP_PASSWORD": "secret",
            "EMAIL_TO": "a@example.com",
        })
        reload_app_config()
        with patch("app.smtplib.SMTP_SSL") as mock_smtp_cls:
            instance = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = instance
            app.send_email_message("🎉", "续期成功", "user")
        mock_smtp_cls.assert_called_once_with("smtp.example.com", 465, timeout=15)
        instance.login.assert_called_once_with("notify@mangoqwq.com", "secret")

    def test_anonymous_relay_without_credentials(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "EMAIL_TO": "a@example.com",
        })
        reload_app_config()
        with patch("app.smtplib.SMTP") as mock_smtp_cls:
            instance = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = instance
            app.send_email_message("🎉", "续期成功", "user")
        instance.login.assert_not_called()

    def test_smtp_failure_does_not_raise(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "notify@mangoqwq.com",
            "SMTP_PASSWORD": "secret",
            "EMAIL_TO": "a@example.com",
        })
        reload_app_config()
        with patch("app.smtplib.SMTP", side_effect=smtplib.SMTPException("relay denied")):
            # 不应抛异常中断续期主流程
            app.send_email_message("🎉", "续期成功", "user")

    def test_email_message_structure(self):
        os.environ.update({
            "SMTP_HOST": "smtp.example.com",
            "SMTP_USERNAME": "notify@mangoqwq.com",
            "SMTP_PASSWORD": "secret",
            "EMAIL_TO": "notify@mangoqwq.com",
            "EMAIL_SUBJECT_PREFIX": "[Vycebump]",
        })
        reload_app_config()
        captured = {}

        def fake_send(msg):
            captured["msg"] = msg

        with patch("app.smtplib.SMTP") as mock_smtp_cls:
            instance = MagicMock()
            mock_smtp_cls.return_value.__enter__.return_value = instance
            instance.send_message.side_effect = fake_send
            app.send_email_message("🎉", "续期成功", "someone@example.com", details={"server_name": "vps-mini"})

        msg = captured["msg"]
        self.assertIsInstance(msg, email.message.EmailMessage)
        self.assertEqual(msg["From"], "notify@mangoqwq.com")
        self.assertIn("[Vycebump]", msg["Subject"])
        self.assertTrue(msg.is_multipart())
        parts = list(msg.walk())
        content_types = {part.get_content_type() for part in parts}
        self.assertIn("text/plain", content_types)
        self.assertIn("text/html", content_types)


def reload_app_config():
    """重新读取模块级 SMTP/EMAIL 配置常量 (与 app.py 保持一致的解析逻辑)。"""
    app.SMTP_HOST = os.environ.get("SMTP_HOST") or ""
    app.SMTP_PORT = int(os.environ.get("SMTP_PORT") or "0") or 0
    app.SMTP_USERNAME = os.environ.get("SMTP_USERNAME") or ""
    app.SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD") or ""
    app.EMAIL_FROM = os.environ.get("EMAIL_FROM") or ""
    app.EMAIL_TO = [addr.strip() for addr in os.environ.get("EMAIL_TO", "").split(",") if addr.strip()]
    app.EMAIL_SUBJECT_PREFIX = os.environ.get("EMAIL_SUBJECT_PREFIX") or "[Vycebump]"


def restore_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
