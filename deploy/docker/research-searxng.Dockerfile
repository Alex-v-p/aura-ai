# One authorized search deployable: stock SearXNG is loopback-only and the
# signed Aura adapter is its sole Core-facing listener.
FROM searxng/searxng:latest@sha256:fc2943975dea39558eb355b01b29c834785857c21516d16fa6d6078b1c5724b2

USER root
COPY deploy/research/browser-provider-requirements.txt /tmp/provider-requirements.txt
RUN /usr/local/searxng/.venv/bin/pip install --no-cache-dir --disable-pip-version-check --require-hashes \
       -r /tmp/provider-requirements.txt \
    && rm -f /tmp/provider-requirements.txt \
    && rm -rf /root/.cache

COPY deploy/research/searxng_adapter.py /usr/local/searxng/searxng_adapter.py
COPY deploy/research/searxng_entrypoint.sh /usr/local/searxng/searxng_entrypoint.sh
RUN chmod 0555 /usr/local/searxng/searxng_entrypoint.sh

USER 977:977
EXPOSE 8080
ENTRYPOINT ["/usr/local/searxng/searxng_entrypoint.sh"]
