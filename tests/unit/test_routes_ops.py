from types import SimpleNamespace

import psycopg
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from shoplegacy import observability, pages, routes_ops
from shoplegacy.config import load_settings
from shoplegacy.routers import ALL_ROUTERS
from shoplegacy.store import Store

SECRET_DETAIL = "password authentication failed for user secret_db_user"


class _BrokenStore:
    def ping(self) -> None:
        raise psycopg.OperationalError(SECRET_DETAIL)


def _app(store: object) -> FastAPI:
    app = FastAPI()
    app.state.shop = SimpleNamespace(store=store)
    app.include_router(routes_ops.router)
    return app


def test_store_ping_succeeds(app_dsn: str) -> None:
    store = Store(app_dsn)
    try:
        store.ping()
    finally:
        store.close()


def test_readyz_ok_with_database(app_dsn: str) -> None:
    store = Store(app_dsn)
    try:
        response = TestClient(_app(store)).get("/readyz")
    finally:
        store.close()
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


def test_readyz_degraded_without_leaking_error() -> None:
    with capture_logs() as logs:
        response = TestClient(_app(_BrokenStore())).get("/readyz")
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "db": "error"}
    assert SECRET_DETAIL not in response.text
    assert any(
        entry["event"] == "readyz_db_error" and entry["log_level"] == "warning" for entry in logs
    )


def test_readyz_exempt_from_rate_limit_and_tracing() -> None:
    assert "/readyz" in observability.EXEMPT_PATHS
    assert "readyz" in observability.OTEL_EXCLUDED_URLS.split(",")
    settings = load_settings(
        {
            "SHOPLEGACY_DATABASE_URL": "postgresql://unused@localhost/unused",
            "SHOPLEGACY_JWT_SECRET": "test-secret",
            "SHOPLEGACY_RATE_LIMIT": "2/minute",
        }
    )

    class _OkStore:
        def ping(self) -> None:
            return None

    app = FastAPI(dependencies=[Depends(observability.enforce_rate_limit)])
    app.state.shop = SimpleNamespace(store=_OkStore())
    app.include_router(routes_ops.router)
    observability.install(app, settings)
    client = TestClient(app)
    assert [client.get("/readyz").status_code for _ in range(4)] == [200] * 4


def test_ops_router_mounted_before_pages() -> None:
    routers = [router for router, _ in ALL_ROUTERS]
    assert (routes_ops.router, "") in ALL_ROUTERS
    assert routers.index(routes_ops.router) < routers.index(pages.router)
