"""E2E: CSP nonce, anonymous card view, rate limit behind a proxy, last owner guard."""

import ipaddress
import re
from collections.abc import Callable

import httpx

PublishedCard = Callable[[str, dict[str, str]], str]


def _client_ip(n: int) -> str:
    return str(ipaddress.IPv4Address(0x0A000000 + n))


def test_login_csp_nonce_matches_inline_script(server_url: str) -> None:
    res = httpx.get(f"{server_url}/login", timeout=10)
    assert res.status_code == 200
    policy = res.headers["content-security-policy"]
    header_nonce = re.search(r"'nonce-([^']+)'", policy)
    assert header_nonce is not None, policy
    assert "script-src 'self' 'nonce-" in policy
    body_nonces = re.findall(r'<script[^>]*\bnonce="([^"]+)"', res.text)
    assert body_nonces, "no inline script with a nonce"
    assert set(body_nonces) == {header_nonce.group(1)}


def test_csp_nonce_differs_per_request(server_url: str) -> None:
    first = httpx.get(f"{server_url}/login", timeout=10).headers["content-security-policy"]
    second = httpx.get(f"{server_url}/login", timeout=10).headers["content-security-policy"]
    assert first != second


def test_anonymous_card_hides_internal_fields_reviewer_sees_them(
    server_url: str, auth_headers: dict[str, str], published_card: PublishedCard
) -> None:
    card_id = published_card(server_url, auth_headers)
    anon = httpx.get(f"{server_url}/api/cards/{card_id}", timeout=10)
    assert anon.status_code == 200, anon.text
    body = anon.json()
    assert "quality" not in body
    assert "note" not in body
    assert "feedback" in body
    reviewer = httpx.get(f"{server_url}/api/cards/{card_id}", timeout=10, headers=auth_headers)
    assert reviewer.status_code == 200, reviewer.text
    assert "quality" in reviewer.json()


def test_rate_limit_behind_proxy_counts_each_forwarded_client(server_url_proxy: str) -> None:
    url = f"{server_url_proxy}/api/regions"
    for n in range(4):
        res = httpx.get(url, headers={"X-Forwarded-For": _client_ip(n)}, timeout=10)
        assert res.status_code == 200, f"client {n}: {res.status_code}"


def test_rate_limit_behind_proxy_blocks_same_forwarded_client(server_url_proxy: str) -> None:
    url = f"{server_url_proxy}/api/regions"
    same = {"X-Forwarded-For": _client_ip(200)}
    statuses = [httpx.get(url, headers=same, timeout=10).status_code for _ in range(4)]
    assert statuses == [200, 200, 200, 429]
    other = httpx.get(url, headers={"X-Forwarded-For": _client_ip(201)}, timeout=10)
    assert other.status_code == 200


def test_removing_the_last_owner_is_409(server_url: str, owner_headers: dict[str, str]) -> None:
    org_id = owner_headers["X-Org-Id"]
    members = httpx.get(
        f"{server_url}/api/orgs/{org_id}/members", timeout=10, headers=owner_headers
    )
    assert members.status_code == 200, members.text
    owners = [m for m in members.json() if m["role"] == "owner"]
    assert len(owners) == 1
    res = httpx.delete(
        f"{server_url}/api/orgs/{org_id}/members/{owners[0]['user_id']}",
        timeout=10,
        headers=owner_headers,
    )
    assert res.status_code == 409, res.text
    assert res.json()["detail"] == "last_owner"
