"""E2E: org members (list, add, remove) and org settings (name, retention, approval switch)."""

import base64
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, expect, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Screens fade and slide in (GSAP); axe must judge the settled screen, not a frame mid-fade.
_SETTLED_JS = (
    "() => [document.querySelector('main'), ...document.querySelectorAll('[data-stagger]')]"
    ".every((e) => !e || getComputedStyle(e).opacity === '1')"
)


def _settled(pg: Page) -> Page:
    pg.wait_for_function(_SETTLED_JS)
    return pg


@contextmanager
def _mobile_page(headers: dict[str, str]) -> Iterator[Page]:
    token = headers["Authorization"].removeprefix("Bearer ")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 390, "height": 844})
        context.add_init_script(
            f"window.localStorage.setItem('sl_access_token', {json.dumps(token)});"
            f"window.localStorage.setItem('sl_org_id', {json.dumps(headers['X-Org-Id'])});"
        )
        pg = context.new_page()
        try:
            yield pg
        finally:
            context.close()
            browser.close()


def _claims(headers: dict[str, str]) -> dict[str, Any]:
    token = headers["Authorization"].removeprefix("Bearer ")
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload))
    return claims


def _set_plan(pg_dsn: str, org_id: str, plan: str) -> None:
    env = {**os.environ, "SHOPLEGACY_DATABASE_URL": pg_dsn}
    done = subprocess.run(
        [sys.executable, "-m", "shoplegacy.maintenance", "set-plan", org_id, plan],
        check=False,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr


def test_members_add_and_remove(
    server_url: str,
    owner_headers: dict[str, str],
    second_org: dict[str, str],
    pg_dsn: str,
    axe_check: Callable[[Page], list[dict[str, Any]]],
    shot_dir: Path,
) -> None:
    newcomer = _claims(second_org)
    org_id = owner_headers["X-Org-Id"]

    with _mobile_page(owner_headers) as page:
        page.goto(f"{server_url}/members")
        rows = page.locator("#member-list li[data-user-id]")
        # org_fixture seeds owner, reviewer and interviewer.
        expect(rows).to_have_count(3)
        expect(page.locator("#member-list")).to_contain_text("소유자")
        expect(page.locator("#member-list button.member-remove")).to_have_count(3)
        expect(page.locator('#org-subtabs a[href="/members"]')).to_have_attribute(
            "aria-current", "page"
        )
        expect(page.locator('#app-nav a[data-nav="dashboard"]')).to_have_attribute(
            "aria-current", "page"
        )

        # The free plan holds 3 members: the 4th is refused with the plan-limit sentence.
        page.fill("#member-email", newcomer["email"])
        page.select_option("#member-role", "reviewer")
        page.click("#member-add")
        expect(page.locator("#member-error")).to_contain_text("구성원 한도")
        expect(rows).to_have_count(3)

        _set_plan(pg_dsn, org_id, "pilot")
        page.click("#member-add")
        expect(rows).to_have_count(4)
        added = page.locator(f'#member-list li[data-user-id="{newcomer["sub"]}"]')
        expect(added).to_contain_text(newcomer["email"])
        expect(added).to_contain_text("검토자")
        expect(page.locator("#member-email")).to_have_value("")

        api = httpx.get(
            f"{server_url}/api/orgs/{org_id}/members", timeout=10, headers=owner_headers
        )
        assert api.status_code == 200, api.text
        assert newcomer["sub"] in {m["user_id"] for m in api.json()}

        page.wait_for_timeout(700)
        page.screenshot(path=str(shot_dir / "s10-members.png"), full_page=True)
        assert axe_check(_settled(page)) == []

        added.locator("button.member-remove").click()
        added.locator("button.member-remove-confirm").click()
        expect(rows).to_have_count(3)
        expect(page.locator(f'#member-list li[data-user-id="{newcomer["sub"]}"]')).to_have_count(0)

        api = httpx.get(
            f"{server_url}/api/orgs/{org_id}/members", timeout=10, headers=owner_headers
        )
        assert newcomer["sub"] not in {m["user_id"] for m in api.json()}


def test_member_remove_cancel_keeps_member(server_url: str, owner_headers: dict[str, str]) -> None:
    org_id = owner_headers["X-Org-Id"]
    with _mobile_page(owner_headers) as page:
        page.goto(f"{server_url}/members")
        rows = page.locator("#member-list li[data-user-id]")
        expect(rows).to_have_count(3)
        row = rows.last
        user_id = row.get_attribute("data-user-id")
        deletes: list[str] = []
        page.on("request", lambda req: deletes.append(req.url) if req.method == "DELETE" else None)

        row.locator("button.member-remove").click()
        expect(row).to_contain_text("정말 뺄까요?")
        expect(row.locator("button.member-remove-confirm")).to_be_visible()
        expect(row.locator("button.member-remove")).to_be_hidden()

        row.locator("button.member-remove-cancel").click()
        expect(row.locator("button.member-remove-confirm")).to_be_hidden()
        expect(row.locator("button.member-remove")).to_be_visible()
        page.wait_for_timeout(300)
        expect(rows).to_have_count(3)
        assert deletes == []

        api = httpx.get(
            f"{server_url}/api/orgs/{org_id}/members", timeout=10, headers=owner_headers
        )
        assert api.status_code == 200, api.text
        assert user_id in {m["user_id"] for m in api.json()}


def test_members_unknown_email_is_explained(
    server_url: str, owner_headers: dict[str, str], pg_dsn: str
) -> None:
    _set_plan(pg_dsn, owner_headers["X-Org-Id"], "pilot")
    with _mobile_page(owner_headers) as page:
        page.goto(f"{server_url}/members")
        expect(page.locator("#member-list li[data-user-id]")).to_have_count(3)
        page.fill("#member-email", "nobody-here@example.test")
        page.click("#member-add")
        expect(page.locator("#member-error")).to_contain_text("로그인한 사람을 찾지 못했어요")
        expect(page.locator("#member-list li[data-user-id]")).to_have_count(3)


def test_settings_name_retention_and_approval(
    server_url: str,
    owner_headers: dict[str, str],
    axe_check: Callable[[Page], list[dict[str, Any]]],
    shot_dir: Path,
) -> None:
    org_id = owner_headers["X-Org-Id"]
    with _mobile_page(owner_headers) as page:
        page.goto(f"{server_url}/settings")
        name = page.locator("#settings-org-name")
        expect(name).to_have_value("test org")
        expect(name).to_have_attribute("readonly", "")
        expect(page.locator('#org-subtabs a[href="/settings"]')).to_have_attribute(
            "aria-current", "page"
        )

        approval = page.locator("#require-approval")
        expect(approval).to_be_disabled()
        expect(page.locator("main")).to_contain_text("서버 설정으로 정해져 있어요")
        settings = httpx.get(
            f"{server_url}/api/orgs/{org_id}/settings", timeout=10, headers=owner_headers
        )
        assert settings.status_code == 200, settings.text
        if settings.json()["require_owner_approval"]:
            expect(approval).to_be_checked()
        else:
            expect(approval).not_to_be_checked()

        expect(page.locator("#retention-save")).to_be_enabled()
        expect(page.locator("#retention-note")).to_contain_text("다음 정리 때 지워질 가게")

        page.fill("#retention-days", "10")
        page.click("#retention-save")
        expect(page.locator("#retention-msg")).to_contain_text("30일 이상")

        page.fill("#retention-days", "90")
        page.click("#retention-save")
        expect(page.locator("#retention-msg")).to_contain_text("90일로 저장했어요")
        stored = httpx.get(
            f"{server_url}/api/orgs/{org_id}/retention", timeout=10, headers=owner_headers
        )
        assert stored.status_code == 200, stored.text
        assert stored.json()["days"] == 90

        page.wait_for_timeout(700)
        page.screenshot(path=str(shot_dir / "s10-settings.png"), full_page=True)
        assert axe_check(_settled(page)) == []


def test_interviewer_sees_forbidden_notice(
    server_url: str,
    interviewer_headers: dict[str, str],
    axe_check: Callable[[Page], list[dict[str, Any]]],
    shot_dir: Path,
) -> None:
    with _mobile_page(interviewer_headers) as page:
        page.goto(f"{server_url}/members")
        expect(page.locator("#members-error")).to_contain_text("관리자와 소유자만")
        expect(page.locator("#member-add-box")).to_be_hidden()
        expect(page.locator("#member-list li[data-user-id]")).to_have_count(0)
        page.wait_for_timeout(700)
        page.screenshot(path=str(shot_dir / "s10-members-forbidden.png"), full_page=True)
        assert axe_check(_settled(page)) == []

        page.goto(f"{server_url}/settings")
        expect(page.locator("#settings-error")).to_contain_text("관리자와 소유자만")
        expect(page.locator("#retention-save")).to_be_disabled()
