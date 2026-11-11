"""Application factory entry point (run with ``uvicorn --factory``)."""

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from shoplegacy import app_state, observability
from shoplegacy.app_state import build_state
from shoplegacy.config import load_settings
from shoplegacy.pages import STATIC_DIR
from shoplegacy.routers import ALL_ROUTERS


def create_app() -> FastAPI:
    settings = load_settings()
    app = FastAPI(
        title="가게유산",
        dependencies=[Depends(observability.enforce_rate_limit)],
        lifespan=app_state.lifespan,
    )
    app.state.shop = build_state(settings)
    for router, prefix in ALL_ROUTERS:
        app.include_router(router, prefix=prefix)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    observability.install(app, settings)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
