import { defineConfig, devices } from '@playwright/test';

const appPort = process.env.AURA_WEB_E2E_APP_PORT ?? '4300';
const storybookPort = process.env.AURA_WEB_E2E_STORYBOOK_PORT ?? '6300';
const appBaseUrl = `http://127.0.0.1:${appPort}`;
const storybookBaseUrl = `http://127.0.0.1:${storybookPort}`;
const configuredWorkers = Number.parseInt(process.env.AURA_WEB_E2E_WORKERS ?? '4', 10);
const workers = Number.isFinite(configuredWorkers) && configuredWorkers > 0 ? configuredWorkers : 4;
// Fresh current-source servers are the default. Reusing an existing process is
// intentionally explicit so Compose or another stale dev server cannot satisfy
// this configuration accidentally.
const reuseExistingServer = process.env.AURA_WEB_E2E_REUSE_SERVERS === '1';

export default defineConfig({
  testDir: 'apps/aura-web-e2e/src',
  fullyParallel: true,
  workers,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: { baseURL: appBaseUrl, trace: 'on-first-retry' },
  webServer: [
    { command: `NX_DAEMON=false NX_ISOLATE_PLUGINS=false pnpm nx serve aura-web --host 127.0.0.1 --port ${appPort}`, url: appBaseUrl, reuseExistingServer, timeout: 180_000 },
    { command: `NX_DAEMON=false NX_ISOLATE_PLUGINS=false pnpm nx run storybook:storybook --host 127.0.0.1 --port ${storybookPort}`, url: storybookBaseUrl, reuseExistingServer, timeout: 180_000 },
  ],
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    // Tablet/mobile projects cover responsive form factors and input behavior;
    // Chromium keeps this matrix independent from the host's WebKit runtime.
    { name: 'tablet', use: { ...devices['iPad Mini'], browserName: 'chromium' } },
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
  ],
});
