"""Application state shared by routers (``app.state.shop``)."""

import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request

from shoplegacy import jobs, masking
from shoplegacy.audio_store import AudioStore, build_audio_store
from shoplegacy.billing import metered_llm
from shoplegacy.cards import CardService
from shoplegacy.config import Settings
from shoplegacy.crypto import build_cipher
from shoplegacy.interview import InterviewService
from shoplegacy.ner import build_name_finder
from shoplegacy.payments import PaymentProvider, build_payment_provider
from shoplegacy.providers import ShopLLM, build_llm
from shoplegacy.sms import SmsSender, build_sms_sender
from shoplegacy.store import Store
from shoplegacy.stt import STT, build_stt
from shoplegacy.stt_stream import StreamSTT, build_stream_factory


@dataclass
class State:
    settings: Settings
    store: Store
    llm: ShopLLM
    interview: InterviewService
    stt: STT
    cards: CardService
    sms: SmsSender
    audio_store: AudioStore
    stream_stt_factory: Callable[[], StreamSTT]
    payments: PaymentProvider
    jobs_worker: threading.Thread | None


def build_state(settings: Settings) -> State:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    cipher = build_cipher(settings)
    store = Store(settings.database_url, cipher=cipher)
    stt = build_stt(settings)
    audio_store = build_audio_store(settings, cipher)
    jobs.configure(store=store, stt=stt, audio_store=audio_store, dsn=settings.database_url)
    # Without a database there is no queue to work on.
    worker = (
        jobs.run_worker_thread()
        if settings.stt_worker == "thread" and settings.database_url
        else None
    )
    payments = build_payment_provider(settings)
    stream_stt_factory = build_stream_factory(settings)
    finder = build_name_finder(settings)
    # masking.configure arrives with NER masking; until then the finder is only
    # built so a bad SHOPLEGACY_NER still fails at startup.
    configure = getattr(masking, "configure", None)
    if configure is not None:
        configure(finder)
    llm = metered_llm(build_llm(settings), store)
    interview = InterviewService(
        store,
        llm,
        session_seconds=settings.session_seconds,
        planned=settings.sessions_planned,
    )
    return State(
        settings=settings,
        store=store,
        llm=llm,
        interview=interview,
        stt=stt,
        cards=CardService(store, llm, settings=settings),
        sms=build_sms_sender(settings),
        audio_store=audio_store,
        stream_stt_factory=stream_stt_factory,
        payments=payments,
        jobs_worker=worker,
    )


def get_state(request: Request) -> State:
    state: State = request.app.state.shop
    return state


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """App lifespan: on shutdown stop the job workers and the deferring pool."""
    yield
    jobs.shutdown()
