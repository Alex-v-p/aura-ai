# syntax=docker/dockerfile:1.7

ARG PYTHON_IMAGE=python:3.14.7-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.8.22

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/services/aura-core/src \
    UV_CACHE_DIR=/tmp/uv-cache

COPY --from=uv /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY services/aura-core/ ./services/aura-core/
COPY deploy/docker/aura-core-entrypoint.sh /usr/local/bin/aura-core-entrypoint

RUN uv sync --frozen --no-dev \
    && rm -rf "${UV_CACHE_DIR}" \
    && chmod 0555 /usr/local/bin/aura-core-entrypoint

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin aura \
    && chown -R aura:aura /app

USER aura

ENTRYPOINT ["/usr/local/bin/aura-core-entrypoint"]

CMD ["uv", "run", "python", "-c", "from aura_core.entrypoints.worker.app import main; main()"]
