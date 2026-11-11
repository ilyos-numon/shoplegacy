"""E2E: queued transcription jobs through the API and the interview screen."""

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, expect

OpenSession = Callable[[str, dict[str, str]], str]
TEXT = "가게는 큰길 뒤쪽 골목에 있었어요."
DONE_TIMEOUT_S = 10


def _post(base: str, headers: dict[str, str], session_id: str, **file: Any) -> httpx.Response:
    return httpx.post(
        f"{base}/api/stt/jobs",
        files={
            "file": (
                file.get("name", "note.txt"),
                file.get("data", TEXT.encode()),
                file.get("type", "text/plain"),
            )
        },
        data={"session_id": session_id},
        timeout=10,
        headers=headers,
    )


def _wait_done(base: str, headers: dict[str, str], job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + DONE_TIMEOUT_S
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        res = httpx.get(f"{base}/api/stt/jobs/{job_id}", timeout=10, headers=headers)
        assert res.status_code == 200, res.text
        body = res.json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.3)
    return body


def test_text_job_is_queued_then_done_and_audio_is_removed(
    server_url: str, auth_headers: dict[str, str], open_session: OpenSession, tmp_path: Path
) -> None:
    session_id = open_session(server_url, auth_headers)
    res = _post(server_url, auth_headers, session_id)
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "queued"
    job_id = res.json()["job_id"]
    body = _wait_done(server_url, auth_headers, job_id)
    assert body["status"] == "done", body
    assert body["text"] == TEXT
    assert body["provider"] == "fake"
    stored = [p for p in (tmp_path / "audio").rglob("*") if p.is_file() and job_id in p.name]
    assert stored == []


def test_session_lists_its_jobs(
    server_url: str, auth_headers: dict[str, str], open_session: OpenSession
) -> None:
    session_id = open_session(server_url, auth_headers)
    job_id = _post(server_url, auth_headers, session_id).json()["job_id"]
    _wait_done(server_url, auth_headers, job_id)
    res = httpx.get(
        f"{server_url}/api/sessions/{session_id}/stt-jobs", timeout=10, headers=auth_headers
    )
    assert res.status_code == 200
    assert [j["job_id"] for j in res.json()] == [job_id]


def test_pdf_job_is_415(
    server_url: str, auth_headers: dict[str, str], open_session: OpenSession
) -> None:
    session_id = open_session(server_url, auth_headers)
    res = _post(
        server_url,
        auth_headers,
        session_id,
        name="doc.pdf",
        data=b"%PDF-1.4 fake",
        type="application/pdf",
    )
    assert res.status_code == 415


def test_other_org_session_is_404(
    server_url: str,
    auth_headers: dict[str, str],
    second_org: dict[str, str],
    open_session: OpenSession,
) -> None:
    other_session = open_session(server_url, second_org)
    res = _post(server_url, auth_headers, other_session)
    assert res.status_code == 404
    job_id = _post(server_url, second_org, other_session).json()["job_id"]
    seen = httpx.get(f"{server_url}/api/stt/jobs/{job_id}", timeout=10, headers=auth_headers)
    assert seen.status_code == 404


def test_job_stays_queued_when_worker_is_off(
    server_url_no_worker: str, auth_headers: dict[str, str], open_session: OpenSession
) -> None:
    session_id = open_session(server_url_no_worker, auth_headers)
    res = _post(server_url_no_worker, auth_headers, session_id)
    assert res.status_code == 202, res.text
    job_id = res.json()["job_id"]
    time.sleep(3)
    got = httpx.get(
        f"{server_url_no_worker}/api/stt/jobs/{job_id}", timeout=10, headers=auth_headers
    )
    assert got.json()["status"] == "queued"


def test_interview_screen_shows_job_result_and_passes_axe(
    server_url: str,
    auth_headers: dict[str, str],
    open_session: OpenSession,
    page: Page,
    tmp_path: Path,
    shot_dir: Path,
    axe_check: Callable[[Page], list[dict[str, Any]]],
) -> None:
    session_id = open_session(server_url, auth_headers)
    note = tmp_path / "answer.txt"
    note.write_text(TEXT, encoding="utf-8")
    page.goto(f"{server_url}/interview/{session_id}")
    expect(page.locator("#question-text")).not_to_have_text("질문을 불러오는 중입니다...")
    page.click("#alt-input-btn")
    page.set_input_files("#audio-file", str(note))
    page.click("#transcribe-btn")
    expect(page.locator("#stt-job-status")).to_have_text("완료", timeout=15000)
    expect(page.locator("#answer-text")).to_have_value(TEXT)
    page.screenshot(path=str(shot_dir / "stt-job.png"), full_page=True)
    assert os.path.getsize(shot_dir / "stt-job.png") > 0
    assert axe_check(page) == []
