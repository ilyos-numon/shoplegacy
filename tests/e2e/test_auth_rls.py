"""E2E: authentication and org isolation (RLS) over HTTP."""

import httpx

SHOP = {
    "name": "가나다식당",
    "owner_name": "홍길동",
    "trade": "restaurant",
    "area_type": "residential",
    "years": 10,
    "closed_year": 2025,
    "supplier_names": [],
}


def _create_shop(base: str, headers: dict[str, str]) -> str:
    res = httpx.post(f"{base}/api/shops", json=SHOP, timeout=10, headers=headers)
    assert res.status_code == 201, res.text
    return str(res.json()["shop_id"])


def test_anonymous_create_shop_is_401(server_url: str) -> None:
    res = httpx.post(f"{server_url}/api/shops", json=SHOP, timeout=10)
    assert res.status_code == 401


def test_other_org_cannot_fetch_shop(
    server_url: str, auth_headers: dict[str, str], second_org: dict[str, str]
) -> None:
    shop_id = _create_shop(server_url, auth_headers)
    own = httpx.get(f"{server_url}/api/shops/{shop_id}", timeout=10, headers=auth_headers)
    assert own.status_code == 200
    other = httpx.get(f"{server_url}/api/shops/{shop_id}", timeout=10, headers=second_org)
    assert other.status_code == 404


def test_other_org_shop_list_excludes_shop(
    server_url: str, auth_headers: dict[str, str], second_org: dict[str, str]
) -> None:
    shop_id = _create_shop(server_url, auth_headers)
    mine = httpx.get(f"{server_url}/api/shops", timeout=10, headers=auth_headers)
    assert shop_id in [s["shop_id"] for s in mine.json()]
    theirs = httpx.get(f"{server_url}/api/shops", timeout=10, headers=second_org)
    assert theirs.status_code == 200
    assert shop_id not in [s["shop_id"] for s in theirs.json()]


def test_interviewer_cannot_put_card_is_403(
    server_url: str, auth_headers: dict[str, str], interviewer_headers: dict[str, str]
) -> None:
    res = httpx.put(
        f"{server_url}/api/cards/any-card",
        json={"sections": {"location": "x"}},
        timeout=10,
        headers=interviewer_headers,
    )
    assert res.status_code == 403
