"""E2E: read-only smoke checks against an already running service (local Docker or public URL).

Set ``SHOPLEGACY_SMOKE_URL`` to run. No login, no POST, no external calls.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, sync_playwright

SMOKE_URL = os.environ.get("SHOPLEGACY_SMOKE_URL", "").rstrip("/")
HTTP_TIMEOUT_S = 15

pytestmark = pytest.mark.skipif(not SMOKE_URL, reason="SHOPLEGACY_SMOKE_URL not set")


def _get(path: str) -> httpx.Response:
    return httpx.get(f"{SMOKE_URL}{path}", timeout=HTTP_TIMEOUT_S, follow_redirects=True)


@pytest.fixture
def anon_page() -> Iterator[Page]:
    """Token-less browser page (the shared ``page`` fixture injects a local DB token)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1000, "height": 900})
        pg = context.new_page()
        pg.set_default_timeout(HTTP_TIMEOUT_S * 1000)
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def test_readyz_db_ok() -> None:
    res = _get("/readyz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok", "db": "ok"}


def test_health_ok() -> None:
    assert _get("/health").status_code == 200


def test_home_title(anon_page: Page, shot_dir: Path) -> None:
    anon_page.goto(f"{SMOKE_URL}/")
    assert "가게유산" in anon_page.title()
    anon_page.screenshot(path=str(shot_dir / "smoke-deploy-home.png"), full_page=True)


def test_login_page(anon_page: Page, shot_dir: Path) -> None:
    response = anon_page.goto(f"{SMOKE_URL}/login")
    assert response is not None
    assert response.status == 200
    anon_page.screenshot(path=str(shot_dir / "smoke-deploy-login.png"), full_page=True)


def test_cards_page() -> None:
    res = _get("/cards")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_static_css() -> None:
    res = _get("/static/style.css")
    assert res.status_code == 200
    assert "text/css" in res.headers["content-type"]


def test_unknown_404() -> None:
    assert _get("/no-such-page").status_code == 404
