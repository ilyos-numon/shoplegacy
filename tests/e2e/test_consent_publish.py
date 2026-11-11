"""E2E: publication consent gate on a strict server (SHOPLEGACY_REQUIRE_CONSENT=true)."""

from collections.abc import Callable

import httpx

MakeCard = Callable[[str, dict[str, str]], tuple[str, str]]


def _grant(base: str, headers: dict[str, str], shop_id: str) -> str:
    res = httpx.post(
        f"{base}/api/shops/{shop_id}/consents",
        json={"kind": "publication", "method": "verbal", "note": "e2e"},
        timeout=10,
        headers=headers,
    )
    assert res.status_code == 201, res.text
    return str(res.json()["consent_id"])


def _publish(base: str, headers: dict[str, str], card_id: str) -> httpx.Response:
    return httpx.post(f"{base}/api/cards/{card_id}/publish", timeout=10, headers=headers)


def test_publish_without_consent_is_409(
    server_url_strict: str, auth_headers: dict[str, str], make_card: MakeCard
) -> None:
    _, card_id = make_card(server_url_strict, auth_headers)
    res = _publish(server_url_strict, auth_headers, card_id)
    assert res.status_code == 409, res.text
    assert "consent_missing:publication" in res.json()["detail"]["reasons"]


def test_publish_with_consent_is_200(
    server_url_strict: str, auth_headers: dict[str, str], make_card: MakeCard
) -> None:
    shop_id, card_id = make_card(server_url_strict, auth_headers)
    _grant(server_url_strict, auth_headers, shop_id)
    res = _publish(server_url_strict, auth_headers, card_id)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "published"


def test_revoked_consent_allows_unpublish_and_hides_card(
    server_url_strict: str, auth_headers: dict[str, str], make_card: MakeCard
) -> None:
    base = server_url_strict
    shop_id, card_id = make_card(base, auth_headers)
    consent_id = _grant(base, auth_headers, shop_id)
    assert _publish(base, auth_headers, card_id).status_code == 200
    listed = httpx.get(f"{base}/api/cards?status=published", timeout=10).json()
    assert card_id in [c["card_id"] for c in listed]

    revoke = httpx.post(
        f"{base}/api/consents/{consent_id}/revoke",
        json={"reason": "owner asked"},
        timeout=10,
        headers=auth_headers,
    )
    assert revoke.status_code == 200, revoke.text
    unpub = httpx.post(
        f"{base}/api/cards/{card_id}/status",
        json={"status": "unpublished", "note": "consent revoked"},
        timeout=10,
        headers=auth_headers,
    )
    assert unpub.status_code == 200, unpub.text
    assert unpub.json()["status"] == "unpublished"
    listed = httpx.get(f"{base}/api/cards?status=published", timeout=10).json()
    assert card_id not in [c["card_id"] for c in listed]
    assert httpx.get(f"{base}/cards/{card_id}", timeout=10).status_code == 404
