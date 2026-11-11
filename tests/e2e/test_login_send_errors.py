"""E2E: the login page explains why Supabase refused to send a magic link."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Route, expect, sync_playwright

from tests.e2e.conftest import _start_server

FAKE_SUPABASE = "http://supabase.invalid"


@pytest.fixture
def login_server_url(tmp_path: Path, app_dsn: str, request: pytest.FixtureRequest) -> Iterator[str]:
    extra = {
        "SHOPLEGACY_SUPABASE_URL": FAKE_SUPABASE,
        "SHOPLEGACY_SUPABASE_ANON_KEY": "anon-test-key",
    }
    yield from _start_server(tmp_path, app_dsn, request, require_consent=False, extra_env=extra)


def _send(base: str, status: int, body: dict[str, object]) -> str:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        def otp(route: Route) -> None:
            route.fulfill(
                status=status,
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
                body=json.dumps(body),
            )

        page.route(f"{FAKE_SUPABASE}/auth/v1/otp*", otp)
        page.goto(f"{base}/login")
        page.fill("#email", "owner@example.test")
        page.click("#send-link")
        msg = page.locator("#login-message")
        expect(msg).not_to_have_text("보내는 중입니다…", timeout=15000)
        text = msg.inner_text()
        browser.close()
        return text


def test_repeat_request_shows_wait_seconds(login_server_url: str) -> None:
    text = _send(
        login_server_url,
        429,
        {
            "code": "over_email_send_rate_limit",
            "msg": "For security purposes, you can only request this after 42 seconds.",
        },
    )
    assert "42초 뒤" in text


def test_hourly_cap_shows_rate_limit(login_server_url: str) -> None:
    text = _send(
        login_server_url,
        429,
        {"code": "over_email_send_rate_limit", "msg": "email rate limit exceeded"},
    )
    assert "너무 자주" in text


def test_unknown_error_shows_reason(login_server_url: str) -> None:
    text = _send(login_server_url, 500, {"code": "unexpected_failure", "msg": "boom"})
    assert "보내지 못했습니다" in text
    assert "(" in text
