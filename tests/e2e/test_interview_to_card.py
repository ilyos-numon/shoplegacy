"""E2E: one interview -> draft card -> review -> publish, with PII masking checks."""

import httpx

SHOP = "가나다식당"
OWNER = "홍길동"
PHONE = "010-0000-0000"
SUPPLIER = "라마바정육"
SECRETS = (SHOP, OWNER, PHONE, SUPPLIER)
AXES = ("location", "suppliers", "menu", "pricing", "customers", "failures")
MAX_ANSWERS = 40
ANSWER = f"{SHOP} {OWNER} {PHONE} {SUPPLIER}"


def _create_shop(base: str, headers: dict[str, str]) -> str:
    res = httpx.post(
        f"{base}/api/shops",
        json={
            "name": SHOP,
            "owner_name": OWNER,
            "trade": "restaurant",
            "area_type": "residential",
            "years": 18,
            "closed_year": 2025,
            "supplier_names": [SUPPLIER],
        },
        timeout=10,
        headers=headers,
    )
    assert res.status_code == 201, res.text
    return str(res.json()["shop_id"])


def _start(base: str, headers: dict[str, str], shop_id: str) -> str:
    res = httpx.post(f"{base}/api/sessions", json={"shop_id": shop_id}, timeout=10, headers=headers)
    assert res.status_code == 201, res.text
    return str(res.json()["session_id"])


def _finish_interview(base: str, headers: dict[str, str]) -> str:
    """Register a shop, answer until the tree ends, end the session; return card_id."""
    session_id = _start(base, headers, _create_shop(base, headers))
    for _ in range(MAX_ANSWERS):
        res = httpx.post(
            f"{base}/api/sessions/{session_id}/answer",
            json={"text": ANSWER},
            timeout=10,
            headers=headers,
        )
        assert res.status_code == 200, res.text
        if res.json()["next_question"] is None:
            break
    else:
        raise AssertionError(f"next_question never became null in {MAX_ANSWERS} answers")
    res = httpx.post(f"{base}/api/sessions/{session_id}/end", timeout=30, headers=headers)
    assert res.status_code == 200, res.text
    return str(res.json()["card_id"])


def test_interview_to_published_card(server_url: str, auth_headers: dict[str, str]) -> None:
    card_id = _finish_interview(server_url, auth_headers)

    card_res = httpx.get(f"{server_url}/api/cards/{card_id}", timeout=10, headers=auth_headers)
    assert card_res.status_code == 200
    card = card_res.json()
    assert card["status"] == "draft"
    assert set(card["sections"]) == set(AXES)
    for axis in AXES:
        assert card["sections"][axis].strip(), f"axis {axis} is empty"
    for secret in SECRETS:
        assert secret not in card_res.text
    assert card["warnings"] == []

    assert httpx.get(f"{server_url}/cards/{card_id}", timeout=10).status_code == 404

    put = httpx.put(
        f"{server_url}/api/cards/{card_id}",
        json={"sections": card["sections"]},
        timeout=10,
        headers=auth_headers,
    )
    assert put.status_code == 200, put.text
    publish = httpx.post(
        f"{server_url}/api/cards/{card_id}/publish", timeout=10, headers=auth_headers
    )
    assert publish.status_code == 200, publish.text

    page = httpx.get(f"{server_url}/cards/{card_id}", timeout=10)
    assert page.status_code == 200
    api = httpx.get(f"{server_url}/api/cards/{card_id}", timeout=10, headers=auth_headers)
    assert api.json()["status"] == "published"
    for secret in SECRETS:
        assert secret not in page.text
        assert secret not in api.text


def test_session_for_unknown_shop_is_404(server_url: str, auth_headers: dict[str, str]) -> None:
    res = httpx.post(
        f"{server_url}/api/sessions",
        json={"shop_id": "no-such-shop"},
        timeout=10,
        headers=auth_headers,
    )
    assert res.status_code == 404


def test_blank_answer_is_422(server_url: str, auth_headers: dict[str, str]) -> None:
    session_id = _start(server_url, auth_headers, _create_shop(server_url, auth_headers))
    res = httpx.post(
        f"{server_url}/api/sessions/{session_id}/answer",
        json={"text": "   "},
        timeout=10,
        headers=auth_headers,
    )
    assert res.status_code == 422


def test_publish_with_landmark_is_blocked_with_409(
    server_url: str, auth_headers: dict[str, str]
) -> None:
    card_id = _finish_interview(server_url, auth_headers)
    sections = httpx.get(
        f"{server_url}/api/cards/{card_id}", timeout=10, headers=auth_headers
    ).json()["sections"]
    sections["location"] = "행복아파트 앞이었어요"
    put = httpx.put(
        f"{server_url}/api/cards/{card_id}",
        json={"sections": sections},
        timeout=10,
        headers=auth_headers,
    )
    assert put.status_code == 200, put.text

    res = httpx.post(f"{server_url}/api/cards/{card_id}/publish", timeout=10, headers=auth_headers)
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "gate_failed"
    assert "warnings" in res.json()["detail"]["reasons"]
    assert httpx.get(
        f"{server_url}/api/cards/{card_id}", timeout=10, headers=auth_headers
    ).json()["warnings"]
    assert httpx.get(f"{server_url}/cards/{card_id}", timeout=10).status_code == 404
