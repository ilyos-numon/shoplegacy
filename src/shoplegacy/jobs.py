"""Queued speech-to-text transcription jobs."""

import asyncio
import atexit
import contextvars
import functools
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import procrastinate
import procrastinate.tasks
import structlog
from procrastinate.job_context import JobContext

from shoplegacy import autopay
from shoplegacy.audio_store import AudioStore, build_audio_store
from shoplegacy.config import load_settings
from shoplegacy.crypto import build_cipher
from shoplegacy.payments import PaymentProvider, build_payment_provider
from shoplegacy.store import Store, SttJobRow
from shoplegacy.stt import STT, build_stt

QUEUE = "stt"
TASK_NAME = "shoplegacy.transcribe"
BILLING_QUEUE = "billing"
CHARGE_CRON = "0 3 * * *"
CHARGE_TASK_NAME = "shoplegacy.charge_due"
RETRIES = 2
INTERRUPTED = "transcription interrupted"

log = structlog.get_logger("shoplegacy")

# The external worker (``procrastinate --app shoplegacy.jobs.app worker``) runs this
# app; the web process defers and runs its worker thread through copies of it that
# share the task registry but have their own connectors.
app = procrastinate.App(
    connector=procrastinate.PsycopgConnector(conninfo=os.environ.get("SHOPLEGACY_DATABASE_URL", ""))
)


@dataclass(frozen=True)
class _Deps:
    store: Store
    stt: STT
    audio_store: AudioStore
    dsn: str
    payments: PaymentProvider | None = None


@dataclass
class _Worker:
    thread: threading.Thread
    started: threading.Event
    app: procrastinate.App
    loop: asyncio.AbstractEventLoop | None = None
    task: "asyncio.Task[None] | None" = None
    stopping: bool = False


_lock = threading.Lock()
_deps: _Deps | None = None
_defer_app: procrastinate.App | None = None
_workers: list[_Worker] = []
# Rows claimed by this process, kept so a retry can find the audio again: the
# claim only moves queued -> running, so a retry cannot claim a second time.
_claimed: dict[str, SttJobRow] = {}
# (earlier attempts, is this the last one) of the attempt running in this context.
# Called outside a worker it is a single, last attempt.
_attempt: contextvars.ContextVar[tuple[int, bool]] = contextvars.ContextVar(
    "shoplegacy_stt_attempt", default=(0, True)
)


def _app_with(connector: procrastinate.BaseConnector) -> procrastinate.App:
    copy = procrastinate.App(
        connector=connector, import_paths=app.import_paths, worker_defaults=app.worker_defaults
    )
    copy.tasks = app.tasks
    copy.periodic_registry = app.periodic_registry
    return copy


def configure(
    *,
    store: Store,
    stt: STT,
    audio_store: AudioStore,
    dsn: str,
    payments: PaymentProvider | None = None,
) -> None:
    """Sets what jobs run against. Replaces (and stops) an earlier configuration.

    Without ``payments`` the charge job builds the provider from the settings.
    """
    global _deps
    shutdown()
    with _lock:
        _deps = _Deps(store=store, stt=stt, audio_store=audio_store, dsn=dsn, payments=payments)
        _claimed.clear()


def _dependencies() -> _Deps:
    global _deps
    with _lock:
        if _deps is None:
            settings = load_settings()
            cipher = build_cipher(settings)
            _deps = _Deps(
                store=Store(settings.database_url, cipher=cipher),
                stt=build_stt(settings),
                audio_store=build_audio_store(settings, cipher),
                dsn=settings.database_url,
            )
        return _deps


def _payments() -> tuple[Store, PaymentProvider]:
    global _deps
    deps = _dependencies()
    if deps.payments is not None:
        return deps.store, deps.payments
    provider = build_payment_provider(load_settings())
    with _lock:
        if _deps is deps:
            _deps = replace(deps, payments=provider)
    return deps.store, provider


def _dsn() -> str:
    with _lock:
        if _deps is not None:
            return _deps.dsn
    return os.environ.get("SHOPLEGACY_DATABASE_URL", "")


def defer_transcribe(job_id: str) -> None:
    global _defer_app
    dsn = _dsn()
    with _lock:
        if _defer_app is None:
            _defer_app = _app_with(procrastinate.SyncPsycopgConnector(conninfo=dsn))
            _defer_app.open()
        deferrer = _defer_app
    # By name through this app's job manager: the registered task object would
    # defer through the module-level app, which the web process never opens.
    procrastinate.tasks.configure_task(
        name=TASK_NAME, job_manager=deferrer.job_manager, queue=QUEUE
    ).defer(job_id=job_id)


async def _serve(worker_app: procrastinate.App, handle: _Worker) -> None:
    handle.loop = asyncio.get_running_loop()
    handle.task = asyncio.current_task()  # type: ignore[assignment]
    handle.started.set()
    if handle.stopping:
        return
    async with worker_app.open_async():
        await worker_app.run_worker_async(
            queues=[QUEUE, BILLING_QUEUE], wait=True, install_signal_handlers=False
        )


def _run_worker(worker_app: procrastinate.App, handle: _Worker) -> None:
    try:
        asyncio.run(_serve(worker_app, handle))
    except asyncio.CancelledError:
        pass
    except Exception:
        if not handle.stopping:
            log.exception("stt_worker_stopped")
    finally:
        handle.started.set()


def run_worker_thread() -> threading.Thread:
    """Runs the queue worker in a daemon thread of this process."""
    # Connections are opened on demand: a pool that refills itself right after the
    # server drops its connections would hold the database open (and block a
    # DROP DATABASE) while the worker is idle.
    worker_app = _app_with(procrastinate.PsycopgConnector(conninfo=_dsn(), min_size=0, max_size=4))
    handle = _Worker(thread=threading.Thread(), started=threading.Event(), app=worker_app)
    thread = threading.Thread(
        target=_run_worker, args=(worker_app, handle), name="shoplegacy-stt-worker", daemon=True
    )
    handle.thread = thread
    with _lock:
        _workers.append(handle)
    thread.start()
    return thread


def _call_in(handle: _Worker, callback: Callable[[], object]) -> None:
    loop = handle.loop
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(callback)
    except RuntimeError:
        pass


def _close_pool(worker_app: procrastinate.App) -> None:
    asyncio.ensure_future(worker_app.connector.close_async())


def stop_workers(grace: float = 2.0, timeout: float = 10.0) -> None:
    """Stops worker threads started by ``run_worker_thread``.

    The running job gets ``grace`` seconds to finish; after that the worker's pool
    is closed so a worker stuck on an unreachable database stops too.
    """
    with _lock:
        handles = list(_workers)
        _workers.clear()
    for handle in handles:
        handle.stopping = True
        handle.started.wait(timeout)
        task = handle.task
        if task is not None:
            _call_in(handle, task.cancel)
        handle.thread.join(grace)
        if handle.thread.is_alive():
            _call_in(handle, functools.partial(_close_pool, handle.app))
            handle.thread.join(timeout)


def shutdown() -> None:
    """Stops worker threads and closes the deferring connection pool."""
    global _defer_app
    stop_workers()
    with _lock:
        old = _defer_app
        _defer_app = None
    if old is not None:
        old.close()


# Fallback for use outside a server (tests, CLI); the app lifespan stops workers first.
atexit.register(shutdown)


def _track_attempt(call_next: Callable[[], Any], context: JobContext, worker: Any) -> Any:
    strategy = context.task.retry_strategy if context.task is not None else None
    last = (
        strategy is None
        or strategy.get_retry_decision(exception=RuntimeError(), job=context.job) is None
    )
    token = _attempt.set((context.job.attempts, last))
    try:
        return call_next()
    finally:
        _attempt.reset(token)


def _claim(deps: _Deps, job_id: str) -> SttJobRow | None:
    job = deps.store.stt_job_claim(job_id)
    with _lock:
        if job is not None:
            _claimed[job_id] = job
            return job
        return _claimed.get(job_id)


def _forget(job_id: str) -> None:
    with _lock:
        _claimed.pop(job_id, None)


@app.task(name=TASK_NAME, queue=QUEUE, retry=RETRIES, task_middleware=[_track_attempt])
def transcribe_job(job_id: str) -> None:
    """Transcribes one queued job; the audio is deleted once the job is settled.

    A failing attempt is retried; the last one marks the job failed instead of
    raising.
    """
    deps = _dependencies()
    attempts, last = _attempt.get()
    job = _claim(deps, job_id)
    if job is None:
        if attempts > 0:
            # A retry whose earlier attempt ran in a worker process that is gone:
            # the row is stuck in "running" and its audio key is unknown here.
            deps.store.stt_job_fail(job_id, INTERRUPTED)
        return
    try:
        audio = deps.audio_store.get(job.audio_key)
        result = deps.stt.transcribe(audio, filename=job.audio_key, content_type=job.content_type)
        deps.store.stt_job_finish(job_id, result.text, result.duration_s, result.provider)
    except Exception as exc:
        if not last:
            log.warning("stt_job_retry", job_id=job_id, error=type(exc).__name__)
            raise
        log.exception("stt_job_failed", job_id=job_id)
        deps.store.stt_job_fail(job_id, str(exc)[:300])
    _forget(job_id)
    deps.audio_store.delete(job.audio_key)


@app.periodic(cron=CHARGE_CRON)
@app.task(name=CHARGE_TASK_NAME, queue=BILLING_QUEUE)
def charge_due_job(timestamp: int) -> None:
    """Charges the billing keys due today (run daily by the worker's scheduler)."""
    store, provider = _payments()
    outcomes = autopay.charge_due(store, provider, date.today())
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    log.info("autopay_run", scheduled_at=timestamp, total=len(outcomes), **counts)
