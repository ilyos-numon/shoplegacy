"""Operational readiness check (mounted at the root, no prefix)."""

from typing import Annotated

import psycopg
import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from shoplegacy.app_state import State, get_state

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]
log = structlog.get_logger("shoplegacy")


@router.get("/readyz")
def readyz(state: StateDep) -> JSONResponse:
    """200 when the database answers; 503 without the error detail otherwise."""
    try:
        state.store.ping()
    except psycopg.Error as exc:
        log.warning("readyz_db_error", error=type(exc).__name__)
        return JSONResponse(status_code=503, content={"status": "degraded", "db": "error"})
    return JSONResponse(status_code=200, content={"status": "ok", "db": "ok"})
