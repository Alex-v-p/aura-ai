import { defineConfig, devices } from '@playwright/test';

const appPort = process.env['AURA_OBSERVATORY_WEB_E2E_APP_PORT'] ?? '4300';
const appBaseUrl = process.env['AURA_OBSERVATORY_WEB_E2E_BASE_URL'] ?? `http://127.0.0.1:${appPort}`;
const configuredWorkers = Number.parseInt(process.env['AURA_OBSERVATORY_WEB_E2E_WORKERS'] ?? '1', 10);
const workers = Number.isFinite(configuredWorkers) && configuredWorkers > 0 ? configuredWorkers : 1;
const reuseExistingServer = process.env['AURA_OBSERVATORY_WEB_E2E_REUSE_SERVERS'] === '1';

/**
 * Mocked browser acceptance is intentionally the default: it is deterministic,
 * bounded, and exercises the browser contract without requiring databases or
 * an identity provider. The real-stack job uses full-stack.config.ts below.
 */
export default defineConfig({
  testDir: 'src',
  testMatch: /observatory(?:-web|-evaluation|-experiments|-operations)\.spec\.ts/,
  fullyParallel: false,
  workers,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: appBaseUrl,
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
  webServer: process.env['AURA_OBSERVATORY_WEB_E2E_SKIP_SERVER'] === '1'
    ? undefined
    : {
        command: `NX_DAEMON=false NX_ISOLATE_PLUGINS=false pnpm nx serve observatory-web --host 127.0.0.1 --port ${appPort}`,
        url: appBaseUrl,
        reuseExistingServer,
        timeout: 180_000,
      },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    { name: 'tablet', use: { ...devices['iPad Mini'], browserName: 'chromium' } },
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
  ],
});
