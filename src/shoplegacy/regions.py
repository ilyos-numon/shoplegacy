"""Korean administrative regions (sido and sigungu).

Data: ``data/regions_kr.csv`` next to this module, built by
``scripts/fetch_regions.py`` from the public legal-dong code file (source and
license in ``data/README.md``).
"""

import csv
from dataclasses import dataclass
from functools import cache
from pathlib import Path

REGIONS_CSV = Path(__file__).parent / "data" / "regions_kr.csv"
SEARCH_LIMIT = 50


@dataclass(frozen=True)
class Region:
    code: str
    sido: str
    sigungu: str | None  # None for a sido row

    @property
    def label(self) -> str:
        """``"시도 시군구"``, or just the sido for a sido row."""
        return f"{self.sido} {self.sigungu}" if self.sigungu else self.sido


@cache
def _regions() -> tuple[Region, ...]:
    with REGIONS_CSV.open(encoding="utf-8", newline="") as f:
        return tuple(
            Region(code=r["code"], sido=r["sido"], sigungu=r["sigungu"] or None)
            for r in csv.DictReader(f)
        )


@cache
def _by_code() -> dict[str, Region]:
    return {r.code: r for r in _regions()}


def load_regions() -> list[Region]:
    return list(_regions())


def list_sido() -> list[Region]:
    return [r for r in _regions() if r.sigungu is None]


def list_sigungu(sido: str) -> list[Region]:
    return [r for r in _regions() if r.sigungu is not None and r.sido == sido]


def find(code: str) -> Region | None:
    return _by_code().get(code)


def search(q: str) -> list[Region]:
    """Regions whose sido or sigungu contains ``q`` (whitespace-insensitive), max 50."""
    needle = "".join(q.split())
    if not needle:
        return []
    found = [
        r
        for r in _regions()
        if needle in r.sido or (r.sigungu is not None and needle in "".join(r.sigungu.split()))
    ]
    return found[:SEARCH_LIMIT]
