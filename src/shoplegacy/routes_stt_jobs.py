"""Queued speech-to-text job routes."""

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from shoplegacy import jobs
from shoplegacy.app_state import State, get_state
from shoplegacy.audio_store import audio_key
from shoplegacy.auth import InterviewerDep
from shoplegacy.store import ParentNotFoundError, Principal, SttJobRow
from shoplegacy.stt import ALLOWED_TYPES

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]

log = structlog.get_logger("shoplegacy")


class SttJobOut(BaseModel):
    job_id: str
    session_id: str
    status: Literal["queued", "running", "done", "failed"]
    text: str | None
    duration_s: float | None
    provider: str | None
    error: str | None
    created_at: datetime
    finished_at: datetime | None


def _out(row: SttJobRow) -> SttJobOut:
    return SttJobOut(
        job_id=row.job_id,
        session_id=row.session_id,
        status=row.status,  # type: ignore[arg-type]
        text=row.text,
        duration_s=row.duration_s,
        provider=row.provider,
        error=row.error,
        created_at=row.created_at,
        finished_at=row.finished_at,
    )


def _own_session(state: State, p: Principal, session_id: str) -> None:
    row = state.store.get_session(session_id)
    if row is None or row.org_id != p.org_id:
        raise HTTPException(status_code=404, detail="session not found")


def _enqueue(
    state: State, p: Principal, session_id: str, audio: bytes, content_type: str
) -> SttJobRow:
    _own_session(state, p, session_id)
    job_id = str(uuid.uuid4())
    key = audio_key(str(p.org_id), job_id, content_type)
    row = SttJobRow(
        job_id=job_id,
        org_id=str(p.org_id),
        session_id=session_id,
        created_by=p.user_id,
        status="queued",
        audio_key=key,
        content_type=content_type,
        bytes=len(audio),
        text=None,
        duration_s=None,
        provider=None,
        error=None,
        created_at=datetime.now(UTC),
        started_at=None,
        finished_at=None,
    )
    state.audio_store.put(key, audio, content_type)
    try:
        state.store.insert_stt_job(row)
    except ParentNotFoundError:
        state.audio_store.delete(key)
        raise HTTPException(status_code=404, detail="session not found") from None
    except Exception:
        state.audio_store.delete(key)
        raise
    try:
        jobs.defer_transcribe(job_id)
    except Exception:
        log.exception("stt_job_defer_failed", job_id=job_id)
        state.store.stt_job_fail(job_id, "queue unavailable")
        state.audio_store.delete(key)
        raise HTTPException(status_code=503, detail="transcription queue unavailable") from None
    return row


@router.post("/stt/jobs", status_code=202, response_model=SttJobOut)
async def create_job(
    state: StateDep,
    p: InterviewerDep,
    file: Annotated[UploadFile, File()],
    session_id: Annotated[str, Form()],
) -> SttJobOut:
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail=f"unsupported audio type: {content_type}")
    limit = state.settings.stt_max_bytes
    audio = await file.read(limit + 1)
    if len(audio) > limit:
        raise HTTPException(status_code=413, detail=f"audio larger than {limit} bytes")
    row = await run_in_threadpool(_enqueue, state, p, session_id, audio, content_type)
    return _out(row)


@router.get("/stt/jobs/{job_id}", response_model=SttJobOut)
def get_job(job_id: str, state: StateDep, p: InterviewerDep) -> SttJobOut:
    row = state.store.get_stt_job(job_id)
    if row is None or row.org_id != p.org_id:
        raise HTTPException(status_code=404, detail="job not found")
    return _out(row)


@router.get("/sessions/{session_id}/stt-jobs", response_model=list[SttJobOut])
def list_session_jobs(session_id: str, state: StateDep, p: InterviewerDep) -> list[SttJobOut]:
    _own_session(state, p, session_id)
    return [_out(row) for row in state.store.list_stt_jobs(session_id)]
