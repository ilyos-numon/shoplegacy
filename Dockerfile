# Build context is the workspace root, because pyproject.toml pulls handoff from
# the sibling path ../handoff:
#   docker build -t shoplegacy:dev -f shoplegacy/Dockerfile .   (run in the workspace root)
# Only the paths listed below are copied, so .env, data/ and .venv never enter the image.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Path dependency, laid out at ../handoff relative to the project.
COPY handoff/pyproject.toml handoff/README.md handoff/LICENSE ./handoff/
COPY handoff/src/ ./handoff/src/

# Dependencies first (cached until the lock file changes), then the project.
COPY shoplegacy/pyproject.toml shoplegacy/uv.lock shoplegacy/README.md ./shoplegacy/
WORKDIR /app/shoplegacy
RUN uv sync --frozen --no-dev --no-editable --no-install-project

COPY shoplegacy/src/ ./src/
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12-slim

RUN useradd --create-home --uid 1000 app

WORKDIR /app/shoplegacy

# Same path as in the builder: the venv's console scripts have absolute shebangs.
COPY --from=builder --chown=app:app /app/shoplegacy/.venv ./.venv

RUN mkdir -p /app/data && chown app:app /app/data

USER app

ENV PATH=/app/shoplegacy/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    SHOPLEGACY_DATA_DIR=/app/data

EXPOSE 8830

# An empty host makes asyncio bind every interface, IPv4 and IPv6 ("::" alone is IPv6-only).
# It sits inside one shell string because some runtimes (Cloud Run) drop empty exec-form arguments.
CMD ["sh", "-c", "exec uvicorn shoplegacy.main:create_app --factory --host '' --port \"${PORT:-8830}\""]
