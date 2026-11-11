import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

import procrastinate
import procrastinate.tasks
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from procrastinate.testing import InMemoryConnector

from shoplegacy import app_state, autopay, jobs
from shoplegacy.audio_store import AudioStore, audio_key
from shoplegacy.main import create_app
from shoplegacy.payments import PaymentProvider
from shoplegacy.store import Store, SttJobRow
from shoplegacy.stt import STT, Transcript

OrgTokens = tuple[str, str, str, str]
ROW = SttJobRow(
    job_id="job-1",
    org_id="org-1",
    session_id="sess-1",
    created_by="user-1",
    status="queued",
    audio_key="org-1/job-1.txt",
    content_type="text/plain",
    bytes=5,
    text=None,
    duration_s=None,
    provider=None,
    error=None,
    created_at=datetime.now(UTC),
    started_at=None,
    finished_at=None,
)


class FakeStore:
    def __init__(self) -> None:
        self.rows = {ROW.job_id: ROW}
        self.claims: list[str] = []
        self.finished: list[tuple[str, str]] = []
        self.failed: list[tuple[str, str]] = []

    def stt_job_claim(self, job_id: str) -> SttJobRow | None:
        self.claims.append(job_id)
        row = self.rows.get(job_id)
        if row is None or row.status != "queued":
            return None
        self.rows[job_id] = replace(row, status="running")
        return self.rows[job_id]

    def stt_job_finish(
        self, job_id: str, text: str, duration_s: float | None, provider: str
    ) -> None:
        self.finished.append((job_id, text))

    def stt_job_fail(self, job_id: str, error: str) -> None:
        self.failed.append((job_id, error))


class FakeAudio:
    def __init__(self) -> None:
        self.blobs = {ROW.audio_key: b"hello"}
        self.deleted: list[str] = []

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.blobs[key] = data

    def get(self, key: str) -> bytes:
        return self.blobs[key]

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.blobs.pop(key, None)

    def exists(self, key: str) -> bool:
        return key in self.blobs


class FlakySTT:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def transcribe(self, audio: bytes, filename: str, content_type: str) -> Transcript:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("x" * 500)
        return Transcript(text=audio.decode(), duration_s=1.5, provider="flaky")


Fakes = tuple[FakeStore, FakeAudio]


def _configure(stt: FlakySTT) -> Fakes:
    store, audio = FakeStore(), FakeAudio()
    jobs.configure(
        store=cast(Store, store),
        stt=cast(STT, stt),
        audio_store=cast(AudioStore, audio),
        dsn="postgresql://unused@localhost/unused",
    )
    return store, audio


@pytest.fixture(autouse=True)
def _reset_jobs() -> Iterator[None]:
    yield
    jobs.shutdown()


def _run_in_memory(job_id: str) -> InMemoryConnector:
    connector = InMemoryConnector()
    worker_app = jobs._app_with(connector)
    with worker_app.open():
        procrastinate.tasks.configure_task(
            name=jobs.TASK_NAME, job_manager=worker_app.job_manager, queue=jobs.QUEUE
        ).defer(job_id=job_id)
    worker_app.run_worker(queues=[jobs.QUEUE], wait=False, install_signal_handlers=False)
    return connector


def test_task_is_registered_on_the_stt_queue_with_two_retries() -> None:
    task = jobs.app.tasks[jobs.TASK_NAME]
    assert task.queue == jobs.QUEUE == "stt"
    assert task.retry_strategy is not None
    assert getattr(task.retry_strategy, "max_attempts", None) == 2


def test_direct_call_transcribes_and_deletes_audio() -> None:
    store, audio = _configure(FlakySTT(failures=0))
    jobs.transcribe_job("job-1")
    assert store.finished == [("job-1", "hello")]
    assert store.failed == []
    assert audio.deleted == [ROW.audio_key]


def test_unclaimable_job_is_left_alone() -> None:
    store, audio = _configure(FlakySTT(failures=0))
    jobs.transcribe_job("missing")
    assert store.finished == store.failed == []
    assert audio.deleted == []


def test_direct_call_failure_marks_failed_without_raising() -> None:
    store, audio = _configure(FlakySTT(failures=1))
    jobs.transcribe_job("job-1")
    assert store.finished == []
    assert store.failed == [("job-1", "x" * 300)]
    assert audio.deleted == [ROW.audio_key]


def test_worker_retries_then_succeeds() -> None:
    stt = FlakySTT(failures=2)
    store, audio = _configure(stt)
    connector = _run_in_memory("job-1")
    assert stt.calls == 3
    assert store.claims == ["job-1"] * 3
    assert store.finished == [("job-1", "hello")]
    assert store.failed == []
    assert audio.deleted == [ROW.audio_key]
    (job,) = connector.jobs.values()
    assert job["status"] == "succeeded"
    assert job["attempts"] == 3


def test_worker_marks_failed_on_the_last_attempt_only() -> None:
    stt = FlakySTT(failures=10)
    store, audio = _configure(stt)
    connector = _run_in_memory("job-1")
    assert stt.calls == 3
    assert store.failed == [("job-1", "x" * 300)]
    assert store.finished == []
    assert audio.deleted == [ROW.audio_key]
    assert not audio.exists(ROW.audio_key)
    (job,) = connector.jobs.values()
    assert job["status"] == "succeeded"


def test_retry_after_a_lost_worker_fails_the_stuck_job() -> None:
    store, _ = _configure(FlakySTT(failures=0))
    store.rows["job-1"] = replace(ROW, status="running")
    token = jobs._attempt.set((1, False))
    try:
        jobs.transcribe_job("job-1")
    finally:
        jobs._attempt.reset(token)
    assert store.failed == [("job-1", jobs.INTERRUPTED)]
    assert store.finished == []


def test_worker_thread_stops_on_shutdown(app_dsn: str) -> None:
    _configure(FlakySTT(failures=0))
    jobs.configure(
        store=cast(Store, FakeStore()),
        stt=cast(STT, FlakySTT(0)),
        audio_store=cast(AudioStore, FakeAudio()),
        dsn=app_dsn,
    )
    thread = jobs.run_worker_thread()
    assert thread.daemon and thread.is_alive()
    jobs.shutdown()
    assert not thread.is_alive()


def _worker_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "shoplegacy-stt-worker" and t.is_alive()]


def _wait_for_no_worker_threads(seconds: float) -> list[threading.Thread]:
    deadline = time.monotonic() + seconds
    while _worker_threads() and time.monotonic() < deadline:
        time.sleep(0.05)
    return _worker_threads()


def test_worker_queues_include_billing() -> None:
    assert jobs.BILLING_QUEUE == "billing"
    assert jobs.CHARGE_CRON == "0 3 * * *"
    assert jobs.CHARGE_TASK_NAME == "shoplegacy.charge_due"


def test_charge_job_is_scheduled_daily_on_the_billing_queue() -> None:
    task = jobs.app.tasks[jobs.CHARGE_TASK_NAME]
    assert task is jobs.charge_due_job
    assert task.queue == jobs.BILLING_QUEUE == "billing"
    [periodic] = [
        p for p in jobs.app.periodic_registry.periodic_tasks.values() if p.task is task
    ]
    assert periodic.cron == jobs.CHARGE_CRON == "0 3 * * *"
    copy = jobs._app_with(InMemoryConnector())
    assert copy.periodic_registry is jobs.app.periodic_registry


def test_charge_job_runs_autopay_with_the_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = cast(Store, FakeStore())
    provider = cast(PaymentProvider, object())
    jobs.configure(
        store=store,
        stt=cast(STT, FlakySTT(0)),
        audio_store=cast(AudioStore, FakeAudio()),
        dsn="postgresql://unused@localhost/unused",
        payments=provider,
    )
    calls: list[tuple[object, object, date]] = []

    def charge_due(s: Store, p: PaymentProvider, today: date) -> list[autopay.ChargeOutcome]:
        calls.append((s, p, today))
        return [autopay.ChargeOutcome(org_id="org-1", invoice_id="inv-1", status="paid", code=None)]

    monkeypatch.setattr(autopay, "charge_due", charge_due)
    jobs.charge_due_job(timestamp=0)
    assert calls == [(store, provider, date.today())]


def test_lifespan_shutdown_stops_workers(app_dsn: str) -> None:
    jobs.configure(
        store=cast(Store, FakeStore()),
        stt=cast(STT, FlakySTT(0)),
        audio_store=cast(AudioStore, FakeAudio()),
        dsn=app_dsn,
    )
    thread = jobs.run_worker_thread()
    with TestClient(FastAPI(lifespan=app_state.lifespan)):
        assert thread.is_alive()
    assert jobs._workers == []
    thread.join(2)
    assert not thread.is_alive()


def test_app_lifespan_shutdown_stops_the_stt_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_dsn: str
) -> None:
    monkeypatch.setenv("SHOPLEGACY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", app_dsn)
    monkeypatch.setenv("SHOPLEGACY_JWT_SECRET", "test-secret")
    monkeypatch.setenv("SHOPLEGACY_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_WORKER", "thread")
    with TestClient(create_app()) as client:
        assert client.get("/health").status_code == 200
        assert jobs._workers
    assert jobs._workers == []
    assert _wait_for_no_worker_threads(2.0) == []


def _client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_dsn: str, headers: dict[str, str]
) -> TestClient:
    monkeypatch.setenv("SHOPLEGACY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHOPLEGACY_DATABASE_URL", app_dsn)
    monkeypatch.setenv("SHOPLEGACY_JWT_SECRET", "test-secret")
    monkeypatch.setenv("SHOPLEGACY_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_PROVIDER", "fake")
    monkeypatch.setenv("SHOPLEGACY_STT_WORKER", "off")
    monkeypatch.setenv("SHOPLEGACY_STT_MAX_BYTES", "1000")
    return TestClient(create_app(), headers=headers)


def _interviewer_headers(org: OrgTokens) -> dict[str, str]:
    return {"Authorization": f"Bearer {org[3]}", "X-Org-Id": org[0]}


def test_upload_queue_work_and_read_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    org_fixture: OrgTokens,
    stt_session: str,
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn, _interviewer_headers(org_fixture))
    response = client.post(
        "/api/stt/jobs",
        files={"file": ("a.txt", "장사 이야기".encode(), "text/plain; charset=utf-8")},
        data={"session_id": stt_session},
    )
    assert response.status_code == 202, response.text
    job: dict[str, Any] = response.json()
    assert job["status"] == "queued"
    assert job["session_id"] == stt_session
    key = audio_key(org_fixture[0], job["job_id"], "text/plain")
    state = cast(Any, client.app).state.shop
    assert state.audio_store.exists(key)

    worker_app = jobs._app_with(procrastinate.PsycopgConnector(conninfo=app_dsn))
    worker_app.run_worker(queues=[jobs.QUEUE], wait=False, install_signal_handlers=False)

    done = client.get(f"/api/stt/jobs/{job['job_id']}").json()
    assert done["status"] == "done"
    assert done["text"] == "장사 이야기"
    assert done["provider"] == "fake"
    assert done["finished_at"] is not None
    assert not state.audio_store.exists(key)
    listed = client.get(f"/api/sessions/{stt_session}/stt-jobs").json()
    assert [j["job_id"] for j in listed] == [job["job_id"]]


def test_upload_rejects_bad_type_and_unknown_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    app_dsn: str,
    org_fixture: OrgTokens,
    stt_session: str,
) -> None:
    client = _client(tmp_path, monkeypatch, app_dsn, _interviewer_headers(org_fixture))
    bad_type = client.post(
        "/api/stt/jobs",
        files={"file": ("a.pdf", b"%PDF", "application/pdf")},
        data={"session_id": stt_session},
    )
    assert bad_type.status_code == 415
    too_big = client.post(
        "/api/stt/jobs",
        files={"file": ("a.wav", b"0" * 1001, "audio/wav")},
        data={"session_id": stt_session},
    )
    assert too_big.status_code == 413
    unknown = client.post(
        "/api/stt/jobs",
        files={"file": ("a.txt", b"hi", "text/plain")},
        data={"session_id": "sess-nope"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "session not found"
    assert client.get("/api/stt/jobs/nope").status_code == 404
    assert client.get("/api/sessions/sess-nope/stt-jobs").status_code == 404
    assert not (tmp_path / "data" / "audio" / org_fixture[0]).exists() or not any(
        (tmp_path / "data" / "audio" / org_fixture[0]).iterdir()
    )
    assert client.get(f"/api/sessions/{stt_session}/stt-jobs").json() == []
