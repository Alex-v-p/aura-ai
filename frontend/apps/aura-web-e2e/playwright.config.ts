import { defineConfig, devices } from '@playwright/test';

const appPort = process.env['AURA_WEB_E2E_APP_PORT'] ?? '6500';
const appBaseUrl = process.env['AURA_WEB_E2E_BASE_URL'] ?? `http://127.0.0.1:${appPort}`;
const configuredWorkers = Number.parseInt(process.env['AURA_WEB_E2E_WORKERS'] ?? '1', 10);
const workers = Number.isFinite(configuredWorkers) && configuredWorkers > 0 ? configuredWorkers : 1;
const reuseExistingServer = process.env['AURA_WEB_E2E_REUSE_SERVERS'] === '1';
const outputDir = process.env['AURA_WEB_E2E_OUTPUT_DIR'] ?? 'test-results/aura-web-e2e';

/**
 * Deterministic browser acceptance for the governed research, Task Center,
 * and artifacts surface. This config owns only a loopback Aura Web server;
 * provider credentials and real research services are never required.
 * Use playwright.full-stack.config.ts for the separately gated real stack.
 */
export default defineConfig({
  testDir: 'src',
  testMatch: /research-tasks-artifacts\.spec\.ts/,
  fullyParallel: false,
  workers,
  outputDir,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: appBaseUrl,
    // Keep the synthetic matrix deterministic in shared worktrees. Trace
    // teardown can race the worker's artifact cleanup and produce ENOENT
    // failures unrelated to the assertion under test.
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
  webServer: {
    command: `NX_DAEMON=false NX_ISOLATE_PLUGINS=false pnpm exec nx serve aura-web --host 127.0.0.1 --port ${appPort}`,
    url: appBaseUrl,
    reuseExistingServer,
    timeout: 180_000,
  },
  projects: [
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } },
    },
    { name: 'tablet', use: { ...devices['iPad Mini'], browserName: 'chromium' } },
    { name: 'mobile', use: { ...devices['iPhone 13'], browserName: 'chromium' } },
  ],
});
