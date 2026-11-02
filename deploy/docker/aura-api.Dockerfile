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
COPY deploy/docker/aura-core-migrate.sh /usr/local/bin/aura-core-migrate

RUN uv sync --frozen --no-dev \
    && rm -rf "${UV_CACHE_DIR}" \
    && chmod 0555 /usr/local/bin/aura-core-entrypoint /usr/local/bin/aura-core-migrate

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin aura \
    && chown -R aura:aura /app

USER aura
EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/aura-core-entrypoint"]

HEALTHCHECK --interval=10s --timeout=3s --retries=10 --start-period=15s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2)"

CMD ["uv", "run", "uvicorn", "aura_core.entrypoints.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

# Test-only provider runtime. It is selected exclusively by the bounded
# deployment E2E Compose fixtures and is not part of the normal Aura stack.
FROM runtime AS e2e-provider

COPY deploy/compose/e2e/fake_provider.py /fixture/fake_provider.py

WORKDIR /fixture
CMD ["/app/.venv/bin/uvicorn", "fake_provider:app", "--host", "0.0.0.0", "--port", "8081", "--no-access-log"]
