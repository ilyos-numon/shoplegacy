"""E2E: encrypted audio blobs on a real S3 endpoint (local Supabase Storage)."""

import os
import time
from collections.abc import Callable
from typing import Any

import pytest

from shoplegacy.audio_store import FsspecAudioStore
from shoplegacy.crypto import FernetCipher, NullCipher, generate_key
from tests.e2e.test_stt_jobs import _post, _wait_done

OpenSession = Callable[[str, dict[str, str]], str]
SECRET_TEXT = "월세가 너무 올라서 가게를 접었어요."
SEEN_TIMEOUT_S = 5


def test_uploaded_audio_is_removed_from_bucket_when_job_is_done(
    server_url_s3: str,
    auth_headers: dict[str, str],
    open_session: OpenSession,
    s3_bucket: tuple[str, Any],
) -> None:
    bucket, fs = s3_bucket
    org_id = auth_headers["X-Org-Id"]
    session_id = open_session(server_url_s3, auth_headers)
    res = _post(server_url_s3, auth_headers, session_id, data=SECRET_TEXT.encode())
    assert res.status_code == 202, res.text
    job_id = res.json()["job_id"]
    path = f"{bucket}/audio/{org_id}/{job_id}.txt"
    raw = b""
    deadline = time.monotonic() + SEEN_TIMEOUT_S
    while time.monotonic() < deadline and not raw:
        fs.invalidate_cache()
        try:
            if fs.exists(path):
                raw = bytes(fs.cat_file(path))
        except FileNotFoundError:
            continue
    if raw:
        assert "월세".encode() not in raw
    body = _wait_done(server_url_s3, auth_headers, job_id)
    assert body["status"] == "done", body
    assert body["text"] == SECRET_TEXT
    fs.invalidate_cache()
    assert not fs.exists(path)


def test_audio_is_encrypted_at_rest_in_bucket(s3_bucket: tuple[str, Any]) -> None:
    bucket, fs = s3_bucket
    store = FsspecAudioStore(
        f"s3://{bucket}/enc",
        FernetCipher([generate_key()]),
        os.environ["SHOPLEGACY_TEST_S3_ENDPOINT"],
    )
    key = "org-1/job-1.txt"
    plain = SECRET_TEXT.encode()
    store.put(key, plain, "text/plain")
    raw = bytes(fs.cat_file(f"{bucket}/enc/{key}"))
    assert "월세".encode() not in raw
    assert raw != plain
    assert store.get(key) == plain
    store.delete(key)


def test_audio_store_roundtrip_on_s3(s3_bucket: tuple[str, Any]) -> None:
    bucket, _ = s3_bucket
    store = FsspecAudioStore(
        f"s3://{bucket}/unit", NullCipher(), os.environ["SHOPLEGACY_TEST_S3_ENDPOINT"]
    )
    key = "org-1/job-1.wav"
    assert not store.exists(key)
    store.put(key, b"hello-audio", "audio/wav")
    assert store.exists(key)
    assert store.get(key) == b"hello-audio"
    store.delete(key)
    assert not store.exists(key)
    with pytest.raises(KeyError):
        store.get(key)
