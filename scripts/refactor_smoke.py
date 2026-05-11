#!/usr/bin/env python3
"""Phase 0 smoke checks for the current API baseline.

Usage:
  AUTH_PASSWORD=... python scripts/refactor_smoke.py
  python scripts/refactor_smoke.py --base-url http://localhost:8000 --password ...

The script uses only the Python standard library so it can run from the host or
inside the API container without installing test dependencies.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str = ""


class ApiClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: int = 10,
    ) -> tuple[int, Any]:
        url = self.base_url + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8")
            parsed: Any
            try:
                parsed = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                parsed = raw
            return exc.code, parsed


def expect(
    results: list[CheckResult],
    name: str,
    condition: bool,
    detail: str = "",
) -> bool:
    results.append(CheckResult(name, "PASS" if condition else "FAIL", detail))
    return condition


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Phase 0 API smoke checks.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("API_BASE_URL", "http://localhost:8000"),
        help="API base URL. Defaults to API_BASE_URL or http://localhost:8000.",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("AUTH_PASSWORD"),
        help="Auth password. Defaults to AUTH_PASSWORD.",
    )
    parser.add_argument(
        "--skip-auth",
        action="store_true",
        help="Skip auth-only checks when AUTH_PASSWORD is not available.",
    )
    args = parser.parse_args()

    client = ApiClient(args.base_url)
    results: list[CheckResult] = []

    try:
        status, payload = client.request("GET", "/api/health")
        expect(
            results,
            "api health",
            status == 200 and isinstance(payload, dict) and payload.get("status") == "ok",
            f"status={status}",
        )

        authenticated = False
        if args.password:
            status, payload = client.request(
                "POST", "/api/auth/login", {"password": args.password}
            )
            authenticated = expect(
                results,
                "auth login",
                status == 200 and isinstance(payload, dict) and payload.get("ok") is True,
                f"status={status}",
            )
            status, payload = client.request("GET", "/api/auth/me")
            authenticated = expect(
                results,
                "auth me",
                authenticated
                and status == 200
                and isinstance(payload, dict)
                and payload.get("ok") is True,
                f"status={status}",
            )
        elif args.skip_auth:
            results.append(CheckResult("auth login", "SKIP", "no password provided"))
            results.append(CheckResult("auth me", "SKIP", "no password provided"))
        else:
            results.append(
                CheckResult("auth login", "FAIL", "AUTH_PASSWORD missing; use --skip-auth")
            )
            authenticated = False

        status, payload = client.request("GET", "/api/sources")
        expect(
            results,
            "sources list",
            status == 200 and isinstance(payload, list),
            f"status={status}",
        )

        status, payload = client.request("GET", "/api/kb/nodes?limit=1")
        expect(
            results,
            "kb nodes list",
            status == 200
            and isinstance(payload, dict)
            and isinstance(payload.get("nodes"), list)
            and "total" in payload,
            f"status={status}",
        )

        status, payload = client.request("GET", "/api/briefing")
        expect(
            results,
            "briefing get",
            status == 200
            and isinstance(payload, dict)
            and "topics" in payload
            and "generated" in payload,
            f"status={status}",
        )

        if authenticated:
            status, payload = client.request("GET", "/api/drafts")
            expect(
                results,
                "drafts list",
                status == 200 and isinstance(payload, list),
                f"status={status}",
            )
        elif args.skip_auth:
            results.append(CheckResult("drafts list", "SKIP", "auth skipped"))
        else:
            results.append(CheckResult("drafts list", "FAIL", "auth unavailable"))

    except (OSError, urllib.error.URLError) as exc:
        results.append(CheckResult("api connection", "FAIL", str(exc)))

    width = max(len(r.name) for r in results) if results else 0
    for result in results:
        suffix = f" - {result.detail}" if result.detail else ""
        print(f"{result.name.ljust(width)}  {result.status}{suffix}")

    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
