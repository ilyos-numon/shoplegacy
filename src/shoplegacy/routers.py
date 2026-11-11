"""All API/page routers with their mount prefixes; ``main.py`` iterates this list."""

from fastapi import APIRouter

from shoplegacy import (
    pages,
    routes_billing,
    routes_cards,
    routes_consent,
    routes_dashboard,
    routes_data,
    routes_interview,
    routes_ops,
    routes_orgs,
    routes_owner,
    routes_payments,
    routes_regions,
    routes_stt,
    routes_stt_jobs,
    routes_stt_stream,
)

ALL_ROUTERS: list[tuple[APIRouter, str]] = [
    (routes_interview.router, "/api"),
    (routes_stt.router, "/api"),
    (routes_cards.router, "/api"),
    (routes_orgs.router, "/api"),
    (routes_consent.router, "/api"),
    (routes_data.router, "/api"),
    (routes_regions.router, "/api"),
    (routes_owner.router, "/api"),
    (routes_dashboard.router, "/api"),
    (routes_billing.router, "/api"),
    (routes_stt_jobs.router, "/api"),
    (routes_stt_stream.router, "/api"),
    (routes_payments.router, "/api"),
    (routes_ops.router, ""),
    (pages.router, ""),
]
