# syntax=docker/dockerfile:1.7

ARG NODE_IMAGE=node:24.8.0-bookworm-slim
ARG PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright:v1.63.0-noble
ARG PNPM_VERSION=12.5.0

FROM ${NODE_IMAGE} AS base

ARG PNPM_VERSION
ENV CI=true

RUN npm install --global --ignore-scripts "pnpm@${PNPM_VERSION}" \
    && pnpm --version

WORKDIR /workspace

FROM base AS dependencies

COPY frontend/ ./frontend/
RUN pnpm --dir frontend install --frozen-lockfile

FROM dependencies AS build

RUN pnpm --dir frontend exec nx build observatory-web --configuration=production

FROM nginx:1.29.2-alpine AS runtime

COPY deploy/docker/observatory-web.nginx.conf /etc/nginx/nginx.conf
COPY --from=build --chown=nginx:nginx /workspace/frontend/dist/apps/observatory-web/browser/ /usr/share/nginx/html/

USER nginx
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=5 --start-period=5s \
  CMD wget --quiet --output-document=- http://127.0.0.1:8080/healthz | grep -q '^ok$'

ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
