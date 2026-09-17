import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from kb_mcp.app import (
    TokenAuthMiddleware,
    build_http_app,
    build_oauth_mcp,
    build_static_mcp,
    require_allowed_email,
)


TOOLS = Path(__file__).resolve().parents[1] / "kb_mcp" / "tools.py"
EXPECTED_TOOLS = {
    "kb_search",
    "kb_get_node",
    "kb_get_nodes_batch",
    "kb_get_related",
    "kb_timeline",
    "kb_compare",
    "kb_cite",
    "kb_summarize_corpus",
    "get_current_time",
}


class KbMcpServerContractTests(unittest.TestCase):
    def test_static_server_exposes_exact_tool_set(self):
        tools = asyncio.run(build_static_mcp().list_tools())
        self.assertEqual({tool.name for tool in tools}, EXPECTED_TOOLS)

    def test_oauth_server_exposes_same_tool_set(self):
        with tempfile.TemporaryDirectory() as storage_dir:
            env = {
                "GOOGLE_CLIENT_ID": "test.apps.googleusercontent.com",
                "GOOGLE_CLIENT_SECRET": "test-secret",
                "MCP_OAUTH_BASE_URL": "https://mcp.example.com",
                "MCP_OAUTH_JWT_SIGNING_KEY": "j" * 32,
                "MCP_OAUTH_STORAGE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "MCP_OAUTH_STORAGE_DIR": storage_dir,
            }
            with patch.dict(os.environ, env, clear=True):
                mcp = build_oauth_mcp()
                mcp.middleware.clear()
                tools = asyncio.run(mcp.list_tools())
        self.assertEqual({tool.name for tool in tools}, EXPECTED_TOOLS)

    def test_static_and_oauth_tool_schemas_match(self):
        static_tools = asyncio.run(build_static_mcp().list_tools())
        with tempfile.TemporaryDirectory() as storage_dir:
            env = {
                "GOOGLE_CLIENT_ID": "test.apps.googleusercontent.com",
                "GOOGLE_CLIENT_SECRET": "test-secret",
                "MCP_OAUTH_BASE_URL": "https://mcp.example.com",
                "MCP_OAUTH_JWT_SIGNING_KEY": "j" * 32,
                "MCP_OAUTH_STORAGE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "MCP_OAUTH_STORAGE_DIR": storage_dir,
            }
            with patch.dict(os.environ, env, clear=True):
                oauth_mcp = build_oauth_mcp()
                oauth_mcp.middleware.clear()
                oauth_tools = asyncio.run(oauth_mcp.list_tools())

        static_schemas = {tool.name: tool.parameters for tool in static_tools}
        oauth_schemas = {tool.name: tool.parameters for tool in oauth_tools}
        self.assertEqual(static_schemas, oauth_schemas)

    def test_adapter_uses_public_api_prefix(self):
        source = TOOLS.read_text(encoding="utf-8")

        self.assertIn('KB_PUBLIC_PREFIX = os.environ.get("KB_PUBLIC_PREFIX", "/api/kb/v1")', source)
        self.assertNotIn('"/api/kb/search"', source)
        self.assertNotIn('"/api/kb/node/', source)


class StaticTokenMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def _request(self, token: str, headers=()):
        reached_app = False
        messages = []

        async def inner(scope, receive, send):
            nonlocal reached_app
            reached_app = True
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {"type": "http", "method": "GET", "path": "/mcp", "headers": list(headers)}
        await TokenAuthMiddleware(inner, token)(scope, receive, send)
        status = next(message["status"] for message in messages if message["type"] == "http.response.start")
        return status, reached_app

    async def test_blank_config_fails_closed(self):
        self.assertEqual(await self._request(""), (503, False))

    async def test_missing_and_wrong_tokens_are_rejected(self):
        self.assertEqual(await self._request("correct"), (401, False))
        self.assertEqual(
            await self._request("correct", [(b"authorization", b"Bearer wrong")]),
            (401, False),
        )

    async def test_both_supported_headers_are_accepted(self):
        self.assertEqual(
            await self._request("correct", [(b"x-mcp-token", b"correct")]),
            (204, True),
        )
        self.assertEqual(
            await self._request("correct", [(b"authorization", b"Bearer correct")]),
            (204, True),
        )


class OAuthAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def _allowed(self, claims, allowlist="owner@gmail.com"):
        ctx = SimpleNamespace(token=SimpleNamespace(claims=claims))
        with patch.dict(os.environ, {"MCP_ALLOWED_EMAILS": allowlist}, clear=True):
            return await require_allowed_email(ctx)

    async def test_empty_allowlist_fails_closed(self):
        self.assertFalse(await self._allowed({"email": "owner@gmail.com", "email_verified": True}, ""))

    async def test_missing_or_unverified_email_is_rejected(self):
        self.assertFalse(await self._allowed({"email_verified": True}))
        self.assertFalse(await self._allowed({"email": "owner@gmail.com", "email_verified": False}))
        self.assertFalse(await self._allowed({"email": "owner@gmail.com", "email_verified": "yes"}))

    async def test_verified_string_and_normalized_email_are_accepted(self):
        self.assertTrue(
            await self._allowed(
                {"email": " Owner@Gmail.com ", "email_verified": "true"},
                " friend@gmail.com, OWNER@gmail.com ",
            )
        )

    async def test_email_outside_allowlist_is_rejected(self):
        self.assertFalse(await self._allowed({"email": "other@gmail.com", "email_verified": True}))


class OAuthHttpAuthorizationTests(unittest.TestCase):
    def test_http_list_and_call_enforce_email_allowlist(self):
        with tempfile.TemporaryDirectory() as storage_dir:
            env = {
                "GOOGLE_CLIENT_ID": "test.apps.googleusercontent.com",
                "GOOGLE_CLIENT_SECRET": "test-secret",
                "MCP_OAUTH_BASE_URL": "https://mcp.example.com",
                "MCP_OAUTH_JWT_SIGNING_KEY": "j" * 32,
                "MCP_OAUTH_STORAGE_ENCRYPTION_KEY": Fernet.generate_key().decode(),
                "MCP_OAUTH_STORAGE_DIR": storage_dir,
                "MCP_ALLOWED_EMAILS": "owner@gmail.com",
            }
            with patch.dict(os.environ, env, clear=True):
                mcp = build_oauth_mcp()

                async def verify_token(token):
                    email = "owner@gmail.com" if token == "allowed" else "other@gmail.com"
                    return AccessToken(
                        token=token,
                        client_id="test-client",
                        scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
                        claims={"email": email, "email_verified": True},
                    )

                mcp.auth.verify_token = verify_token
                with TestClient(mcp.http_app(path="/mcp")) as client:
                    allowed_session = self._initialize(client, "allowed")
                    allowed_tools = self._rpc(
                        client, "allowed", allowed_session, 2, "tools/list", {}
                    )
                    self.assertEqual(
                        {tool["name"] for tool in allowed_tools["result"]["tools"]},
                        EXPECTED_TOOLS,
                    )

                    denied_session = self._initialize(client, "denied")
                    denied_tools = self._rpc(
                        client, "denied", denied_session, 4, "tools/list", {}
                    )
                    self.assertEqual(denied_tools["result"]["tools"], [])
                    denied_call = self._rpc(
                        client,
                        "denied",
                        denied_session,
                        5,
                        "tools/call",
                        {"name": "get_current_time", "arguments": {}},
                    )
                    self.assertTrue(denied_call["result"]["isError"])
                    self.assertIn(
                        "Authorization failed", denied_call["result"]["content"][0]["text"]
                    )

    @classmethod
    def _initialize(cls, client, bearer):
        response = client.post(
            "/mcp",
            headers=cls._headers(bearer),
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        cls._payload(response)
        session_id = response.headers["mcp-session-id"]
        client.post(
            "/mcp",
            headers=cls._headers(bearer, session_id),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        ).raise_for_status()
        return session_id

    @classmethod
    def _rpc(cls, client, bearer, session_id, request_id, method, params):
        response = client.post(
            "/mcp",
            headers=cls._headers(bearer, session_id),
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
        )
        return cls._payload(response)

    @staticmethod
    def _headers(bearer, session_id=None):
        headers = {
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        return headers

    @staticmethod
    def _payload(response):
        response.raise_for_status()
        data_line = next(line for line in response.text.splitlines() if line.startswith("data: "))
        return json.loads(data_line.removeprefix("data: "))


class ModeConfigurationTests(unittest.TestCase):
    def test_missing_and_unknown_mode_fail_closed(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MCP_MODE"):
                build_http_app()
            with self.assertRaisesRegex(RuntimeError, "MCP_MODE"):
                build_http_app("unknown")

    def test_static_mode_does_not_require_oauth_configuration(self):
        with patch.dict(os.environ, {"MCP_MODE": "static", "MCP_STATIC_TOKEN": "test"}, clear=True):
            self.assertIsInstance(build_http_app(), TokenAuthMiddleware)

    def test_oauth_mode_requires_its_configuration(self):
        with patch.dict(os.environ, {"MCP_MODE": "oauth"}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MCP_OAUTH_JWT_SIGNING_KEY"):
                build_http_app()


if __name__ == "__main__":
    unittest.main()
