"""Logging, request ids, error handling, rate limit and security headers."""

import logging
import re
import secrets
import time
import uuid
from collections.abc import Callable
from typing import Any, TextIO, TypeVar

import secure
import sentry_sdk
import structlog
from fastapi import FastAPI, HTTPException
from limits import parse_many
from limits.storage import MemoryStorage
from limits.strategies import MovingWindowRateLimiter, RateLimiter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Span
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import HTTPConnection, Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from shoplegacy.config import Settings

REQUEST_ID_HEADER = "X-Request-Id"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
EXEMPT_PATHS = frozenset({"/health", "/readyz"})
EXEMPT_PREFIXES = ("/static/",)
CSP_NONCE_PLACEHOLDER = "__CSP_NONCE__"
CSP_HEADER = "Content-Security-Policy"
CSP_REPORT_ONLY_HEADER = "Content-Security-Policy-Report-Only"
OTEL_EXCLUDED_URLS = "health,readyz,static"
# The owner bearer token travels in the path (``/owner/{token}``); it must never
# reach logs or span attributes.
_OWNER_TOKEN_RE = re.compile(r"(/owner/)[^/?#]+")
REDACTED_TOKEN = "{token}"
ROUTE_LIMIT_ATTR = "_shoplegacy_route_limit"
# Toss Payments widget v2 sources, allowed only on the billing pages.
PAYMENT_CSP_SOURCES: dict[str, tuple[str, ...]] = {
    "script-src": ("https://js.tosspayments.com",),
    "frame-src": ("'self'", "https://js.tosspayments.com", "https://*.tosspayments.com"),
    "connect-src": ("https://*.tosspayments.com",),
    "img-src": ("https://static.tosspayments.com",),
}
PAYMENT_PAGE_PREFIX = "/billing"

# No Permissions-Policy: the interview page needs the microphone.
SECURE_HEADERS = secure.Secure(
    hsts=secure.StrictTransportSecurity().max_age(31536000).include_subdomains(),
    xcto=secure.XContentTypeOptions().nosniff(),
    xfo=secure.XFrameOptions().sameorigin(),
    referrer=secure.ReferrerPolicy().strict_origin_when_cross_origin(),
)

log = structlog.get_logger("shoplegacy")
F = TypeVar("F", bound=Callable[..., Any])


def configure_logging(log_format: str, stream: TextIO | None = None) -> None:
    """Configure structlog for ``console`` or ``json`` single-line output.

    Logs go to ``stream`` when given, else to stdout.
    """
    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    renderer: structlog.typing.Processor
    if log_format == "json":
        processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[*processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        cache_logger_on_first_use=False,
    )


def content_security_policy(nonce: str, supabase_url: str, *, payment: bool = False) -> str:
    """CSP allowing same-origin assets, nonce'd inline scripts and supabase-js from esm.sh.

    With ``payment`` the payment widget sources are added (``frame-src`` only then).
    """
    connect = " ".join(src for src in ("'self'", "https://esm.sh", supabase_url.rstrip("/")) if src)
    directives = [
        ("default-src", "'self'"),
        ("script-src", f"'self' 'nonce-{nonce}' https://esm.sh"),
        ("connect-src", connect),
        ("img-src", "'self' data:"),
        ("style-src", "'self' 'unsafe-inline'"),
        ("media-src", "'self' blob:"),
        ("font-src", "'self'"),
        ("frame-ancestors", "'self'"),
        ("base-uri", "'self'"),
        ("form-action", "'self'"),
        ("object-src", "'none'"),
    ]
    if payment:
        known = {name for name, _ in directives}
        directives = [
            (name, " ".join((value, *PAYMENT_CSP_SOURCES.get(name, ()))))
            for name, value in directives
        ]
        directives += [
            (name, " ".join(sources))
            for name, sources in PAYMENT_CSP_SOURCES.items()
            if name not in known
        ]
    return "; ".join(f"{name} {value}" for name, value in directives)


def redact_path(value: str) -> str:
    """``value`` with every path segment after ``/owner/`` replaced by ``{token}``."""
    return _OWNER_TOKEN_RE.sub(rf"\g<1>{REDACTED_TOKEN}", value)


def route_limit(limit_value: str) -> Callable[[F], F]:
    """Mark an endpoint with a stricter per-route limit, enforced by the app's limiter."""

    def mark(func: F) -> F:
        setattr(func, ROUTE_LIMIT_ATTR, limit_value)
        return func

    return mark


def _request_id(request: Request) -> str:
    incoming = request.headers.get(REQUEST_ID_HEADER, "")
    if _REQUEST_ID_RE.match(incoming):
        return incoming
    return uuid.uuid4().hex


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Request id, CSP nonce, access log, 500 fallback and security headers."""

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        super().__init__(app)
        self.supabase_url = settings.supabase_url
        self.csp_header = CSP_REPORT_ONLY_HEADER if settings.csp_report_only else CSP_HEADER

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = _request_id(request)
        request.state.request_id = request_id
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            sentry_sdk.capture_exception(exc)
            log.exception(
                "unhandled_error",
                method=request.method,
                path=redact_path(request.url.path),
                error=type(exc).__name__,
            )
            response = JSONResponse(
                status_code=500,
                content={"detail": "internal error", "request_id": request_id},
            )
        response.headers[REQUEST_ID_HEADER] = request_id
        for name, value in SECURE_HEADERS.headers.items():
            response.headers[name] = value
        if not request.url.path.startswith(EXEMPT_PREFIXES):
            payment = request.url.path.startswith(PAYMENT_PAGE_PREFIX)
            response.headers[self.csp_header] = content_security_policy(
                nonce, self.supabase_url, payment=payment
            )
        log.info(
            "request",
            method=request.method,
            path=redact_path(request.url.path),
            status=response.status_code,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        structlog.contextvars.clear_contextvars()
        return response


def enforce_rate_limit(request: HTTPConnection) -> None:
    """App-wide dependency: 429 once the client exceeds the route's limit.

    Runs after routing, so ``scope["endpoint"]`` is the endpoint serving the
    request, also for routes inside included routers. A ``route_limit`` mark
    replaces the default limit for that endpoint. WebSocket connections are not
    limited.
    """
    if request.scope["type"] != "http":
        return
    path = request.url.path
    if path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES):
        return
    limiter: RateLimiter | None = getattr(request.app.state, "limiter", None)
    if limiter is None:
        return
    key = request.client.host if request.client is not None else "unknown"
    handler = request.scope.get("endpoint")
    route_value = getattr(handler, ROUTE_LIMIT_ATTR, None)
    limit_value = (
        route_value if isinstance(route_value, str) else request.app.state.rate_limit_default
    )
    # The default limit counts per request path (e.g. each session's answer URL);
    # a route limit counts across every path of its endpoint.
    if isinstance(route_value, str) and handler is not None:
        name = f"{handler.__module__}.{handler.__qualname__}"
    else:
        name = path
    for item in parse_many(limit_value):
        if not limiter.hit(item, key, name):
            raise HTTPException(status_code=429, detail=f"rate limit exceeded: {item}")


def _trusted_proxies(value: str) -> list[str]:
    return [host.strip() for host in value.split(",") if host.strip()]


def _redact_span(span: Span, scope: dict[str, Any]) -> None:
    """Server request hook: rewrite span attributes that carry the raw request path."""
    attributes = getattr(span, "attributes", None) or {}
    for key, value in list(attributes.items()):
        if isinstance(value, str):
            redacted = redact_path(value)
            if redacted != value:
                span.set_attribute(key, redacted)


def _install_tracing(app: FastAPI, settings: Settings, span_exporter: SpanExporter | None) -> None:
    processor: SpanProcessor
    if span_exporter is not None:
        processor = SimpleSpanProcessor(span_exporter)
    elif settings.otel_endpoint:
        processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint))
    else:
        return
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: settings.otel_service_name}))
    provider.add_span_processor(processor)
    app.state.tracer_provider = provider
    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        excluded_urls=OTEL_EXCLUDED_URLS,
        server_request_hook=_redact_span,
        exclude_spans=["receive", "send"],
    )


def install(app: FastAPI, settings: Settings, *, span_exporter: SpanExporter | None = None) -> None:
    """Install observability middleware on ``app`` (safe to call twice)."""
    if getattr(app.state, "observability_installed", False):
        return
    app.state.observability_installed = True
    configure_logging(settings.log_format)
    if settings.sentry_dsn:
        sentry_sdk.init(dsn=settings.sentry_dsn, traces_sample_rate=0.0, send_default_pii=False)
    app.state.limiter = MovingWindowRateLimiter(MemoryStorage())
    app.state.rate_limit_default = settings.rate_limit
    app.add_middleware(RequestContextMiddleware, settings=settings)
    # Added last so it wraps everything: the rate limiter keys on the real client.
    trusted = _trusted_proxies(settings.trusted_proxies)
    if trusted:
        app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=trusted)
    _install_tracing(app, settings, span_exporter)
