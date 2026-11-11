import sys
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pytest
from fastapi.testclient import TestClient

from shoplegacy.config import load_settings
from shoplegacy.main import create_app
from shoplegacy.stt import (
    ALLOWED_TYPES,
    FakeSTT,
    UnsupportedAudioError,
    WhisperSTT,
    build_stt,
)

T = TypeVar("T")

DB_ENV = {
    "SHOPLEGACY_DATABASE_URL": "postgresql://unused@localhost/unused",
    "SHOPLEGACY_JWT_SECRET": "test-secret",
}


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    headers: dict[str, str],
    max_bytes: int = 1000,
) -> TestClient:
    monkeypatch.setenv("SHOPLEGACY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", app_dsn)
    monkeypatch.setenv("SHOPLEGACY_JWT_SECRET", DB_ENV["SHOPLEGACY_JWT_SECRET"])
    monkeypatch.setenv("SHOPLEGACY_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_MAX_BYTES", str(max_bytes))
    return TestClient(create_app(), headers=headers)


def test_fake_stt_decodes_utf8_text() -> None:
    result = FakeSTT().transcribe("  월세가 싸서 들어왔어요 \n".encode(), "a.txt", "text/plain")
    assert result.text == "월세가 싸서 들어왔어요"
    assert result.provider == "fake"
    assert result.duration_s is None


def test_fake_stt_binary_returns_placeholder() -> None:
    result = FakeSTT().transcribe(b"\xff\xfe\x00\x81RIFF", "a.wav", "audio/wav")
    assert result.text == "녹음 내용을 받아 적었습니다."
    assert result.provider == "fake"


def test_allowed_types_contract() -> None:
    assert "text/plain" in ALLOWED_TYPES
    assert "audio/wav" in ALLOWED_TYPES
    assert "application/pdf" not in ALLOWED_TYPES


def test_transcribe_endpoint_ok(
    tmp_path: Path,
    app_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn, reviewer_headers)
    response = client.post(
        "/api/stt/transcribe",
        files={"file": ("a.txt", "장사 이야기".encode(), "text/plain")},
    )
    assert response.status_code == 200
    assert response.json() == {"text": "장사 이야기", "duration_s": None, "provider": "fake"}


def test_transcribe_endpoint_accepts_type_parameters(
    tmp_path: Path,
    app_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn, reviewer_headers)
    response = client.post(
        "/api/stt/transcribe",
        files={"file": ("a.webm", b"\x1a\x45\xdf\xa3\xff", "audio/webm;codecs=opus")},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "녹음 내용을 받아 적었습니다."


def test_transcribe_endpoint_rejects_type(
    tmp_path: Path,
    app_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn, reviewer_headers)
    response = client.post(
        "/api/stt/transcribe",
        files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")},
    )
    assert response.status_code == 415


def test_transcribe_endpoint_rejects_size(
    tmp_path: Path,
    app_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn, reviewer_headers, max_bytes=1000)
    response = client.post(
        "/api/stt/transcribe",
        files={"file": ("a.txt", b"a" * 2000, "text/plain")},
    )
    assert response.status_code == 413


def test_build_stt_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings({**DB_ENV, "SHOPLEGACY_STT_PROVIDER": "fake"})
    assert isinstance(build_stt(settings), FakeSTT)


def test_build_stt_whisper_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    settings = load_settings({**DB_ENV, "SHOPLEGACY_STT_PROVIDER": "whisper"})
    with pytest.raises(ValueError) as excinfo:
        build_stt(settings)
    assert str(excinfo.value) == "faster-whisper is not installed; run uv sync --extra stt"


def test_whisper_rejects_text_plain() -> None:
    with pytest.raises(UnsupportedAudioError):
        WhisperSTT("small").transcribe(b"hello", "a.txt", "text/plain")


def test_transcribe_endpoint_runs_in_threadpool(
    tmp_path: Path,
    app_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    reviewer_headers: dict[str, str],
) -> None:
    from shoplegacy import routes_stt

    calls: list[object] = []
    real = routes_stt.run_in_threadpool

    async def spy(func: Callable[..., T], *args: object) -> T:
        calls.append(func)
        return await real(func, *args)

    monkeypatch.setattr(routes_stt, "run_in_threadpool", spy)
    client = _client(tmp_path, monkeypatch, app_dsn, reviewer_headers)
    response = client.post(
        "/api/stt/transcribe",
        files={"file": ("a.txt", "스레드".encode(), "text/plain")},
    )
    assert response.status_code == 200
    assert response.json()["text"] == "스레드"
    assert len(calls) == 1
    assert getattr(calls[0], "__self__", None) is client.app.state.shop.stt  # type: ignore[attr-defined]
