"""Shop consent API (mounted under ``/api``)."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from shoplegacy.app_state import State, get_state
from shoplegacy.auth import InterviewerDep, Principal
from shoplegacy.consent import (
    Consent,
    ConsentExistsError,
    ConsentKind,
    ConsentMethod,
    ConsentNotFoundError,
    ConsentRevokedError,
    ConsentService,
)

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]


class ConsentCreate(BaseModel):
    kind: ConsentKind
    method: ConsentMethod
    note: str | None = Field(default=None, max_length=500)


class ConsentRevoke(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


def _own_shop(state: State, p: Principal, shop_id: str) -> None:
    """Other orgs' shops look missing (404)."""
    row = state.store.get_shop(shop_id)
    if row is None or row.org_id != p.org_id:
        raise HTTPException(status_code=404, detail="shop not found")


@router.post("/shops/{shop_id}/consents", status_code=201)
def grant_consent(shop_id: str, body: ConsentCreate, state: StateDep, p: InterviewerDep) -> Consent:
    _own_shop(state, p, shop_id)
    try:
        return ConsentService(state.store).grant(shop_id, body.kind, body.method, body.note)
    except ConsentExistsError:
        raise HTTPException(status_code=409, detail="consent already active") from None


@router.get("/shops/{shop_id}/consents")
def list_consents(shop_id: str, state: StateDep, p: InterviewerDep) -> list[Consent]:
    _own_shop(state, p, shop_id)
    return ConsentService(state.store).list(shop_id)


@router.post("/consents/{consent_id}/revoke")
def revoke_consent(
    consent_id: str, body: ConsentRevoke, state: StateDep, p: InterviewerDep
) -> Consent:
    row = state.store.get_consent(consent_id)
    if row is None or row.org_id != p.org_id:
        raise HTTPException(status_code=404, detail="consent not found")
    try:
        return ConsentService(state.store).revoke(consent_id, body.reason)
    except ConsentNotFoundError:
        raise HTTPException(status_code=404, detail="consent not found") from None
    except ConsentRevokedError:
        raise HTTPException(status_code=409, detail="consent already revoked") from None
