"""UI E2E: register, interview with a text 'recording', review, publish, public card."""

import os
import re
from pathlib import Path

from playwright.sync_api import Page, expect

SHOP = "가나다식당"
OWNER = "홍길동"
AXES = ("location", "suppliers", "menu", "pricing", "customers", "failures")
MAX_ANSWERS = 40
END_NOTICE = "모두 마쳤습니다"
ANSWER = "손님들이 편하게 드시도록 늘 같은 방식으로 준비했어요."
TRANSCRIPT = "가게는 큰길에서 한 블록 들어간 골목 안쪽에 있었어요."


def _save(page: Page, name: str) -> None:
    out = Path(os.environ.get("ILLY_SHOT_DIR", "tests/e2e/screenshots"))
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    page.screenshot(path=str(path), full_page=True)
    assert path.stat().st_size > 0


def test_register_interview_review_publish(server_url: str, page: Page, tmp_path: Path) -> None:
    page.goto(f"{server_url}/")
    page.click("#new-shop-btn")
    page.fill("#shop-name", SHOP)
    page.fill("#owner-name", OWNER)
    page.fill("#years", "18")
    page.fill("#closed-year", "2025")
    page.fill("#supplier-names", "라마바정육")
    page.click("#start-btn")
    page.wait_for_url(re.compile(r"/shops/[^/]+/consent$"))
    page.click("#consent-save")

    page.wait_for_url(re.compile(r"/interview/[^/]+$"))
    question = page.locator("#question-text")
    expect(question).not_to_have_text(re.compile(r"불러오는 중"))
    expect(question).not_to_be_empty()
    first_question = question.inner_text()

    note = tmp_path / "answer.txt"
    note.write_text(TRANSCRIPT, encoding="utf-8")
    page.set_input_files("#audio-file", str(note))
    page.click("#transcribe-btn")
    expect(page.locator("#answer-text")).to_have_value(TRANSCRIPT)
    _save(page, "interview.png")

    page.click("#submit-btn")
    expect(page.locator("#answer-text")).to_have_value("")
    expect(question).not_to_have_text(first_question)

    for _ in range(MAX_ANSWERS):
        if END_NOTICE in question.inner_text():
            break
        current = question.inner_text()
        page.fill("#answer-text", ANSWER)
        page.click("#submit-btn")
        expect(page.locator("#answer-text")).to_have_value("")
        expect(question).not_to_have_text(current)
    expect(question).to_contain_text(END_NOTICE)

    page.click("#end-btn")
    page.wait_for_url(re.compile(r"/review/[^/]+$"))
    card_id = page.url.rsplit("/", 1)[1]
    areas = page.locator("textarea[data-axis]")
    expect(areas).to_have_count(6)
    for axis in AXES:
        expect(page.locator(f'textarea[data-axis="{axis}"]')).not_to_have_value("")
    expect(page.locator("#warnings")).to_be_attached()
    expect(page.locator("#publish-btn")).to_be_visible()
    _save(page, "review.png")

    page.click("#publish-btn")
    page.wait_for_url(f"{server_url}/cards/{card_id}")
    expect(page.locator("#tags")).to_contain_text("년 운영")
    expect(page.locator("section[data-axis] h2")).to_have_count(6)
    body = page.locator("body")
    expect(body).not_to_contain_text(SHOP)
    expect(body).not_to_contain_text(OWNER)
    _save(page, "card.png")
