"""Smoke E2E: the real server starts, answers /health and serves the first page."""

from pathlib import Path

import httpx
from playwright.sync_api import Page, expect


def test_health_ok(server_url: str) -> None:
    res = httpx.get(f"{server_url}/health", timeout=5)
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_home_page_shows_title(server_url: str, page: Page, shot_dir: Path) -> None:
    page.goto(f"{server_url}/")
    expect(page.get_by_role("heading", name="가게유산")).to_be_visible()
    expect(page).to_have_title("가게유산")
    page.screenshot(path=str(shot_dir / "smoke-home.png"), full_page=True)


def test_static_css_served(server_url: str) -> None:
    res = httpx.get(f"{server_url}/static/style.css", timeout=5)
    assert res.status_code == 200
    assert "text/css" in res.headers["content-type"]


def test_unknown_path_is_404(server_url: str) -> None:
    res = httpx.get(f"{server_url}/no-such-page", timeout=5)
    assert res.status_code == 404
