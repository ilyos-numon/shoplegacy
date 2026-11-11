"""Org dashboard and report API (mounted under ``/api``)."""

from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel

from shoplegacy.app_state import State, get_state
from shoplegacy.dashboard import report_csv, report_xlsx
from shoplegacy.routes_orgs import PathAdminDep

router = APIRouter()
StateDep = Annotated[State, Depends(get_state)]

DEFAULT_PERIOD_DAYS = 30
_MEDIA_TYPES = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class ConsentCounts(BaseModel):
    granted: int
    revoked: int


class DashboardStatsOut(BaseModel):
    period_from: date
    period_to: date
    shops: int
    interviews: int
    published_cards: int
    cards_by_status: dict[str, int]
    consents: dict[str, ConsentCounts]


def _today() -> date:
    return datetime.now(UTC).date()


def _period(since: date | None, until: date | None) -> tuple[date, date]:
    """Both dates inclusive; by default the last 30 days ending today (UTC)."""
    end = until if until is not None else _today()
    start = since if since is not None else end - timedelta(days=DEFAULT_PERIOD_DAYS - 1)
    if start > end:
        raise HTTPException(status_code=422, detail="from_after_to")
    return start, end


FromQuery = Annotated[date | None, Query(alias="from")]
ToQuery = Annotated[date | None, Query(alias="to")]


@router.get("/orgs/{org_id}/dashboard")
def dashboard(
    org_id: str,
    _: PathAdminDep,
    state: StateDep,
    since: FromQuery = None,
    until: ToQuery = None,
) -> DashboardStatsOut:
    start, end = _period(since, until)
    stats = state.store.dashboard_stats(org_id, start, end)
    return DashboardStatsOut(
        period_from=stats.period_from,
        period_to=stats.period_to,
        shops=stats.shops,
        interviews=stats.interviews,
        published_cards=stats.published_cards,
        cards_by_status=stats.cards_by_status,
        consents={k: ConsentCounts(**v) for k, v in stats.consents.items()},
    )


@router.get("/orgs/{org_id}/report")
def report(
    org_id: str,
    _: PathAdminDep,
    state: StateDep,
    since: FromQuery = None,
    until: ToQuery = None,
    fmt: Annotated[Literal["csv", "xlsx"], Query(alias="format")] = "csv",
) -> Response:
    start, end = _period(since, until)
    rows = state.store.report_rows(org_id, start, end, state.settings.report_max_rows)
    body = report_csv(rows) if fmt == "csv" else report_xlsx(rows)
    filename = f"report-{start.isoformat()}-{end.isoformat()}.{fmt}"
    return Response(
        content=body,
        media_type=_MEDIA_TYPES[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
