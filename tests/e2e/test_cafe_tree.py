"""E2E: trade-specific question trees and area-conditional branch nodes."""

import httpx

AXES = ("location", "suppliers", "menu", "pricing", "customers", "failures")
BRANCH_IDS = {"loc-03", "cus-03"}
MAX_ANSWERS = 40
ANSWER = "손님들이 편하게 드시도록 늘 같은 방식으로 준비했어요."


def _shop(base: str, headers: dict[str, str], trade: str, area_type: str) -> str:
    res = httpx.post(
        f"{base}/api/shops",
        json={
            "name": "가나다식당",
            "owner_name": "홍길동",
            "trade": trade,
            "area_type": area_type,
            "years": 8,
            "closed_year": 2025,
            "supplier_names": [],
        },
        timeout=10,
        headers=headers,
    )
    assert res.status_code == 201, res.text
    return str(res.json()["shop_id"])


def _asked_ids(base: str, headers: dict[str, str], shop_id: str) -> set[str]:
    res = httpx.post(f"{base}/api/sessions", json={"shop_id": shop_id}, timeout=10, headers=headers)
    assert res.status_code == 201, res.text
    session_id = res.json()["session_id"]
    ids = {res.json()["question"]["id"]}
    for _ in range(MAX_ANSWERS):
        ans = httpx.post(
            f"{base}/api/sessions/{session_id}/answer",
            json={"text": ANSWER},
            timeout=10,
            headers=headers,
        )
        assert ans.status_code == 200, ans.text
        nxt = ans.json()["next_question"]
        if nxt is None:
            return ids
        ids.add(nxt["id"])
    raise AssertionError("interview never ended")


def test_cafe_shop_first_question_has_known_axis(
    server_url: str, auth_headers: dict[str, str]
) -> None:
    shop_id = _shop(server_url, auth_headers, "cafe", "residential")
    res = httpx.post(
        f"{server_url}/api/sessions", json={"shop_id": shop_id}, timeout=10, headers=auth_headers
    )
    assert res.status_code == 201, res.text
    assert res.json()["question"]["axis"] in AXES


def test_station_area_reaches_branch_node_residential_does_not(
    server_url: str, auth_headers: dict[str, str]
) -> None:
    station = _asked_ids(
        server_url, auth_headers, _shop(server_url, auth_headers, "restaurant", "station")
    )
    assert station & BRANCH_IDS, f"no branch node asked: {sorted(station)}"
    residential = _asked_ids(
        server_url, auth_headers, _shop(server_url, auth_headers, "restaurant", "residential")
    )
    assert not residential & BRANCH_IDS, f"branch node leaked: {sorted(residential)}"


def test_unknown_trade_is_422(server_url: str, auth_headers: dict[str, str]) -> None:
    res = httpx.post(
        f"{server_url}/api/shops",
        json={
            "name": "x",
            "owner_name": "y",
            "trade": "spaceship",
            "area_type": "other",
            "years": 1,
            "closed_year": 2025,
            "supplier_names": [],
        },
        timeout=10,
        headers=auth_headers,
    )
    assert res.status_code == 422
