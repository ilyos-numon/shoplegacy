import time
import uuid
from collections.abc import Callable
from pathlib import Path

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from shoplegacy.config import load_settings
from shoplegacy.main import create_app
from shoplegacy.stt import FAKE_PLACEHOLDER
from shoplegacy.stt_stream import FakeStreamSTT, Segment, build_stream_factory

OrgTokens = tuple[str, str, str, str]
SECRET = "test-secret"


def _mint(user_id: str, email: str) -> str:
    claims = {
        "sub": user_id,
        "email": email,
        "aud": "authenticated",
        "role": "authenticated",
        "exp": int(time.time()) + 3600,
    }
    return jwt.encode(claims, SECRET, algorithm="HS256")


def _client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    max_seconds: int = 600,
    max_concurrent: int = 4,
) -> TestClient:
    monkeypatch.setenv("SHOPLEGACY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", app_dsn)
    monkeypatch.setenv("SHOPLEGACY_JWT_SECRET", SECRET)
    monkeypatch.setenv("SHOPLEGACY_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_WORKER", "off")
    monkeypatch.setenv("SHOPLEGACY_STT_STREAM_MAX_SECONDS", str(max_seconds))
    monkeypatch.setenv("SHOPLEGACY_STT_STREAM_MAX_CONCURRENT", str(max_concurrent))
    return TestClient(create_app())


def _start(token: str | None, session_id: str, sample_rate: int = 16000) -> dict[str, object]:
    frame: dict[str, object] = {
        "type": "start",
        "session_id": session_id,
        "sample_rate": sample_rate,
    }
    if token is not None:
        frame["token"] = token
    return frame


def _closed_with(receive: Callable[[], object]) -> WebSocketDisconnect:
    with pytest.raises(WebSocketDisconnect) as info:
        for _ in range(10):
            receive()
    return info.value


def test_fake_stream_flush_returns_fixed_sentence() -> None:
    stream = FakeStreamSTT()
    assert stream.flush() == []
    assert stream.feed(b"\x00\x00" * 16000) == []
    segments = stream.flush()
    assert [s.text for s in segments] == [FAKE_PLACEHOLDER]
    assert segments[0].start_s == 0.0
    assert segments[0].end_s == pytest.approx(1.0)
    assert stream.flush() == []


def test_fake_stream_text_is_one_utterance() -> None:
    stream = FakeStreamSTT()
    stream.feed(b"\x00\x00" * 8000)
    assert stream.feed_text("월세가 쌌어요") == [
        Segment(text="월세가 쌌어요", start_s=0.0, end_s=0.5)
    ]


def test_build_stream_factory_fake_gives_new_instance_each_call() -> None:
    settings = load_settings(
        {
            "SHOPLEGACY_STT_PROVIDER": "fake",
            "SHOPLEGACY_DATABASE_URL": "postgresql://unused@localhost/unused",
            "SHOPLEGACY_JWT_SECRET": SECRET,
        }
    )
    factory = build_stream_factory(settings)
    first, second = factory(), factory()
    assert first.provider == "fake"
    assert first is not second


def test_stream_ready_final_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    org_fixture: OrgTokens,
    stt_session: str,
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn)
    with client.websocket_connect("/api/stt/stream") as ws:
        ws.send_json(_start(org_fixture[3], stt_session))
        assert ws.receive_json() == {"type": "ready", "provider": "fake", "max_seconds": 600}
        ws.send_json({"type": "text", "text": "점심 장사가 잘 됐어요"})
        final = ws.receive_json()
        assert final["type"] == "final"
        assert final["text"] == "점심 장사가 잘 됐어요"
        ws.send_bytes(b"\x01\x00" * 16000)
        ws.send_json({"type": "stop"})
        flushed = ws.receive_json()
        assert flushed["type"] == "final"
        assert flushed["text"] == FAKE_PLACEHOLDER
        assert flushed["end_s"] == pytest.approx(1.0)
        assert ws.receive_json() == {"type": "done", "segments": 2}
        closed = _closed_with(ws.receive_json)
    assert closed.code == 1000


def test_stream_without_token_closes_1008(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    org_fixture: OrgTokens,
    stt_session: str,
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn)
    with client.websocket_connect("/api/stt/stream") as ws:
        ws.send_json(_start(None, stt_session))
        closed = _closed_with(ws.receive_json)
    assert closed.code == 1008
    assert closed.reason == "unauthorized"


def test_stream_bad_token_closes_1008(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    org_fixture: OrgTokens,
    stt_session: str,
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn)
    with client.websocket_connect("/api/stt/stream") as ws:
        ws.send_json(_start("not-a-jwt", stt_session))
        closed = _closed_with(ws.receive_json)
    assert closed.code == 1008
    assert closed.reason == "unauthorized"


def test_stream_other_org_session_closes_1008(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    pg_dsn: str,
    make_user: Callable[[str, str], str],
    org_fixture: OrgTokens,
    stt_session: str,
) -> None:
    other_org = str(uuid.uuid4())
    email = f"other-{other_org[:8]}@example.test"
    user_id = make_user(pg_dsn, email)
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO public.orgs (org_id, name, created_by) VALUES (%s, %s, %s)",
            (other_org, "other org", user_id),
        )
        conn.execute(
            "INSERT INTO public.org_members (org_id, user_id, role) VALUES (%s, %s, 'owner')",
            (other_org, user_id),
        )
    client = _client(tmp_path, monkeypatch, app_dsn)
    with client.websocket_connect("/api/stt/stream") as ws:
        ws.send_json(_start(_mint(user_id, email), stt_session))
        closed = _closed_with(ws.receive_json)
    assert closed.code == 1008
    assert closed.reason == "forbidden"


def test_stream_wrong_sample_rate_closes_1003(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    org_fixture: OrgTokens,
    stt_session: str,
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn)
    with client.websocket_connect("/api/stt/stream") as ws:
        ws.send_json(_start(org_fixture[3], stt_session, sample_rate=48000))
        closed = _closed_with(ws.receive_json)
    assert closed.code == 1003
    assert closed.reason == "sample_rate"


def test_stream_first_frame_must_be_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    org_fixture: OrgTokens,
    stt_session: str,
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn)
    with client.websocket_connect("/api/stt/stream") as ws:
        ws.send_bytes(b"\x00\x00" * 100)
        closed = _closed_with(ws.receive_json)
    assert closed.code == 1008
    assert closed.reason == "start_required"


def test_stream_large_frame_closes_1009(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    org_fixture: OrgTokens,
    stt_session: str,
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn)
    with client.websocket_connect("/api/stt/stream") as ws:
        ws.send_json(_start(org_fixture[3], stt_session))
        assert ws.receive_json()["type"] == "ready"
        ws.send_bytes(b"\x00" * 65536)
        ws.send_bytes(b"\x00" * 65537)
        closed = _closed_with(ws.receive_json)
    assert closed.code == 1009


def test_stream_three_bad_messages_close_1003(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    org_fixture: OrgTokens,
    stt_session: str,
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn)
    with client.websocket_connect("/api/stt/stream") as ws:
        ws.send_json(_start(org_fixture[3], stt_session))
        assert ws.receive_json()["type"] == "ready"
        ws.send_text("not json")
        assert ws.receive_json() == {"type": "error", "code": "bad_message"}
        ws.send_json({"type": "dance"})
        assert ws.receive_json() == {"type": "error", "code": "bad_message"}
        ws.send_json({"type": "text", "text": "가" * 2001})
        assert ws.receive_json() == {"type": "error", "code": "bad_message"}
        closed = _closed_with(ws.receive_json)
    assert closed.code == 1003


def test_stream_two_bad_messages_keep_going(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    org_fixture: OrgTokens,
    stt_session: str,
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn)
    with client.websocket_connect("/api/stt/stream") as ws:
        ws.send_json(_start(org_fixture[3], stt_session))
        assert ws.receive_json()["type"] == "ready"
        for _ in range(2):
            ws.send_json({"type": "dance"})
            assert ws.receive_json()["code"] == "bad_message"
        ws.send_json({"type": "stop"})
        assert ws.receive_json() == {"type": "done", "segments": 0}
        closed = _closed_with(ws.receive_json)
    assert closed.code == 1000


def test_stream_max_seconds_flushes_and_closes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    org_fixture: OrgTokens,
    stt_session: str,
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn, max_seconds=10)
    with client.websocket_connect("/api/stt/stream") as ws:
        ws.send_json(_start(org_fixture[3], stt_session))
        assert ws.receive_json()["max_seconds"] == 10
        for _ in range(6):  # 6 x 2 s = 12 s of audio
            ws.send_bytes(b"\x00\x00" * 32000)
        final = ws.receive_json()
        assert final["type"] == "final"
        assert final["text"] == FAKE_PLACEHOLDER
        assert ws.receive_json() == {"type": "done", "segments": 1}
        closed = _closed_with(ws.receive_json)
    assert closed.code == 1000
    assert closed.reason == "max_duration"


def test_stream_over_concurrent_limit_closes_1013(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    org_fixture: OrgTokens,
    stt_session: str,
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn, max_concurrent=1)
    with client.websocket_connect("/api/stt/stream") as first:
        first.send_json(_start(org_fixture[3], stt_session))
        assert first.receive_json()["type"] == "ready"
        with client.websocket_connect("/api/stt/stream") as second:
            closed = _closed_with(second.receive_json)
        assert closed.code == 1013
        assert closed.reason == "too_many_streams"
        first.send_json({"type": "stop"})
        assert first.receive_json() == {"type": "done", "segments": 0}
    # The slot is free again once the first stream ends.
    with client.websocket_connect("/api/stt/stream") as third:
        third.send_json(_start(org_fixture[3], stt_session))
        assert third.receive_json()["type"] == "ready"
