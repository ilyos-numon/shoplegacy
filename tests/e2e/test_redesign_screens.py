"""E2E: the 16 redesigned screens at phone size (screenshots, axe), no external assets, nav by role."""

import ipaddress
import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect, sync_playwright

from tests.conftest import mint_token

MakeCard = Callable[..., tuple[str, str]]
OnScreen = Callable[[str, Page], None]

# Screens fade and slide in (GSAP); axe and screenshots must see the settled screen:
# full opacity and the main slide (x +-28) finished.
_SETTLED_JS = (
    "() => [document.querySelector('main'), ...document.querySelectorAll('[data-stagger]')]"
    ".every((e) => !e || getComputedStyle(e).opacity === '1')"
    " && ((m) => !m || ['none', 'matrix(1, 0, 0, 1, 0, 0)']"
    ".includes(getComputedStyle(m).transform))(document.querySelector('main'))"
)
_NEXT_FRAMES_JS = (
    "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"
)
_SCROLL_WIDTH_JS = "() => Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)"
SCREEN_NAMES = (
    "login",
    "onboarding",
    "home",
    "consent",
    "interview",
    "interview-building",
    "inbox",
    "review",
    "owner",
    "cards",
    "card",
    "dashboard",
    "members",
    "settings",
    "billing",
    "not-found",
)
FORBIDDEN_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com", "cdnjs.cloudflare.com", "unpkg.com")
ALLOWED_EXTERNAL_HOSTS = {"esm.sh"}  # supabase-js on the login screen


def _settled(pg: Page) -> Page:
    pg.wait_for_function(_SETTLED_JS)
    # Chromium refreshes the scrollable overflow of the cleared slide on the next frames.
    pg.evaluate(_NEXT_FRAMES_JS)
    return pg


def _context(
    browser: Browser, headers: dict[str, str] | None, token: str | None = None
) -> BrowserContext:
    context = browser.new_context(
        viewport={"width": 390, "height": 844}, color_scheme="light", locale="ko-KR"
    )
    bearer = token or (headers["Authorization"].removeprefix("Bearer ") if headers else None)
    if bearer:
        script = f"window.localStorage.setItem('sl_access_token', {json.dumps(bearer)});"
        if headers:
            script += (
                f"window.localStorage.setItem('sl_org_id', {json.dumps(headers['X-Org-Id'])});"
            )
        context.add_init_script(script)
    return context


def _walk(
    base: str,
    on_screen: OnScreen,
    *,
    owner: dict[str, str],
    reviewer: dict[str, str],
    orgless_token: str,
    make_card: MakeCard,
    published_card: Callable[[str, dict[str, str]], str],
    open_session: Callable[[str, dict[str, str]], str],
    on_request: Callable[[str], None],
) -> None:
    """Visit all 16 screens, calling ``on_screen(name, page)`` once each when settled."""
    shop_id, draft_id = make_card(base, reviewer)
    pub_id = published_card(base, reviewer)
    link = httpx.post(
        f"{base}/api/cards/{draft_id}/owner-link",
        json={"phone": None},
        timeout=10,
        headers=reviewer,
    )
    assert link.status_code == 201, link.text
    owner_url = str(link.json()["url"])
    live_session = open_session(base, reviewer)  # third shop: the free plan allows three

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            anon = _context(browser, None)
            orgless = _context(browser, None, token=orgless_token)
            rev = _context(browser, reviewer)
            own = _context(browser, owner)
            for ctx in (anon, orgless, rev, own):
                ctx.on("request", lambda req: on_request(req.url))

            def show(ctx: BrowserContext, name: str, path: str, ready: str | None = None) -> None:
                pg = ctx.new_page()
                pg.goto(f"{base}{path}")
                if ready:
                    expect(pg.locator(ready).first).to_be_visible()
                on_screen(name, _settled(pg))
                pg.close()

            show(anon, "login", "/login", "main")
            show(orgless, "onboarding", "/onboarding", "#org-name")
            show(rev, "home", "/", "#new-shop-btn")
            show(rev, "consent", f"/shops/{shop_id}/consent", "#consent-save")
            building = rev.new_page()
            held: list[Any] = []
            building.route(
                "**/api/sessions/*/end", lambda route: held.append(route)
            )  # keep the card-building view on screen
            building.goto(f"{base}/interview/{live_session}")
            expect(building.locator("#end-btn")).to_be_visible()
            on_screen("interview", _settled(building))
            building.locator("#end-btn").click()
            expect(building.locator("#build")).to_be_visible()
            expect(building.locator("#build li.on").first).to_be_visible()
            on_screen("interview-building", _settled(building))
            for route in held:
                route.abort()
            building.close()

            show(rev, "inbox", "/review", "#inbox-list li[data-card-id]")
            show(rev, "review", f"/review/{draft_id}", "#card-status")
            show(anon, "owner", urlsplit(owner_url).path, "#owner-card")
            show(rev, "cards", "/cards", "main")
            show(rev, "card", f"/cards/{pub_id}", "main")
            show(own, "dashboard", "/dashboard", "#org-subtabs")
            show(own, "members", "/members", "#member-list li")
            show(own, "settings", "/settings", "main")
            show(own, "billing", "/billing", "main")
            show(rev, "not-found", f"/cards/{uuid.uuid4().hex}", "main")
        finally:
            browser.close()


@pytest.fixture
def orgless_token(pg_dsn: str, jwt_secret: str, make_user: Callable[[str, str], str]) -> str:
    email = f"orgless-{uuid.uuid4().hex[:8]}@example.test"
    return mint_token(make_user(pg_dsn, email), email, jwt_secret)


@pytest.fixture
def walk_args(
    owner_headers: dict[str, str],
    reviewer_headers: dict[str, str],
    orgless_token: str,
    make_card: MakeCard,
    published_card: Callable[[str, dict[str, str]], str],
    open_session: Callable[[str, dict[str, str]], str],
) -> dict[str, Any]:
    return {
        "open_session": open_session,
        "owner": owner_headers,
        "reviewer": reviewer_headers,
        "orgless_token": orgless_token,
        "make_card": make_card,
        "published_card": published_card,
    }


def test_mobile_screens_axe_and_screenshots(
    server_url: str,
    walk_args: dict[str, Any],
    axe_check: Callable[[Page], list[dict[str, Any]]],
    shot_dir: Path,
) -> None:
    seen: list[str] = []
    violations: dict[str, list[str]] = {}
    overflow: dict[str, int] = {}

    def on_screen(name: str, pg: Page) -> None:
        seen.append(name)
        width = pg.evaluate(_SCROLL_WIDTH_JS)
        if width > 390:
            overflow[name] = width
        pg.screenshot(path=str(shot_dir / f"m-{name}.png"), full_page=True)
        found = axe_check(pg)
        if found:
            violations[name] = [f"{v['id']} ({v['impact']})" for v in found]

    _walk(server_url, on_screen, on_request=lambda _url: None, **walk_args)
    assert seen == list(SCREEN_NAMES)
    assert violations == {}
    assert overflow == {}  # no sideways scroll at phone width


def _is_local(host: str | None) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def test_no_external_font_or_script_requests(server_url: str, walk_args: dict[str, Any]) -> None:
    hosts: set[str] = set()

    def on_request(url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and not _is_local(parts.hostname):
            hosts.add(str(parts.hostname))

    seen: list[str] = []
    _walk(server_url, lambda name, _pg: seen.append(name), on_request=on_request, **walk_args)
    assert len(seen) == len(SCREEN_NAMES)
    assert not [h for h in hosts if h in FORBIDDEN_HOSTS], sorted(hosts)
    assert hosts <= ALLOWED_EXTERNAL_HOSTS, sorted(hosts)


@contextmanager
def _page(headers: dict[str, str]) -> Iterator[Page]:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = _context(browser, headers)
        try:
            yield context.new_page()
        finally:
            context.close()
            browser.close()


def test_nav_hidden_by_role(
    server_url: str,
    interviewer_headers: dict[str, str],
    reviewer_headers: dict[str, str],
    owner_headers: dict[str, str],
) -> None:
    def nav(pg: Page, name: str) -> Any:
        return pg.locator(f'#app-nav a[data-nav="{name}"]')

    with _page(interviewer_headers) as pg:
        pg.goto(f"{server_url}/")
        expect(nav(pg, "home")).to_be_visible()
        expect(nav(pg, "inbox")).to_be_hidden()
        expect(nav(pg, "dashboard")).to_be_hidden()
    with _page(reviewer_headers) as pg:
        pg.goto(f"{server_url}/")
        expect(nav(pg, "inbox")).to_be_visible()
        expect(nav(pg, "dashboard")).to_be_hidden()
    with _page(owner_headers) as pg:
        pg.goto(f"{server_url}/dashboard")
        expect(nav(pg, "dashboard")).to_be_visible()
        expect(pg.locator('#org-subtabs a[href="/billing"]')).to_be_visible()
