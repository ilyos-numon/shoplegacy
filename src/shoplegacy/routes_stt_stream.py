"""Live speech-to-text WebSocket route.

The token travels in the first (``start``) frame, never in the URL, so it stays
out of access logs.
"""

import asyncio
import json
import threading
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from shoplegacy.app_state import State
from shoplegacy.auth import AuthError, decode_token, has_role
from shoplegacy.store import PRINCIPAL, Principal
from shoplegacy.stt_stream import BYTES_PER_SECOND, SAMPLE_RATE, Segment, UnsupportedTextError

router = APIRouter()

START_TIMEOUT_S = 10.0
MAX_FRAME_BYTES = 65536
MAX_TEXT_CHARS = 2000
MAX_BAD_MESSAGES = 3

CLOSE_NORMAL = 1000
CLOSE_UNSUPPORTED = 1003
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_TRY_AGAIN = 1013


class _Counter:
    """Open streams in this process (per app)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.value = 0

    def enter(self) -> int:
        with self._lock:
            self.value += 1
            return self.value

    def leave(self) -> None:
        with self._lock:
            self.value -= 1


class _Rejected(Exception):
    def __init__(self, code: int, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


def _counter(websocket: WebSocket) -> _Counter:
    app_state = websocket.app.state
    counter: _Counter | None = getattr(app_state, "stt_stream_counter", None)
    if counter is None:
        counter = _Counter()
        app_state.stt_stream_counter = counter
    return counter


def _parse(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def _authorize(state: State, start: dict[str, Any]) -> None:
    """Resolve the caller from the start frame and check the session's org."""
    token = start.get("token")
    if not isinstance(token, str) or not token.strip():
        raise _Rejected(CLOSE_POLICY, "unauthorized")
    try:
        claims = await run_in_threadpool(decode_token, token.strip(), state.settings)
    except AuthError:
        raise _Rejected(CLOSE_POLICY, "unauthorized") from None
    email = claims.get("email")
    user = Principal(
        user_id=claims["sub"],
        email=email if isinstance(email, str) and email else None,
        org_id=None,
        role=None,
        claims=claims,
    )
    PRINCIPAL.set(user)
    memberships = await run_in_threadpool(state.store.my_orgs, user.user_id)
    if not any(has_role(m.role, "interviewer") for m in memberships):
        raise _Rejected(CLOSE_POLICY, "unauthorized")
    session_id = start.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise _Rejected(CLOSE_POLICY, "forbidden")
    row = await run_in_threadpool(state.store.get_session, session_id)
    role = (
        next((m.role for m in memberships if m.org_id.lower() == row.org_id.lower()), None)
        if row is not None
        else None
    )
    if row is None or role is None:
        raise _Rejected(CLOSE_POLICY, "forbidden")
    if not has_role(role, "interviewer"):
        raise _Rejected(CLOSE_POLICY, "unauthorized")
    PRINCIPAL.set(
        Principal(
            user_id=user.user_id, email=user.email, org_id=row.org_id, role=role, claims=claims
        )
    )
    if start.get("sample_rate") != SAMPLE_RATE:
        raise _Rejected(CLOSE_UNSUPPORTED, "sample_rate")


async def _send_finals(websocket: WebSocket, segments: list[Segment]) -> int:
    for segment in segments:
        await websocket.send_json(
            {
                "type": "final",
                "text": segment.text,
                "start_s": segment.start_s,
                "end_s": segment.end_s,
            }
        )
    return len(segments)


@router.websocket("/stt/stream")
async def stt_stream(websocket: WebSocket) -> None:
    state: State = websocket.app.state.shop
    settings = state.settings
    await websocket.accept()
    counter = _counter(websocket)
    try:
        if counter.enter() > settings.stt_stream_max_concurrent:
            await websocket.close(code=CLOSE_TRY_AGAIN, reason="too_many_streams")
            return
        await _run(websocket, state)
    except WebSocketDisconnect:
        pass
    finally:
        counter.leave()


async def _run(websocket: WebSocket, state: State) -> None:
    settings = state.settings
    try:
        first = await asyncio.wait_for(websocket.receive(), START_TIMEOUT_S)
    except TimeoutError:
        await websocket.close(code=CLOSE_POLICY, reason="start_required")
        return
    if first["type"] == "websocket.disconnect":
        return
    start = _parse(first["text"]) if first.get("text") is not None else None
    if start is None or start.get("type") != "start":
        await websocket.close(code=CLOSE_POLICY, reason="start_required")
        return
    try:
        await _authorize(state, start)
    except _Rejected as exc:
        await websocket.close(code=exc.code, reason=exc.reason)
        return

    stream = state.stream_stt_factory()
    await websocket.send_json(
        {
            "type": "ready",
            "provider": stream.provider,
            "max_seconds": settings.stt_stream_max_seconds,
        }
    )
    sent = 0
    received = 0
    bad = 0

    async def finish(reason: str) -> None:
        nonlocal sent
        sent += await _send_finals(websocket, await run_in_threadpool(stream.flush))
        await websocket.send_json({"type": "done", "segments": sent})
        await websocket.close(code=CLOSE_NORMAL, reason=reason)

    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        audio = message.get("bytes")
        if audio is not None:
            if len(audio) > MAX_FRAME_BYTES:
                await websocket.close(code=CLOSE_TOO_BIG, reason="frame_too_large")
                return
            received += len(audio)
            sent += await _send_finals(websocket, await run_in_threadpool(stream.feed, audio))
            if received / BYTES_PER_SECOND > settings.stt_stream_max_seconds:
                await finish("max_duration")
                return
            continue
        data = _parse(message.get("text") or "")
        kind = data.get("type") if data is not None else None
        if data is not None and kind == "stop":
            await finish("")
            return
        text = data.get("text") if data is not None else None
        if kind == "text" and isinstance(text, str) and len(text) <= MAX_TEXT_CHARS:
            try:
                segments = await run_in_threadpool(stream.feed_text, text)
            except UnsupportedTextError:
                await websocket.send_json({"type": "error", "code": "text_not_supported"})
                continue
            sent += await _send_finals(websocket, segments)
            continue
        bad += 1
        await websocket.send_json({"type": "error", "code": "bad_message"})
        if bad >= MAX_BAD_MESSAGES:
            await websocket.close(code=CLOSE_UNSUPPORTED, reason="bad_message")
            return
