import os
import unittest

os.environ.setdefault("AUTH_PASSWORD", "test-password")
os.environ.setdefault("AUTH_SECRET", "test-secret")

import auth
from fastapi import HTTPException


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.original_service_token = auth.KB_SERVICE_TOKEN
        auth.KB_SERVICE_TOKEN = "service-secret"

    def tearDown(self):
        auth.KB_SERVICE_TOKEN = self.original_service_token

    def test_service_token_accepts_x_header_value(self):
        identity = auth.require_auth_or_service_token(x_kb_service_token="service-secret")

        self.assertEqual(identity, {"sub": "service", "scope": "service"})

    def test_service_token_accepts_bearer_authorization(self):
        identity = auth.require_auth_or_service_token(authorization="Bearer service-secret")

        self.assertEqual(identity, {"sub": "service", "scope": "service"})

    def test_service_token_rejects_wrong_value(self):
        with self.assertRaises(HTTPException) as ctx:
            auth.require_auth_or_service_token(x_kb_service_token="wrong")

        self.assertEqual(ctx.exception.status_code, 401)

    def test_cookie_auth_still_works(self):
        token = auth.create_token()

        identity = auth.require_auth_or_service_token(token=token)

        self.assertEqual(identity["sub"], "user")

    def test_blank_config_does_not_accept_service_token(self):
        auth.KB_SERVICE_TOKEN = ""

        with self.assertRaises(HTTPException) as ctx:
            auth.require_auth_or_service_token(x_kb_service_token="service-secret")

        self.assertEqual(ctx.exception.status_code, 401)


class PasswordTests(unittest.TestCase):
    def setUp(self):
        self.original = auth.AUTH_PASSWORD
        auth.AUTH_PASSWORD = "s3cr3t"

    def tearDown(self):
        auth.AUTH_PASSWORD = self.original

    def test_correct_password_strips_whitespace(self):
        self.assertTrue(auth.verify_password("  s3cr3t  "))

    def test_wrong_password(self):
        self.assertFalse(auth.verify_password("nope"))

    def test_non_ascii_password_does_not_raise(self):
        auth.AUTH_PASSWORD = "пароль密码"
        self.assertTrue(auth.verify_password("пароль密码"))
        self.assertFalse(auth.verify_password("wrong"))


class LoginRateLimitTests(unittest.TestCase):
    def setUp(self):
        self.original_max = auth.LOGIN_MAX_ATTEMPTS
        auth.LOGIN_MAX_ATTEMPTS = 3
        auth._login_attempts.clear()

    def tearDown(self):
        auth.LOGIN_MAX_ATTEMPTS = self.original_max
        auth._login_attempts.clear()

    def test_blocks_after_max_failures_and_clears_on_success(self):
        ip = "203.0.113.7"
        for _ in range(3):
            self.assertFalse(auth.login_rate_limited(ip))
            auth.record_failed_login(ip)
        self.assertTrue(auth.login_rate_limited(ip))  # 4th attempt blocked
        auth.clear_login_attempts(ip)                 # successful login resets
        self.assertFalse(auth.login_rate_limited(ip))

    def test_other_ips_unaffected(self):
        for _ in range(3):
            auth.record_failed_login("203.0.113.7")
        self.assertTrue(auth.login_rate_limited("203.0.113.7"))
        self.assertFalse(auth.login_rate_limited("198.51.100.2"))


if __name__ == "__main__":
    unittest.main()
