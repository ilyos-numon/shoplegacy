"""Org dashboard statistics and CSV/XLSX reports."""

import csv
import io
from collections.abc import Sequence
from dataclasses import astuple, dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from openpyxl import Workbook  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from shoplegacy.store import ReportRow

REPORT_COLUMNS: list[str] = [
    "shop_id",
    "trade",
    "region",
    "sessions",
    "last_interview_at",
    "card_status",
    "published_at",
    "consent_recording",
    "consent_storage",
    "consent_ai_processing",
    "consent_publication",
    "owner_decision",
    "created_at",
]

# Spreadsheet apps evaluate cells starting with these as formulas.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True)
class DashboardStats:
    period_from: date
    period_to: date
    shops: int
    interviews: int
    published_cards: int
    cards_by_status: dict[str, int]
    consents: dict[str, dict[str, int]]  # kind -> {"granted": n, "revoked": n}


def _safe_text(value: str) -> str:
    return "'" + value if value.startswith(_FORMULA_PREFIXES) else value


def _utc(value: datetime) -> datetime:
    return value.astimezone(UTC) if value.tzinfo is not None else value


def _csv_cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return _utc(value).isoformat()
    if isinstance(value, int):
        return str(value)
    return _safe_text(str(value))


def _xlsx_cell(value: object) -> object:
    if value is None or isinstance(value, int):
        return value
    if isinstance(value, datetime):
        # Excel has no time zones: write UTC wall-clock time.
        return _utc(value).replace(tzinfo=None)
    return _safe_text(str(value))


def report_csv(rows: Sequence["ReportRow"]) -> bytes:
    """UTF-8 CSV with a BOM (so spreadsheet apps detect the encoding)."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(REPORT_COLUMNS)
    for row in rows:
        writer.writerow([_csv_cell(v) for v in astuple(row)])
    return buf.getvalue().encode("utf-8-sig")


def report_xlsx(rows: Sequence["ReportRow"]) -> bytes:
    """One sheet named "report": the header row, then one row per report row."""
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "report"
    ws.append(REPORT_COLUMNS)
    for row in rows:
        ws.append([_xlsx_cell(v) for v in astuple(row)])
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
