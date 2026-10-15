import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env['AURA_OBSERVATORY_FULL_STACK_BASE_URL'] ?? 'http://observatory.localhost:4300';

/**
 * Compose owns the full stack. This config deliberately has no webServer and
 * never provisions credentials. deploy/compose/run-observatory-full-stack-e2e.sh
 * supplies the owner identity and starts the disposable observatory profile.
 */
export default defineConfig({
  testDir: 'src',
  testMatch: /full-stack\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL,
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
  projects: [
    { name: 'observatory-full-stack', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
  ],
});
