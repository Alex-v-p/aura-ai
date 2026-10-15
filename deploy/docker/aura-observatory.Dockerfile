# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.14.7-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.22

FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/services/aura-observatory/src \
    UV_CACHE_DIR=/tmp/uv-cache

COPY --from=uv /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY services/aura-observatory/ ./services/aura-observatory/

RUN uv sync --frozen --no-dev \
    && rm -rf "${UV_CACHE_DIR}"

RUN useradd --create-home --uid 10002 --shell /usr/sbin/nologin observatory \
    && chown -R observatory:observatory /app

USER observatory
EXPOSE 8000

FROM runtime AS api
CMD ["/app/.venv/bin/uvicorn", "aura_observatory.entrypoints.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM runtime AS worker
CMD ["/app/.venv/bin/python", "-m", "aura_observatory.entrypoints.worker"]

FROM runtime AS migrate
CMD ["/app/.venv/bin/python", "-m", "aura_observatory.entrypoints.cli", "migrate"]
