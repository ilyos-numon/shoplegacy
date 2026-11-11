"""Speech-to-text API (mounted under ``/api``)."""

from typing import Annotated

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool

from shoplegacy.app_state import State, get_state
from shoplegacy.auth import InterviewerDep
from shoplegacy.stt import (
    ALLOWED_TYPES,
    AudioTooLargeError,
    Transcript,
    UnsupportedAudioError,
)

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]


@router.post("/stt/transcribe", response_model=Transcript)
async def transcribe(
    state: StateDep, p: InterviewerDep, file: Annotated[UploadFile, File()]
) -> Transcript:
    content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail=f"unsupported audio type: {content_type}")
    limit = state.settings.stt_max_bytes
    audio = await file.read(limit + 1)
    if len(audio) > limit:
        raise HTTPException(status_code=413, detail=f"audio larger than {limit} bytes")
    try:
        return await run_in_threadpool(
            state.stt.transcribe, audio, file.filename or "", content_type
        )
    except UnsupportedAudioError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from None
    except AudioTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None
