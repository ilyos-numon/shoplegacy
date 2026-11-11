"""E2E: live transcription over WebSocket and the microphone button."""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page, expect
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

OpenSession = Callable[[str, dict[str, str]], str]
PLACEHOLDER = "녹음 내용을 받아 적었습니다."
RECV_TIMEOUT_S = 10


def _ws_url(base: str) -> str:
    return base.replace("http://", "ws://") + "/api/stt/stream"


def _token(headers: dict[str, str]) -> str:
    return headers["Authorization"].removeprefix("Bearer ")


def _start(session_id: str, token: str | None, rate: int = 16000) -> str:
    frame: dict[str, Any] = {"type": "start", "session_id": session_id, "sample_rate": rate}
    if token is not None:
        frame["token"] = token
    return json.dumps(frame)


def test_start_text_stop_returns_final_and_done(
    server_url: str, auth_headers: dict[str, str], open_session: OpenSession
) -> None:
    session_id = open_session(server_url, auth_headers)
    with connect(_ws_url(server_url)) as ws:
        ws.send(_start(session_id, _token(auth_headers)))
        ready = json.loads(ws.recv(timeout=RECV_TIMEOUT_S))
        assert ready["type"] == "ready"
        assert ready["provider"] == "fake"
        ws.send(json.dumps({"type": "text", "text": "골목 안쪽에 있었어요."}))
        final = json.loads(ws.recv(timeout=RECV_TIMEOUT_S))
        assert final["type"] == "final"
        assert final["text"] == "골목 안쪽에 있었어요."
        ws.send(json.dumps({"type": "stop"}))
        done = json.loads(ws.recv(timeout=RECV_TIMEOUT_S))
        assert done == {"type": "done", "segments": 1}
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=RECV_TIMEOUT_S)
        assert ws.protocol.close_code == 1000


def test_audio_frame_then_stop_yields_placeholder(
    server_url: str, auth_headers: dict[str, str], open_session: OpenSession
) -> None:
    session_id = open_session(server_url, auth_headers)
    with connect(_ws_url(server_url)) as ws:
        ws.send(_start(session_id, _token(auth_headers)))
        assert json.loads(ws.recv(timeout=RECV_TIMEOUT_S))["type"] == "ready"
        ws.send(b"\x00\x01" * 1600)
        ws.send(json.dumps({"type": "stop"}))
        final = json.loads(ws.recv(timeout=RECV_TIMEOUT_S))
        assert final["text"] == PLACEHOLDER
        assert json.loads(ws.recv(timeout=RECV_TIMEOUT_S))["segments"] == 1


def test_start_without_token_closes_1008(
    server_url: str, auth_headers: dict[str, str], open_session: OpenSession
) -> None:
    session_id = open_session(server_url, auth_headers)
    with connect(_ws_url(server_url)) as ws:
        ws.send(_start(session_id, None))
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=RECV_TIMEOUT_S)
        assert ws.protocol.close_code == 1008


def test_other_org_session_closes_1008(
    server_url: str,
    auth_headers: dict[str, str],
    second_org: dict[str, str],
    open_session: OpenSession,
) -> None:
    other = open_session(server_url, second_org)
    with connect(_ws_url(server_url)) as ws:
        ws.send(_start(other, _token(auth_headers)))
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=RECV_TIMEOUT_S)
        assert ws.protocol.close_code == 1008


def test_microphone_button_fills_live_transcript(
    server_url: str,
    open_session: OpenSession,
    auth_headers: dict[str, str],
    mic_page: Page,
    shot_dir: Path,
) -> None:
    session_id = open_session(server_url, auth_headers)
    mic_page.goto(f"{server_url}/interview/{session_id}")
    expect(mic_page.locator("#question-text")).not_to_have_text("질문을 불러오는 중입니다...")
    mic_page.click("#mic-btn")
    expect(mic_page.locator("#mic-status")).to_have_text("듣는 중", timeout=10000)
    expect(mic_page.locator("#mic-btn")).to_have_attribute("aria-pressed", "true")
    mic_page.wait_for_timeout(2000)
    mic_page.click("#mic-btn")
    expect(mic_page.locator("#mic-status")).to_have_text("끝", timeout=10000)
    expect(mic_page.locator("#live-transcript")).to_contain_text(PLACEHOLDER)
    expect(mic_page.locator("#answer-text")).to_have_value(re.compile(PLACEHOLDER))
    mic_page.screenshot(path=str(shot_dir / "stt-live.png"), full_page=True)
