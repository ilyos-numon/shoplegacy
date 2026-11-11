"""Auth config, current user and org/member API (mounted under ``/api``)."""

from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from shoplegacy.app_state import State, get_state
from shoplegacy.auth import UserDep, has_role
from shoplegacy.routes_billing import enforce_limit
from shoplegacy.store import LastOwnerError, Principal, Role

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]


class AuthConfig(BaseModel):
    supabase_url: str
    anon_key: str


class OrgMembership(BaseModel):
    org_id: str
    name: str
    role: Role


class MeResponse(BaseModel):
    user_id: str
    email: str | None
    orgs: list[OrgMembership]


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MemberCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: Role


class Member(BaseModel):
    user_id: str
    email: str | None
    role: Role


class OrgSettings(BaseModel):
    org_id: str
    name: str
    require_owner_approval: bool


@router.get("/auth/config")
def auth_config(state: StateDep) -> AuthConfig:
    s = state.settings
    return AuthConfig(supabase_url=s.supabase_url, anon_key=s.supabase_anon_key)


@router.get("/me")
def me(p: UserDep, state: StateDep) -> MeResponse:
    orgs = [
        OrgMembership(org_id=m.org_id, name=m.name, role=m.role)
        for m in state.store.my_orgs(p.user_id)
    ]
    return MeResponse(user_id=p.user_id, email=p.email, orgs=orgs)


@router.post("/orgs", status_code=201)
def create_org(body: OrgCreate, p: UserDep, state: StateDep) -> OrgMembership:
    org = state.store.create_org(body.name, p.user_id)
    return OrgMembership(org_id=org.org_id, name=org.name, role="owner")


def _path_org_admin(org_id: str, p: UserDep, state: StateDep) -> Principal:
    """Admin (or owner) of the org in the path, regardless of ``X-Org-Id``."""
    wanted = org_id.lower()
    role = next(
        (m.role for m in state.store.my_orgs(p.user_id) if m.org_id.lower() == wanted), None
    )
    if not has_role(role, "admin"):
        raise HTTPException(status_code=403, detail="forbidden")
    return replace(p, org_id=org_id, role=role)


PathAdminDep = Annotated[Principal, Depends(_path_org_admin)]


@router.get("/orgs/{org_id}/settings")
def org_settings(org_id: str, admin: PathAdminDep, state: StateDep) -> OrgSettings:
    wanted = org_id.lower()
    org = next(m for m in state.store.my_orgs(admin.user_id) if m.org_id.lower() == wanted)
    return OrgSettings(
        org_id=org.org_id,
        name=org.name,
        require_owner_approval=state.settings.require_owner_approval,
    )


@router.get("/orgs/{org_id}/members")
def list_members(org_id: str, _: PathAdminDep, state: StateDep) -> list[Member]:
    return [
        Member(user_id=m.user_id, email=m.email, role=m.role)
        for m in state.store.list_members(org_id)
    ]


@router.post("/orgs/{org_id}/members", status_code=201)
def add_member(org_id: str, body: MemberCreate, admin: PathAdminDep, state: StateDep) -> Member:
    if body.role == "owner" and admin.role != "owner":
        raise HTTPException(status_code=403, detail="forbidden")
    user_id = state.store.user_id_by_email(body.email.strip())
    if user_id is None:
        raise HTTPException(status_code=404, detail="user not found")
    members = state.store.list_members(org_id)
    current = next((m.role for m in members if m.user_id == user_id), None)
    if current == "owner" and admin.role != "owner":
        raise HTTPException(status_code=403, detail="forbidden")
    if current is None:
        enforce_limit(state.store, org_id, "members")
    try:
        state.store.upsert_member(org_id, user_id, body.role)
    except LastOwnerError:
        raise HTTPException(status_code=409, detail="last_owner") from None
    email = next((m.email for m in state.store.list_members(org_id) if m.user_id == user_id), None)
    return Member(user_id=user_id, email=email or body.email.strip(), role=body.role)


@router.delete("/orgs/{org_id}/members/{user_id}", status_code=204)
def remove_member(org_id: str, user_id: str, admin: PathAdminDep, state: StateDep) -> Response:
    current = next((m.role for m in state.store.list_members(org_id) if m.user_id == user_id), None)
    if current is None:
        raise HTTPException(status_code=404, detail="member not found")
    if current == "owner" and admin.role != "owner":
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        state.store.remove_member(org_id, user_id)
    except LastOwnerError:
        raise HTTPException(status_code=409, detail="last_owner") from None
    except LookupError:
        raise HTTPException(status_code=404, detail="member not found") from None
    return Response(status_code=204)
