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

RUN pnpm --dir frontend exec nx build aura-web --configuration=production
RUN pnpm --dir frontend run pwa:inject

FROM dependencies AS frontend-checks

CMD ["sh", "-c", "pnpm --dir frontend run lint && pnpm --dir frontend run test && pnpm --dir frontend run build:production && pnpm --dir frontend run pwa:inject && pnpm --dir frontend run build-storybook"]

FROM ${PLAYWRIGHT_IMAGE} AS frontend-e2e

ARG PNPM_VERSION
ENV CI=true

RUN npm install --global --ignore-scripts "pnpm@${PNPM_VERSION}" \
    && pnpm --version

WORKDIR /workspace
COPY --from=dependencies /workspace/frontend/ ./frontend/

RUN pnpm --dir frontend exec playwright install chromium webkit

RUN printf '%s\n' \
  "import { defineConfig, devices } from '@playwright/test';" \
  "export default defineConfig({" \
  "  testDir: '/workspace/frontend/apps/aura-web-e2e/src'," \
  "  fullyParallel: true," \
  "  reporter: [['list'], ['html', { open: 'never' }]]," \
  "  use: { baseURL: process.env.BASE_URL ?? 'http://aura-web:8080', trace: 'on-first-retry' }," \
  "  webServer: { command: 'pnpm --dir frontend exec nx run storybook:storybook --host 0.0.0.0 --port 6006', url: 'http://localhost:6006', timeout: 120000, reuseExistingServer: false }," \
  "  projects: [" \
  "    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } }," \
  "    { name: 'tablet', use: { ...devices['iPad Mini'] } }," \
  "    { name: 'mobile', use: { ...devices['iPhone 13'] } }," \
  "  ]," \
  "});" > /workspace/playwright.compose.config.ts

FROM nginx:1.29.2-alpine AS runtime

COPY deploy/docker/aura-web.nginx.conf /etc/nginx/nginx.conf
COPY --from=build --chown=nginx:nginx /workspace/frontend/dist/apps/aura-web/browser/ /usr/share/nginx/html/

USER nginx
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --retries=5 --start-period=5s \
  CMD wget --quiet --output-document=- http://127.0.0.1:8080/healthz | grep -q '^ok$'

ENTRYPOINT ["nginx"]
CMD ["-g", "daemon off;"]
