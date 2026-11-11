"""STT API E2E: plain-text passthrough with the fake provider, type and size limits."""

import httpx


def test_text_upload_is_returned_as_is(server_url: str, auth_headers: dict[str, str]) -> None:
    content = "가게는 큰길 뒤쪽 골목에 있었어요."
    res = httpx.post(
        f"{server_url}/api/stt/transcribe",
        files={"file": ("note.txt", content.encode(), "text/plain")},
        timeout=10,
        headers=auth_headers,
    )
    assert res.status_code == 200
    body = res.json()
    assert body["text"] == content
    assert body["provider"] == "fake"


def test_pdf_upload_is_rejected_with_415(server_url: str, auth_headers: dict[str, str]) -> None:
    res = httpx.post(
        f"{server_url}/api/stt/transcribe",
        files={"file": ("doc.pdf", b"%PDF-1.4 fake", "application/pdf")},
        timeout=10,
        headers=auth_headers,
    )
    assert res.status_code == 415


def test_oversized_upload_is_rejected_with_413(
    stt_small_server_url: str, auth_headers: dict[str, str]
) -> None:
    res = httpx.post(
        f"{stt_small_server_url}/api/stt/transcribe",
        files={"file": ("big.txt", b"a" * 2000, "text/plain")},
        timeout=10,
        headers=auth_headers,
    )
    assert res.status_code == 413
