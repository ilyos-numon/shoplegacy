"""Org plan and usage API (mounted under ``/api``)."""

from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from shoplegacy.app_state import State, get_state
from shoplegacy.auth import UserDep
from shoplegacy.billing import (
    PLANS,
    LimitResource,
    Plan,
    PlanLimitError,
    check_limit,
    current_usage,
    plan_of,
)
from shoplegacy.store import PRINCIPAL, Principal, Store

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]


class PlanNumbers(BaseModel):
    shops: int
    llm_calls_month: int
    members: int


class PlanInfo(BaseModel):
    plan: Plan
    limits: PlanNumbers
    usage: PlanNumbers
    period: str


def enforce_limit(store: Store, org_id: str, resource: LimitResource) -> None:
    """``check_limit`` for routes: over the limit becomes 402 ``plan_limit:<resource>``."""
    try:
        check_limit(store, org_id, resource)
    except PlanLimitError as exc:
        raise HTTPException(status_code=402, detail=f"plan_limit:{exc.resource}") from None


async def _path_org_member(org_id: str, p: UserDep, state: StateDep) -> Principal:
    """Any member of the org in the path, set as ``PRINCIPAL`` for the endpoint.

    Async so the ContextVar set here reaches the threadpool that runs the endpoint.
    """
    wanted = org_id.lower()
    memberships = await run_in_threadpool(state.store.my_orgs, p.user_id)
    role = next((m.role for m in memberships if m.org_id.lower() == wanted), None)
    if role is None:
        raise HTTPException(status_code=403, detail="forbidden")
    member = replace(p, org_id=wanted, role=role)
    PRINCIPAL.set(member)
    return member


PathMemberDep = Annotated[Principal, Depends(_path_org_member)]


def _plan_info(store: Store, org_id: str) -> PlanInfo:
    now = datetime.now(UTC)
    plan = plan_of(store.get_org(org_id).plan)
    limits = PLANS[plan]
    today = now.date()
    return PlanInfo(
        plan=plan,
        limits=PlanNumbers(
            shops=limits.shops, llm_calls_month=limits.llm_calls_month, members=limits.members
        ),
        usage=PlanNumbers(
            shops=current_usage(store, org_id, "shops", today),
            llm_calls_month=current_usage(store, org_id, "llm_calls", today),
            members=current_usage(store, org_id, "members", today),
        ),
        period=now.strftime("%Y-%m"),
    )


@router.get("/orgs/{org_id}/plan")
def get_plan(org_id: str, _: PathMemberDep, state: StateDep) -> PlanInfo:
    return _plan_info(state.store, org_id.lower())
