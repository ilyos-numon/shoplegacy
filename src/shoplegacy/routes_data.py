"""Shop data export, deletion and retention API (mounted under ``/api``)."""

import dataclasses
import io
import json
import zipfile
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from shoplegacy.app_state import State, get_state
from shoplegacy.auth import AdminDep
from shoplegacy.routes_orgs import PathAdminDep
from shoplegacy.store import ParentNotFoundError, ShopExport

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]

MIN_RETENTION_DAYS = 30


class RetentionUpdate(BaseModel):
    days: int = Field(ge=MIN_RETENTION_DAYS)


class Retention(BaseModel):
    days: int
    next_purge_count: int


def _plain(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _export_zip(export: ShopExport) -> bytes:
    files: dict[str, Any] = {
        "shop.json": export.shop,
        "sessions.json": export.sessions,
        "answers.json": export.answers,
        "cards.json": export.cards,
        "card_versions.json": export.card_versions,
        "consents.json": export.consents,
        "owner_reviews.json": export.owner_reviews,
        "audit.json": export.audit,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, value in files.items():
            text = json.dumps(_plain(value), ensure_ascii=False, indent=2, default=str)
            zf.writestr(name, text)
    return buf.getvalue()


@router.get("/shops/{shop_id}/export")
def export_shop(shop_id: str, p: AdminDep, state: StateDep) -> Response:
    try:
        export = state.store.export_shop(shop_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="shop not found") from None
    if p.org_id is None or export.shop.org_id.lower() != p.org_id.lower():
        raise HTTPException(status_code=404, detail="shop not found")
    content = _export_zip(export)
    state.store.add_audit("shop.export", "shops", shop_id, {"shop_id": shop_id, "by": p.user_id})
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="shop-{shop_id}.zip"'},
    )


@router.delete("/shops/{shop_id}", status_code=204)
def delete_shop(shop_id: str, _: AdminDep, state: StateDep) -> Response:
    try:
        state.store.delete_shop(shop_id)
    except ParentNotFoundError:
        raise HTTPException(status_code=404, detail="shop not found") from None
    return Response(status_code=204)


def _next_purge_count(state: State, org_id: str) -> int:
    """Shops of the org whose retention deadline has already passed."""
    now = datetime.now(UTC)
    wanted = org_id.lower()
    return sum(
        1
        for s in state.store.list_shops()
        if s.org_id.lower() == wanted and s.retention_until is not None and s.retention_until < now
    )


def _retention(state: State, org_id: str, days: int) -> Retention:
    return Retention(days=days, next_purge_count=_next_purge_count(state, org_id))


@router.get("/orgs/{org_id}/retention")
def get_retention(org_id: str, _: PathAdminDep, state: StateDep) -> Retention:
    try:
        org = state.store.get_org(org_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="org not found") from None
    return _retention(state, org_id, org.retention_days)


@router.put("/orgs/{org_id}/retention")
def put_retention(
    org_id: str, body: RetentionUpdate, admin: PathAdminDep, state: StateDep
) -> Retention:
    if admin.role != "owner":
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        org = state.store.update_org(org_id, retention_days=body.days)
    except LookupError:
        raise HTTPException(status_code=404, detail="org not found") from None
    return _retention(state, org_id, org.retention_days)
