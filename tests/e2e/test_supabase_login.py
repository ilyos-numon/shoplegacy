"""E2E: a real local Supabase login (ES256 via JWKS) -> onboarding -> shop registration."""

import os
import re
import secrets
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import jwt
import pytest
from playwright.sync_api import Page, expect, sync_playwright

from tests.e2e.conftest import PROJECT_ROOT, _start_server

STATUS_TIMEOUT_S = 20
HTTP_TIMEOUT_S = 10


def _supabase_env() -> tuple[str, str] | None:
    """``(API_URL, ANON_KEY)`` from override env vars or ``supabase status -o env``."""
    url = os.environ.get("SHOPLEGACY_TEST_SUPABASE_URL")
    key = os.environ.get("SHOPLEGACY_TEST_SUPABASE_ANON_KEY")
    if url and key:
        return url.rstrip("/"), key
    try:
        done = subprocess.run(
            ["supabase", "status", "-o", "env"],
            check=False,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=STATUS_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    values: dict[str, str] = {}
    for line in done.stdout.splitlines():
        name, sep, raw = line.partition("=")
        if sep:
            values[name.strip()] = raw.strip().strip('"')
    if "API_URL" not in values or "ANON_KEY" not in values:
        return None
    return values["API_URL"].rstrip("/"), values["ANON_KEY"]


def _running_env() -> tuple[str, str]:
    env = _supabase_env()
    if env is not None:
        try:
            if httpx.get(f"{env[0]}/auth/v1/health", timeout=5).status_code == 200:
                return env
        except httpx.HTTPError:
            pass
    pytest.skip("local supabase not running")


@pytest.fixture
def supabase_server_url(
    tmp_path: Path, app_dsn: str, request: pytest.FixtureRequest
) -> Iterator[str]:
    api_url, anon_key = _running_env()
    extra = {
        "SHOPLEGACY_JWKS_URL": f"{api_url}/auth/v1/.well-known/jwks.json",
        "SHOPLEGACY_SUPABASE_URL": api_url,
        "SHOPLEGACY_SUPABASE_ANON_KEY": anon_key,
    }
    yield from _start_server(tmp_path, app_dsn, request, require_consent=False, extra_env=extra)


@pytest.fixture
def supabase_user_token(supabase_server_url: str) -> str:
    api_url, anon_key = _running_env()
    res = httpx.post(
        f"{api_url}/auth/v1/signup",
        headers={"apikey": anon_key, "Content-Type": "application/json"},
        json={
            "email": f"e2e-{secrets.token_hex(4)}@example.test",
            "password": secrets.token_urlsafe(16),
        },
        timeout=HTTP_TIMEOUT_S,
    )
    assert res.status_code == 200, f"signup failed: {res.status_code}"
    token = str(res.json()["access_token"])
    if jwt.get_unverified_header(token).get("alg") != "ES256":
        pytest.skip("local supabase signs HS256; JWKS path not exercised")
    return token


def _save(page: Page, name: str) -> None:
    out = Path(os.environ.get("ILLY_SHOT_DIR", "tests/e2e/screenshots"))
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    page.screenshot(path=str(path), full_page=True)
    assert path.stat().st_size > 0


def test_supabase_login_onboarding_and_shop(
    supabase_server_url: str, supabase_user_token: str
) -> None:
    base = supabase_server_url
    bearer = {"Authorization": f"Bearer {supabase_user_token}"}

    me = httpx.get(f"{base}/api/me", headers=bearer, timeout=HTTP_TIMEOUT_S)
    assert me.status_code == 200
    assert me.json()["orgs"] == []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1000, "height": 900})
        context.add_init_script(
            f"window.localStorage.setItem('sl_access_token', '{supabase_user_token}');"
        )
        page = context.new_page()
        try:
            page.goto(f"{base}/")
            page.wait_for_url(re.compile(r"/onboarding\?next=%2F$"))
            page.fill("#org-name", "가나다 소상공인지원센터")
            page.click("#create-org")
            page.wait_for_url(f"{base}/")

            page.click("#new-shop-btn")
            page.fill("#shop-name", "가나다식당")
            page.fill("#owner-name", "홍길동")
            page.fill("#years", "18")
            page.fill("#closed-year", "2025")
            page.click("#start-btn")
            page.wait_for_url(re.compile(r"/shops/[^/]+/consent$"))
            page.click("#consent-save")
            page.wait_for_url(re.compile(r"/interview/[^/]+$"))
            expect(page.locator("#question-text")).not_to_have_text(re.compile(r"불러오는 중"))
            _save(page, "supabase-login-onboarding.png")
        finally:
            context.close()
            browser.close()

    after = httpx.get(f"{base}/api/me", headers=bearer, timeout=HTTP_TIMEOUT_S)
    assert after.status_code == 200
    assert len(after.json()["orgs"]) == 1
