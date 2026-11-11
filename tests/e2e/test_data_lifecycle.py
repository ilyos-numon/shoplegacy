"""E2E: shop export, deletion and audit trail."""

import io
import json
import zipfile
from collections.abc import Callable

import httpx
import psycopg

MakeCard = Callable[..., tuple[str, str]]

EXPECTED_FILES = {
    "shop.json",
    "sessions.json",
    "answers.json",
    "cards.json",
    "card_versions.json",
    "consents.json",
    "owner_reviews.json",
    "audit.json",
}


def test_export_zip_has_eight_files_and_db_holds_ciphertext(
    server_url: str,
    owner_headers: dict[str, str],
    reviewer_headers: dict[str, str],
    make_card: MakeCard,
    pg_dsn: str,
) -> None:
    shop_id, _ = make_card(server_url, reviewer_headers)
    res = httpx.get(f"{server_url}/api/shops/{shop_id}/export", timeout=30, headers=owner_headers)
    assert res.status_code == 200, res.text
    assert res.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        assert set(zf.namelist()) == EXPECTED_FILES
        shop = json.loads(zf.read("shop.json"))
    assert shop["owner_name"] == "홍길동"
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        row = conn.execute("SELECT owner_name FROM shops WHERE shop_id = %s", (shop_id,)).fetchone()
    assert row is not None
    assert str(row[0]).startswith("enc:v1:")
    assert "홍길동" not in str(row[0])
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        audit = conn.execute(
            "SELECT count(*) FROM audit_log WHERE action = 'shop.export' AND row_id = %s",
            (shop_id,),
        ).fetchone()
    assert audit is not None and audit[0] == 1


def test_delete_shop_removes_it_and_keeps_audit(
    server_url: str,
    owner_headers: dict[str, str],
    reviewer_headers: dict[str, str],
    make_card: MakeCard,
    pg_dsn: str,
) -> None:
    shop_id, _ = make_card(server_url, reviewer_headers)
    res = httpx.delete(f"{server_url}/api/shops/{shop_id}", timeout=30, headers=owner_headers)
    assert res.status_code == 204, res.text
    gone = httpx.get(f"{server_url}/api/shops/{shop_id}", timeout=10, headers=owner_headers)
    assert gone.status_code == 404
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*) FROM audit_log WHERE action = 'shop.delete' AND row_id = %s",
            (shop_id,),
        ).fetchone()
    assert row is not None and row[0] == 1


def test_interviewer_cannot_export_or_delete(
    server_url: str,
    reviewer_headers: dict[str, str],
    interviewer_headers: dict[str, str],
    make_card: MakeCard,
) -> None:
    shop_id, _ = make_card(server_url, reviewer_headers)
    export = httpx.get(
        f"{server_url}/api/shops/{shop_id}/export", timeout=10, headers=interviewer_headers
    )
    assert export.status_code == 403
    delete = httpx.delete(
        f"{server_url}/api/shops/{shop_id}", timeout=10, headers=interviewer_headers
    )
    assert delete.status_code == 403
    still = httpx.get(f"{server_url}/api/shops/{shop_id}", timeout=10, headers=reviewer_headers)
    assert still.status_code == 200


def test_export_of_unknown_shop_is_404(server_url: str, owner_headers: dict[str, str]) -> None:
    res = httpx.get(
        f"{server_url}/api/shops/no-such-shop/export", timeout=10, headers=owner_headers
    )
    assert res.status_code == 404
