"""Convert the national legal-dong code file into the bundled sido/sigungu CSV.

Usage: uv run python scripts/fetch_regions.py <source.csv> <out.csv>

The source is the "전국 법정동" CSV from 공공데이터포털 (see
src/shoplegacy/data/README.md) with the columns 법정동코드, 시도명, 시군구명,
읍면동명, 리명, 순번, 생성일자. It lists current codes only. This script does
not download anything; pass the path of a file you already have.

Kept rows: sido codes (``XX00000000``) and sigungu codes (``XXXXX00000`` whose
sigungu part is not ``000``). Output columns: ``code,sido,sigungu`` (sigungu is
empty for a sido row), sorted by code.
"""

import csv
import sys
from pathlib import Path

ENCODINGS = ("utf-8-sig", "cp949")
CODE_COL = "법정동코드"
SIDO_COL = "시도명"
SIGUNGU_COL = "시군구명"


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"{path}: not UTF-8 or CP949")


def convert(text: str) -> list[tuple[str, str, str]]:
    reader = csv.DictReader(text.splitlines())
    missing = {CODE_COL, SIDO_COL, SIGUNGU_COL} - set(reader.fieldnames or [])
    if missing:
        raise SystemExit(f"missing columns: {sorted(missing)}")
    rows: dict[str, tuple[str, str, str]] = {}
    for rec in reader:
        code = (rec[CODE_COL] or "").strip()
        if len(code) != 10 or not code.isdigit():
            continue
        sido = (rec[SIDO_COL] or "").strip()
        sigungu = (rec[SIGUNGU_COL] or "").strip()
        if code[2:] == "00000000":
            rows[code] = (code, sido, "")
        elif code[5:] == "00000" and sigungu:
            rows[code] = (code, sido, sigungu)
    return [rows[c] for c in sorted(rows)]


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    src, out = Path(argv[1]), Path(argv[2])
    rows = convert(_read_text(src))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["code", "sido", "sigungu"])
        writer.writerows(rows)
    sido = sum(1 for r in rows if not r[2])
    print(f"wrote {out}: {sido} sido, {len(rows) - sido} sigungu")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
