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

        self.assertEqual(identity, {"sub": "service", "scope": "kb:read"})

    def test_service_token_accepts_bearer_authorization(self):
        identity = auth.require_auth_or_service_token(authorization="Bearer service-secret")

        self.assertEqual(identity, {"sub": "service", "scope": "kb:read"})

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


if __name__ == "__main__":
    unittest.main()
