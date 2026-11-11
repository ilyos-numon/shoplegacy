"""Card claim evidence: link card sentences to source answers and score support."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Literal

from handoff.contracts import containment
from handoff.llm_tasks.verify import SUPPORT_MIN
from pydantic import BaseModel

from shoplegacy.masking import MaskMap, mask
from shoplegacy.question_tree import AXIS_LABELS, Axis

if TYPE_CHECKING:
    from shoplegacy.interview import AnswerEntry
    from shoplegacy.providers import ShopLLM

# Split after a sentence end (. ! ? and the Korean endings 다. 요.) followed by whitespace.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
QUOTE_MAX_CHARS = 160


class Evidence(BaseModel):
    claim: str
    answer_id: int | None
    quote: str
    supported: bool
    # "reviewer": the sentence was not in the LLM draft (written by the reviewer).
    source: Literal["answer", "reviewer"] = "answer"


class Quality(BaseModel):
    supported_ratio: float
    missing_axes: list[Axis]
    gate_ok: bool
    reasons: list[str]
    # Shown but never blocking: reviewer_sentences:<n>, llm_fallback, llm_quota_exceeded,
    # owner_changes_requested.
    flags: list[str] = []


EvidenceBuilder = Callable[
    ["ShopLLM", dict[Axis, str], list["AnswerEntry"], MaskMap], dict[Axis, list[Evidence]]
]


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s.strip()]


def build_evidence(
    llm: ShopLLM,
    sections: dict[Axis, str],
    answers: list[AnswerEntry],
    mask_map: MaskMap,
    *,
    draft_sections: dict[Axis, str] | None = None,
) -> dict[Axis, list[Evidence]]:
    """Check each sentence against the answers. With ``draft_sections``, sentences that
    were not in the LLM draft are marked ``source="reviewer"`` and not checked."""
    if draft_sections is not None:
        drafted, written = split_reviewer_sentences(sections, draft_sections)
        built = build_evidence(llm, drafted, answers, mask_map)
        return merge_reviewer_sentences(built, sections, written)

    from shoplegacy.cards import CARD_AXES  # local: cards imports this module

    out: dict[Axis, list[Evidence]] = {}
    for axis in CARD_AXES:
        claims = split_sentences(sections.get(axis, ""))
        if not claims:
            out[axis] = []
            continue
        sources = _excerpts(axis, answers, mask_map)
        verdicts = llm.verify_claims(
            axis_question(axis), claims, [text for _, text in sources]
        )
        items: list[Evidence] = []
        for claim, verdict in zip(claims, verdicts, strict=True):
            cite = verdict.citation_index
            supported = verdict.supported
            cited = cite is not None and 1 <= cite <= len(sources)
            if not (supported and cited) and llm.last_provider() != "fallback":
                # The verifier answered but gave no usable citation: rescue sentences
                # that are nearly verbatim in one answer excerpt.
                cite = lexical_citation(claim, [text for _, text in sources])
                supported = cite is not None
            if supported and cite is not None and 1 <= cite <= len(sources):
                answer_id, quote = sources[cite - 1]
                items.append(Evidence(claim=claim, answer_id=answer_id, quote=quote, supported=True))
            else:
                items.append(Evidence(claim=claim, answer_id=None, quote="", supported=False))
        out[axis] = items
    return out


def lexical_citation(claim: str, excerpts: list[str]) -> int | None:
    """1-based number of the first excerpt with containment(claim, excerpt) >= SUPPORT_MIN."""
    for index, excerpt in enumerate(excerpts, start=1):
        if containment(claim, excerpt) >= SUPPORT_MIN:
            return index
    return None


def split_reviewer_sentences(
    sections: dict[Axis, str], draft_sections: dict[Axis, str]
) -> tuple[dict[Axis, str], dict[Axis, set[str]]]:
    """(sections keeping only drafted sentences, reviewer-written sentences per axis)."""
    drafted: dict[Axis, str] = {}
    written: dict[Axis, set[str]] = {}
    for axis, text in sections.items():
        known = set(split_sentences(draft_sections.get(axis, "")))
        sentences = split_sentences(text)
        drafted[axis] = " ".join(s for s in sentences if s in known)
        written[axis] = {s for s in sentences if s not in known}
    return drafted, written


def merge_reviewer_sentences(
    built: dict[Axis, list[Evidence]],
    sections: dict[Axis, str],
    written: dict[Axis, set[str]],
) -> dict[Axis, list[Evidence]]:
    """Put reviewer-written sentences back in their place among the checked evidence."""
    out: dict[Axis, list[Evidence]] = {axis: list(items) for axis, items in built.items()}
    for axis, text in sections.items():
        mine = written.get(axis, set())
        if not mine:
            continue
        checked = iter(built.get(axis, []))
        items: list[Evidence] = []
        for sentence in split_sentences(text):
            if sentence in mine:
                items.append(
                    Evidence(
                        claim=sentence, answer_id=None, quote="", supported=False, source="reviewer"
                    )
                )
            else:
                item = next(checked, None)
                if item is not None:
                    items.append(item)
        items.extend(checked)
        out[axis] = items
    return out


def reviewer_sentence_count(evidence: dict[Axis, list[Evidence]]) -> int:
    return sum(1 for items in evidence.values() for e in items if e.source == "reviewer")


def axis_question(axis: Axis) -> str:
    return f"가게의 {AXIS_LABELS[axis]}에 대해 사장님은 무엇을 말했나요?"


def _excerpts(
    axis: Axis, answers: list[AnswerEntry], mask_map: MaskMap
) -> list[tuple[int | None, str]]:
    """Masked answers on the axis, cut into chunks of at most QUOTE_MAX_CHARS."""
    out: list[tuple[int | None, str]] = []
    for entry in answers:
        if entry.axis != axis:
            continue
        text = mask(entry.answer, mask_map).text.strip()
        for start in range(0, len(text), QUOTE_MAX_CHARS):
            chunk = text[start : start + QUOTE_MAX_CHARS].strip()
            if chunk:
                out.append((entry.answer_id, chunk))
    return out


def supported_ratio(evidence: dict[Axis, list[Evidence]], axes: Iterable[Axis]) -> float:
    """Share of supported sentences. Reviewer-written sentences are left out; if only
    those remain the ratio is 1.0. No sentences at all is 0.0."""
    items = [e for axis in axes for e in evidence.get(axis, [])]
    if not items:
        return 0.0
    checked = [e for e in items if e.source != "reviewer"]
    if not checked:
        return 1.0
    return sum(1 for e in checked if e.supported) / len(checked)
