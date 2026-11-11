import json
import re

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from shoplegacy import observability, pages
from shoplegacy.config import load_settings

RATE_LIMITED = [Depends(observability.enforce_rate_limit)]
DB_ENV = {
    "SHOPLEGACY_DATABASE_URL": "postgresql://unused@localhost/unused",
    "SHOPLEGACY_JWT_SECRET": "test-secret",
}


def _client(span_exporter: SpanExporter | None = None, **env: str) -> TestClient:
    settings = load_settings({**DB_ENV, **env})
    app = FastAPI(dependencies=RATE_LIMITED)

    @app.get("/ok")
    def ok() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/boom")
    def boom() -> dict[str, str]:
        raise RuntimeError("kaboom")

    @app.get("/nonce")
    def nonce(request: Request) -> PlainTextResponse:
        return PlainTextResponse(request.state.csp_nonce)

    @app.get("/static/x.css")
    def static_file() -> PlainTextResponse:
        return PlainTextResponse("body{}", media_type="text/css")

    @app.get("/api/owner/{token}/card")
    def owner_card(token: str) -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/owner/{token}")
    def owner_page(token: str) -> dict[str, str]:
        raise RuntimeError("kaboom")

    app.include_router(pages.router)
    observability.install(app, settings, span_exporter=span_exporter)
    observability.install(app, settings, span_exporter=span_exporter)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return TestClient(app)


def test_request_id_generated_and_echoed() -> None:
    client = _client()
    generated = client.get("/ok")
    assert generated.status_code == 200
    assert len(generated.headers["X-Request-Id"]) >= 16
    echoed = client.get("/ok", headers={"X-Request-Id": "abc-123"})
    assert echoed.headers["X-Request-Id"] == "abc-123"
    bogus = client.get("/ok", headers={"X-Request-Id": "bad id\twith spaces"})
    assert bogus.headers["X-Request-Id"] != "bad id\twith spaces"


def test_security_headers() -> None:
    response = _client().get("/ok")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "max-age=" in response.headers["Strict-Transport-Security"]
    assert "Content-Security-Policy" in response.headers


def test_unhandled_exception_returns_500_json_with_request_id() -> None:
    response = _client().get("/boom", headers={"X-Request-Id": "req-500"})
    assert response.status_code == 500
    assert response.json() == {"detail": "internal error", "request_id": "req-500"}
    assert response.headers["X-Request-Id"] == "req-500"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_rate_limit_third_request_429() -> None:
    client = _client(SHOPLEGACY_RATE_LIMIT="2/minute")
    assert client.get("/ok").status_code == 200
    assert client.get("/ok").status_code == 200
    third = client.get("/ok")
    assert third.status_code == 429
    assert third.json()["detail"].startswith("rate limit exceeded")
    assert third.headers["X-Request-Id"]


def test_rate_limit_exempts_health() -> None:
    client = _client(SHOPLEGACY_RATE_LIMIT="2/minute")
    assert [client.get("/health").status_code for _ in range(4)] == [200] * 4


def test_rate_limit_applies_to_routes_from_included_routers() -> None:
    settings = load_settings({**DB_ENV, "SHOPLEGACY_RATE_LIMIT": "3/minute"})
    api = APIRouter()

    @api.get("/regions")
    def regions() -> list[str]:
        return []

    inner = APIRouter()

    @inner.get("/items/{item_id}")
    def item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    outer = APIRouter()
    outer.include_router(inner, prefix="/nested")
    app = FastAPI(dependencies=RATE_LIMITED)
    app.include_router(api, prefix="/api")
    app.include_router(outer, prefix="/api")
    observability.install(app, settings)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    client = TestClient(app)
    assert [client.get("/api/regions").status_code for _ in range(4)] == [200, 200, 200, 429]
    nested = [client.get("/api/nested/items/a").status_code for _ in range(4)]
    assert nested == [200, 200, 200, 429]
    assert [client.get("/health").status_code for _ in range(5)] == [200] * 5


def test_route_limit_replaces_default_limit() -> None:
    settings = load_settings({**DB_ENV, "SHOPLEGACY_RATE_LIMIT": "5/minute"})
    api = APIRouter()

    @api.post("/feedback")
    @observability.route_limit("1/minute")
    def feedback() -> dict[str, str]:
        return {"status": "ok"}

    @api.get("/regions")
    def regions() -> list[str]:
        return []

    app = FastAPI(dependencies=RATE_LIMITED)
    app.include_router(api, prefix="/api")
    observability.install(app, settings)
    client = TestClient(app)
    limited = client.post("/api/feedback")
    assert limited.status_code == 200
    second = client.post("/api/feedback", headers={"X-Request-Id": "req-429"})
    assert second.status_code == 429
    assert second.json()["detail"] == "rate limit exceeded: 1 per 1 minute"
    assert second.headers["X-Request-Id"] == "req-429"
    assert [client.get("/api/regions").status_code for _ in range(5)] == [200] * 5


def test_default_limit_counts_each_path_separately() -> None:
    settings = load_settings({**DB_ENV, "SHOPLEGACY_RATE_LIMIT": "2/minute"})
    app = FastAPI(dependencies=RATE_LIMITED)

    @app.post("/sessions/{session_id}/answer")
    def answer(session_id: str) -> dict[str, str]:
        return {"id": session_id}

    observability.install(app, settings)
    client = TestClient(app)
    first = [client.post("/sessions/a/answer").status_code for _ in range(3)]
    assert first == [200, 200, 429]
    assert [client.post("/sessions/b/answer").status_code for _ in range(2)] == [200, 200]


def test_route_limit_counts_across_paths_of_the_endpoint() -> None:
    settings = load_settings({**DB_ENV, "SHOPLEGACY_RATE_LIMIT": "50/minute"})
    app = FastAPI(dependencies=RATE_LIMITED)

    @app.post("/cards/{card_id}/feedback")
    @observability.route_limit("2/minute")
    def feedback(card_id: str) -> dict[str, str]:
        return {"id": card_id}

    observability.install(app, settings)
    client = TestClient(app)
    statuses = [client.post(f"/cards/{card}/feedback").status_code for card in "abc"]
    assert statuses == [200, 200, 429]


def test_rate_limit_dependency_leaves_websockets_alone() -> None:
    settings = load_settings({**DB_ENV, "SHOPLEGACY_RATE_LIMIT": "1/minute"})
    app = FastAPI(dependencies=RATE_LIMITED)

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        await websocket.send_text("hi")
        await websocket.close()

    observability.install(app, settings)
    client = TestClient(app)
    for _ in range(3):
        with client.websocket_connect("/ws") as conn:
            assert conn.receive_text() == "hi"


def test_json_log_single_line(capsys: pytest.CaptureFixture[str]) -> None:
    client = _client(SHOPLEGACY_LOG_FORMAT="json")
    capsys.readouterr()
    client.get("/ok", headers={"X-Request-Id": "req-log"})
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "request"
    assert record["request_id"] == "req-log"
    assert record["status"] == 200
    assert record["path"] == "/ok"


def test_json_log_for_exception_is_single_line(capsys: pytest.CaptureFixture[str]) -> None:
    client = _client(SHOPLEGACY_LOG_FORMAT="json")
    capsys.readouterr()
    client.get("/boom", headers={"X-Request-Id": "req-err"})
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    records = [json.loads(line) for line in lines]
    events = [r["event"] for r in records]
    assert events == ["unhandled_error", "request"]
    assert records[0]["request_id"] == "req-err"
    assert "kaboom" in records[0]["exception"]


NONCE_RE = re.compile(r"'nonce-([A-Za-z0-9_-]{16,})'")


def _nonce(csp: str) -> str:
    match = NONCE_RE.search(csp)
    assert match, csp
    return match.group(1)


def test_csp_header_has_fresh_nonce_and_policy() -> None:
    client = _client(SHOPLEGACY_SUPABASE_URL="https://abc.supabase.co")
    first = client.get("/ok").headers["Content-Security-Policy"]
    second = client.get("/ok").headers["Content-Security-Policy"]
    assert _nonce(first) != _nonce(second)
    assert "default-src 'self'" in first
    assert "script-src 'self' 'nonce-" in first
    assert "https://esm.sh" in first
    assert "connect-src 'self' https://esm.sh https://abc.supabase.co" in first
    assert "object-src 'none'" in first
    assert "frame-ancestors 'self'" in first
    assert "unsafe-inline" not in first.split("script-src", 1)[1].split(";", 1)[0]


def test_csp_without_supabase_url_has_no_extra_connect_source() -> None:
    csp = _client().get("/ok").headers["Content-Security-Policy"]
    assert "connect-src 'self' https://esm.sh;" in csp


def test_csp_on_billing_pages_allows_payment_widget() -> None:
    client = _client()
    for path in ("/billing", "/billing/pay-test"):
        csp = client.get(path).headers["Content-Security-Policy"]
        directives = {d.split(" ", 1)[0]: d for d in csp.split("; ")}
        assert "https://js.tosspayments.com" in directives["script-src"], csp
        assert "'nonce-" in directives["script-src"]
        assert "https://*.tosspayments.com" in directives["frame-src"], csp
        assert "https://*.tosspayments.com" in directives["connect-src"], csp
        assert "https://static.tosspayments.com" in directives["img-src"], csp
        assert "object-src 'none'" in csp


def test_csp_outside_billing_has_no_payment_sources() -> None:
    client = _client()
    for path in ("/", "/cards", "/ok"):
        csp = client.get(path).headers["Content-Security-Policy"]
        assert "tosspayments" not in csp, (path, csp)
        assert "frame-src" not in csp


def test_csp_payment_flag() -> None:
    plain = observability.content_security_policy("n", "")
    paid = observability.content_security_policy("n", "", payment=True)
    assert "tosspayments" not in plain
    assert "script-src 'self' 'nonce-n' https://esm.sh https://js.tosspayments.com;" in paid
    assert paid.endswith("frame-src 'self' https://js.tosspayments.com https://*.tosspayments.com")


def test_request_nonce_matches_header() -> None:
    response = _client().get("/nonce")
    assert response.text == _nonce(response.headers["Content-Security-Policy"])


def test_login_page_nonce_matches_header() -> None:
    response = _client().get("/login")
    assert response.status_code == 200
    nonce = _nonce(response.headers["Content-Security-Policy"])
    assert f'<script nonce="{nonce}">' in response.text
    assert observability.CSP_NONCE_PLACEHOLDER not in response.text
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["content-type"].startswith("text/html")


def test_static_response_has_no_csp() -> None:
    response = _client().get("/static/x.css")
    assert response.status_code == 200
    assert "Content-Security-Policy" not in response.headers
    assert "Content-Security-Policy-Report-Only" not in response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_csp_report_only_mode() -> None:
    response = _client(SHOPLEGACY_CSP_REPORT_ONLY="true").get("/ok")
    assert "Content-Security-Policy" not in response.headers
    assert "'nonce-" in response.headers["Content-Security-Policy-Report-Only"]


def test_csp_on_unhandled_error() -> None:
    response = _client().get("/boom")
    assert response.status_code == 500
    assert "'nonce-" in response.headers["Content-Security-Policy"]


def test_trusted_proxy_uses_forwarded_client_ip() -> None:
    client = _client(SHOPLEGACY_RATE_LIMIT="2/minute", SHOPLEGACY_TRUSTED_PROXIES="testclient")
    a = {"X-Forwarded-For": "2001:db8::1"}
    b = {"X-Forwarded-For": "2001:db8::2"}
    assert client.get("/ok", headers=a).status_code == 200
    assert client.get("/ok", headers=a).status_code == 200
    assert client.get("/ok", headers=b).status_code == 200
    assert client.get("/ok", headers=a).status_code == 429


def test_untrusted_proxy_ignores_forwarded_for() -> None:
    client = _client(SHOPLEGACY_RATE_LIMIT="2/minute")
    assert client.get("/ok", headers={"X-Forwarded-For": "2001:db8::1"}).status_code == 200
    assert client.get("/ok", headers={"X-Forwarded-For": "2001:db8::2"}).status_code == 200
    assert client.get("/ok", headers={"X-Forwarded-For": "2001:db8::3"}).status_code == 429


def test_other_proxy_is_not_trusted() -> None:
    client = _client(SHOPLEGACY_RATE_LIMIT="2/minute", SHOPLEGACY_TRUSTED_PROXIES="2001:db8::ff")
    assert client.get("/ok", headers={"X-Forwarded-For": "2001:db8::1"}).status_code == 200
    assert client.get("/ok", headers={"X-Forwarded-For": "2001:db8::2"}).status_code == 200
    assert client.get("/ok", headers={"X-Forwarded-For": "2001:db8::3"}).status_code == 429


def test_otel_exports_one_span_per_request() -> None:
    exporter = InMemorySpanExporter()
    client = _client(span_exporter=exporter, SHOPLEGACY_OTEL_SERVICE_NAME="sl-test")
    assert client.get("/ok").status_code == 200
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].resource.attributes["service.name"] == "sl-test"
    exporter.clear()
    client.get("/health")
    client.get("/static/x.css")
    assert exporter.get_finished_spans() == ()


def test_otel_disabled_without_endpoint_or_exporter() -> None:
    client = _client()
    assert client.get("/ok").status_code == 200
    assert getattr(client.app.state, "tracer_provider", None) is None  # type: ignore[attr-defined]


OWNER_TOKEN = "owner-tok-5f2a9c"


def test_owner_token_not_in_access_log(capsys: pytest.CaptureFixture[str]) -> None:
    client = _client(SHOPLEGACY_LOG_FORMAT="json")
    capsys.readouterr()
    assert client.get(f"/api/owner/{OWNER_TOKEN}/card").status_code == 200
    assert client.get(f"/owner/{OWNER_TOKEN}").status_code == 500
    out = capsys.readouterr().out
    records = [json.loads(line) for line in out.strip().splitlines()]
    paths = {r["path"] for r in records if "path" in r}
    assert {"/api/owner/{token}/card", "/owner/{token}"} <= paths
    assert OWNER_TOKEN not in out


def test_owner_token_not_in_span_attributes() -> None:
    exporter = InMemorySpanExporter()
    client = _client(span_exporter=exporter)
    assert client.get(f"/api/owner/{OWNER_TOKEN}/card", params={"q": "1"}).status_code == 200
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    attributes = dict(spans[0].attributes or {})
    assert attributes["http.target"] == "/api/owner/{token}/card"
    assert OWNER_TOKEN not in spans[0].name
    assert all(OWNER_TOKEN not in str(value) for value in attributes.values())


def test_redact_path() -> None:
    assert observability.redact_path("/owner/abc") == "/owner/{token}"
    assert observability.redact_path("/api/owner/abc/card?x=1") == "/api/owner/{token}/card?x=1"
    assert observability.redact_path("/api/cards/abc") == "/api/cards/abc"
