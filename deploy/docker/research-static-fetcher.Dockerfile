# The digest is updated only as part of the reviewed provider image refresh.
FROM python:3.14-slim@sha256:ec6a14df5b63e3fbb3498e3240f7f640c230f9dca67683d23265db3866697f2d AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src
WORKDIR /app

COPY deploy/research/provider-requirements.txt /tmp/provider-requirements.txt

RUN addgroup --system --gid 10001 aura-provider \
    && adduser --system --uid 10001 --ingroup aura-provider --home /nonexistent --no-create-home aura-provider \
    && pip install --no-cache-dir --disable-pip-version-check --require-hashes \
       -r /tmp/provider-requirements.txt \
    && rm -f /tmp/provider-requirements.txt
COPY services/aura-research-static-fetcher/src /app/src
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["python", "-m", "uvicorn", "aura_research_static_fetcher.app:app", "--host", "0.0.0.0", "--port", "8080"]
