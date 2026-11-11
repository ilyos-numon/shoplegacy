"""Answer masking: contacts via handoff.privacy, shop/owner/supplier names, addresses, then NER."""

import re

from handoff.privacy import find_pii, scrub_contacts
from pydantic import BaseModel, Field

from shoplegacy.ner import NameFinder, NameSpan, NoopNameFinder

ADDRESS_RE = r"[가-힣]{1,10}(동|로|길)\s?\d{1,4}(-\d{1,4})?"
LANDMARK_RE = r"[가-힣]{2,10}(초등학교|중학교|고등학교|아파트|역)"
SHOP_MASK = "우리 가게"
OWNER_MASK = "사장님"
ADDRESS_MASK = "[주소]"
NAME_MASK, ORG_MASK, LOCATION_MASK = "[이름]", "[상호]", "[지역]"

_ADDRESS = re.compile(ADDRESS_RE)
_LANDMARK = re.compile(LANDMARK_RE)
# Bracketed placeholders such as "[전화번호]" left by earlier masking steps.
_PLACEHOLDER = re.compile(r"\[[^\[\]\s]{1,10}\]")
_NER_MASKS = {"PERSON": NAME_MASK, "ORG": ORG_MASK, "LOCATION": LOCATION_MASK}

_default_finder: NameFinder = NoopNameFinder()


def configure(finder: NameFinder) -> None:
    """Set the finder used when mask() is called without one."""
    global _default_finder
    _default_finder = finder


class MaskMap(BaseModel):
    shop_name: str = ""
    owner_name: str = ""
    supplier_names: list[str] = Field(default_factory=list)


class MaskResult(BaseModel):
    text: str
    replaced: int
    warnings: list[str]
    ner_replaced: int = 0


def _dictionary(mask_map: MaskMap) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = [
        (mask_map.shop_name, SHOP_MASK),
        (mask_map.owner_name, OWNER_MASK),
    ]
    suppliers = [(name, "거래처 " + chr(65 + i)) for i, name in enumerate(mask_map.supplier_names)]
    pairs.extend(sorted(suppliers, key=lambda p: len(p[0]), reverse=True))
    return pairs


def _protected(text: str, labels: list[str]) -> list[tuple[int, int]]:
    ranges = [(m.start(), m.end()) for m in _PLACEHOLDER.finditer(text)]
    for label in labels:
        start = text.find(label)
        while start != -1:
            ranges.append((start, start + len(label)))
            start = text.find(label, start + 1)
    return ranges


def _apply_names(
    text: str, spans: list[NameSpan], protected: list[tuple[int, int]]
) -> tuple[str, int]:
    chosen: list[NameSpan] = []
    for span in sorted(spans, key=lambda s: (s.start, -(s.end - s.start))):
        if span.end <= span.start or span.start < 0 or span.end > len(text):
            continue
        if any(span.start < end and start < span.end for start, end in protected):
            continue
        if chosen and span.start < chosen[-1].end:
            continue
        chosen.append(span)
    for span in reversed(chosen):
        text = text[: span.start] + _NER_MASKS[span.label] + text[span.end :]
    return text, len(chosen)


def mask(text: str, mask_map: MaskMap, finder: NameFinder | None = None) -> MaskResult:
    out = scrub_contacts(text)
    replaced = len(find_pii(text)) - len(find_pii(out))
    labels = [ADDRESS_MASK, *_NER_MASKS.values()]
    for key, label in _dictionary(mask_map):
        labels.append(label)
        key = key.strip()
        if not key:
            continue
        count = out.count(key)
        if count:
            out = out.replace(key, label)
            replaced += count
    out, addresses = _ADDRESS.subn(ADDRESS_MASK, out)
    replaced += addresses
    spans = (finder or _default_finder).find(out)
    out, ner_replaced = _apply_names(out, spans, _protected(out, labels))
    replaced += ner_replaced
    warnings = find_pii(out) + [m.group(0) for m in _LANDMARK.finditer(out)]
    return MaskResult(text=out, replaced=replaced, warnings=warnings, ner_replaced=ner_replaced)
