# Chromium is installed in a reviewed, immutable provider image build.
FROM mcr.microsoft.com/playwright/python:v1.56.0-noble@sha256:508b37a524219d210a67a7190beb079efda72553e0dcb9ef21d4dae8f8a7686b AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

COPY deploy/research/browser-provider-requirements.txt /tmp/provider-requirements.txt

RUN groupadd --system --gid 10002 aura-provider \
    && useradd --system --uid 10002 --gid 10002 --home-dir /nonexistent --no-create-home aura-provider \
    && pip install --no-cache-dir --disable-pip-version-check --require-hashes \
       -r /tmp/provider-requirements.txt \
    && rm -f /tmp/provider-requirements.txt
COPY services/aura-research-browser-fetcher/src /app/src
USER 10002:10002
EXPOSE 8080
ENTRYPOINT ["python", "-m", "uvicorn", "aura_research_browser_fetcher.app:app", "--host", "0.0.0.0", "--port", "8080"]
