# mypy: disable-error-code="import-untyped"
"""Dump what real Qwen does on each golden case (draft, claims, excerpts, verify replies).

No database: YAML -> mask -> QwenShopLLM.draft_sections -> mask sections -> build_evidence.
Raw responses are captured by a recording transport. The API key is read from the
SHOPLEGACY_LLM_API_KEY environment variable and never written anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import yaml
from handoff.contracts import ClaimVerdict, containment
from handoff.llm_tasks.verify import _parse_verdicts

from shoplegacy.cards import CARD_AXES, CardTags
from shoplegacy.config import DEFAULT_LLM_BASE_URL
from shoplegacy.evidence import build_evidence, split_sentences
from shoplegacy.interview import AnswerEntry
from shoplegacy.masking import MaskMap, mask
from shoplegacy.providers import DRAFT_SYSTEM, LLMUsage, QwenShopLLM, _citations
from shoplegacy.question_tree import Axis, FollowUp

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "tests" / "golden" / "cases"
DEFAULT_OUT = ROOT / "eval-diagnose"
START = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


class RecordingTransport(httpx.BaseTransport):
    """Passes requests through and keeps (kind, status, content, finish_reason) per call."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner
        self.records: list[dict[str, Any]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content or b"{}")
            system = body["messages"][0]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            system = ""
        kind = "draft" if system == DRAFT_SYSTEM else "verify"
        response = self._inner.handle_request(request)
        raw = response.read()
        content: Any = None
        finish: Any = None
        try:
            data = json.loads(raw)
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish = choice.get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError):
            pass
        self.records.append(
            {"kind": kind, "status": response.status_code, "content": content, "finish": finish}
        )
        return httpx.Response(
            response.status_code,
            # raw is already decoded: drop encoding/length so httpx does not decode it again
            headers=[
                (k, v)
                for k, v in response.headers.items()
                if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")
            ],
            content=raw,
            request=request,
        )


class Probe:
    """ShopLLM wrapper that remembers each verify call's inputs and raw reply."""

    def __init__(self, llm: QwenShopLLM, transport: RecordingTransport) -> None:
        self._llm = llm
        self._transport = transport
        self.calls: list[dict[str, Any]] = []

    def follow_up(
        self, question: str, answer: str, candidates: list[FollowUp]
    ) -> FollowUp | None:
        return self._llm.follow_up(question, answer, candidates)

    def draft_sections(
        self, answers_by_axis: dict[Axis, list[str]], tags: CardTags
    ) -> dict[Axis, str]:
        return self._llm.draft_sections(answers_by_axis, tags)

    def verify_claims(
        self, question: str, claims: list[str], excerpts: list[str]
    ) -> list[ClaimVerdict]:
        mark = len(self._transport.records)
        verdicts = self._llm.verify_claims(question, claims, excerpts)
        new = [r for r in self._transport.records[mark:] if r["status"] < 400]
        last = new[-1] if new else {"content": None, "finish": None}
        self.calls.append(
            {
                "claims": claims,
                "excerpts": excerpts,
                "content": last["content"],
                "finish": last["finish"],
                "verdicts": verdicts,
                "provider": self._llm.last_provider(),
            }
        )
        return verdicts

    def last_provider(self) -> str:
        return self._llm.last_provider()

    def last_usage(self) -> LLMUsage:
        return self._llm.last_usage()


def _answers(case: dict[str, Any]) -> list[AnswerEntry]:
    out: list[AnswerEntry] = []
    n = 0
    session_id = uuid.uuid4().hex
    for axis, texts in case["answers"].items():
        for text in texts:
            n += 1
            out.append(
                AnswerEntry(
                    session_id=session_id,
                    question_id=f"{axis}-{n}",
                    axis=axis,
                    question=f"{axis} 질문 {n}",
                    answer=text,
                    created_at=START + timedelta(seconds=n),
                    session_number=1,
                    answer_id=n,
                )
            )
    return out


def _best(claim: str, excerpts: list[str]) -> tuple[float, int | None]:
    best, index = 0.0, None
    for i, excerpt in enumerate(excerpts, start=1):
        score = containment(claim, excerpt)
        if score > best:
            best, index = score, i
    return round(best, 3), index


def diagnose(path: Path, base_url: str, model: str, api_key: str) -> dict[str, Any]:
    case = yaml.safe_load(path.read_text(encoding="utf-8"))
    shop = case["shop"]
    mask_map = MaskMap(
        shop_name=shop["name"],
        owner_name=shop["owner_name"],
        supplier_names=list(shop["supplier_names"]),
    )
    tags = CardTags(
        trade=shop["trade"],
        area_type=shop["area_type"],
        years=shop["years"],
        closed_year=shop["closed_year"],
    )
    answers = _answers(case)
    answers_by_axis: dict[Axis, list[str]] = {axis: [] for axis in CARD_AXES}
    for entry in answers:
        answers_by_axis[entry.axis].append(mask(entry.answer, mask_map).text)

    transport = RecordingTransport(httpx.HTTPTransport())
    llm = QwenShopLLM(base_url, model, api_key, 60.0, transport=transport)
    probe = Probe(llm, transport)
    drafted = llm.draft_sections(answers_by_axis, tags)
    draft_provider = llm.last_provider()
    draft_raw: dict[str, Any] = {}
    draft_records = [r for r in transport.records if r["kind"] == "draft" and r["status"] < 400]
    if draft_records and isinstance(draft_records[-1]["content"], str):
        try:
            loaded = json.loads(draft_records[-1]["content"])
            if isinstance(loaded, dict):
                draft_raw = loaded
        except ValueError:
            pass
    sections = {axis: mask(drafted.get(axis, ""), mask_map).text for axis in CARD_AXES}
    evidence = build_evidence(probe, sections, answers, mask_map)

    calls = iter(probe.calls)
    axes_out: dict[str, Any] = {}
    total = supported = 0
    for axis in CARD_AXES:
        claims = split_sentences(sections.get(axis, ""))
        call = next(calls) if claims else None
        excerpts = call["excerpts"] if call else []
        content = call["content"] if call else None
        verdicts_out = []
        parsed = False
        if call:
            text = content if isinstance(content, str) else ""
            parsed = _parse_verdicts(text, claims, _citations(excerpts)) is not None
            for i, (claim, verdict) in enumerate(zip(claims, call["verdicts"]), start=1):
                score, best = _best(claim, excerpts)
                verdicts_out.append(
                    {
                        "index": i,
                        "supported": verdict.supported,
                        "citation": verdict.citation_index,
                        "max_containment": score,
                        "best_excerpt": best,
                    }
                )
        items = evidence.get(axis, [])
        total += len(items)
        supported += sum(1 for e in items if e.supported)
        axes_out[axis] = {
            "answers_masked": answers_by_axis[axis],
            "draft_raw": draft_raw.get(axis),
            "section": sections.get(axis, ""),
            "claims": claims,
            "excerpts": excerpts,
            "verify_raw": content if isinstance(content, str) else "",
            "finish_reason": call["finish"] if call else None,
            "parsed": parsed,
            "verdicts": verdicts_out,
        }
    return {
        "case": path.stem,
        "model": model,
        "draft_provider": draft_provider,
        "tags": f"업종: {tags.trade}, 상권: {tags.area_type}, 운영 {tags.years}년, "
        f"폐업 {tags.closed_year}년",
        "axes": axes_out,
        "supported_ratio": round(supported / total, 3) if total else 0.0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("cases", nargs="*", help="case ids (default: all)")
    args = parser.parse_args()
    api_key = os.environ.get("SHOPLEGACY_LLM_API_KEY", "")
    if not api_key:
        print("SHOPLEGACY_LLM_API_KEY is empty", file=sys.stderr)
        return 2
    base_url = os.environ.get("SHOPLEGACY_LLM_BASE_URL") or DEFAULT_LLM_BASE_URL
    model = os.environ.get("SHOPLEGACY_LLM_MODEL") or "qwen3.7-flash"
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = sorted(CASES_DIR.glob("g*.yaml"))
    if args.cases:
        paths = [p for p in paths if p.stem in set(args.cases)]
    for path in paths:
        result = diagnose(path, base_url, model, api_key)
        (out_dir / f"{path.stem}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print(f"DIAGNOSE {path.stem} ratio={result['supported_ratio']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
