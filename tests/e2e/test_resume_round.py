"""E2E (API): resume a shop for a second round, round limit, shop list."""

import httpx

SHOP = "가나다식당"
OWNER = "홍길동"
PHONE = "010-0000-0000"
SUPPLIER = "라마바정육"
SECRETS = (SHOP, OWNER, PHONE, SUPPLIER)
ANSWER = f"{SHOP} {OWNER} {PHONE} {SUPPLIER}"
ROUND_LIMIT = 5


def _create_shop(
    base: str, headers: dict[str, str], name: str = SHOP, area_type: str = "residential"
) -> str:
    res = httpx.post(
        f"{base}/api/shops",
        json={
            "name": name,
            "owner_name": OWNER,
            "trade": "restaurant",
            "area_type": area_type,
            "years": 18,
            "closed_year": 2025,
            "supplier_names": [SUPPLIER],
        },
        timeout=10,
        headers=headers,
    )
    assert res.status_code == 201, res.text
    return str(res.json()["shop_id"])


def _start(base: str, headers: dict[str, str], shop_id: str) -> httpx.Response:
    return httpx.post(
        f"{base}/api/sessions", json={"shop_id": shop_id}, timeout=10, headers=headers
    )


def _short_round(base: str, headers: dict[str, str], shop_id: str) -> tuple[str, str, str]:
    """One short round: main + follow-up answer, then end. Returns (session_id, first_q_id, card_id)."""
    res = _start(base, headers, shop_id)
    assert res.status_code == 201, res.text
    started = res.json()
    session_id = str(started["session_id"])
    for _ in range(2):
        ans = httpx.post(
            f"{base}/api/sessions/{session_id}/answer",
            json={"text": ANSWER},
            timeout=10,
            headers=headers,
        )
        assert ans.status_code == 200, ans.text
        assert ans.json()["next_question"] is not None, ans.text
    end = httpx.post(f"{base}/api/sessions/{session_id}/end", timeout=30, headers=headers)
    assert end.status_code == 200, end.text
    return session_id, str(started["question"]["id"]), str(end.json()["card_id"])


def _progress(base: str, headers: dict[str, str], shop_id: str) -> httpx.Response:
    return httpx.get(f"{base}/api/shops/{shop_id}/progress", timeout=10, headers=headers)


def test_second_round_after_short_first_round(
    server_url: str, auth_headers: dict[str, str]
) -> None:
    shop_id = _create_shop(server_url, auth_headers)
    _, first_q, card_id = _short_round(server_url, auth_headers, shop_id)

    res = _progress(server_url, auth_headers, shop_id)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sessions_done"] == 1
    assert body["next_number"] == 2
    assert body["open_session_id"] is None
    assert body["nodes_done"] == 1
    assert body["can_continue"] is True
    assert body["reason"] == "ok"
    assert body["card_id"] == card_id
    assert body["card_status"] == "draft"
    for secret in SECRETS:
        assert secret not in res.text

    second = _start(server_url, auth_headers, shop_id)
    assert second.status_code == 201, second.text
    data = second.json()
    assert data["number"] == 2
    assert data["question"]["id"] != first_q
    for secret in SECRETS:
        assert secret not in second.text


def test_round_limit_blocks_sixth_session(server_url: str, auth_headers: dict[str, str]) -> None:
    shop_id = _create_shop(server_url, auth_headers)
    for _ in range(ROUND_LIMIT):
        assert _start(server_url, auth_headers, shop_id).status_code == 201

    res = _progress(server_url, auth_headers, shop_id)
    assert res.status_code == 200, res.text
    assert res.json()["sessions_done"] == ROUND_LIMIT
    assert res.json()["can_continue"] is False
    assert res.json()["reason"] == "limit"

    assert _start(server_url, auth_headers, shop_id).status_code == 409


def test_progress_unknown_shop_is_404(server_url: str, auth_headers: dict[str, str]) -> None:
    assert _progress(server_url, auth_headers, "none").status_code == 404


def test_shop_list_newest_first_without_secrets(
    server_url: str, auth_headers: dict[str, str]
) -> None:
    first = _create_shop(server_url, auth_headers)
    second = _create_shop(server_url, auth_headers, area_type="station")
    res = httpx.get(f"{server_url}/api/shops", timeout=10, headers=auth_headers)
    assert res.status_code == 200, res.text
    shops = res.json()
    assert len(shops) >= 2
    assert shops[0]["shop_id"] == second
    assert first in {s["shop_id"] for s in shops}
    for secret in SECRETS:
        assert secret not in res.text
