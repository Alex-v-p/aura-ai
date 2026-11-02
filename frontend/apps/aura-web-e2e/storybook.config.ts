import { defineConfig, devices } from '@playwright/test';

const storybookPort = process.env['AURA_WEB_E2E_STORYBOOK_PORT'] ?? '6300';
const storybookBaseUrl =
  process.env['AURA_WEB_E2E_STORYBOOK_URL'] ?? `http://127.0.0.1:${storybookPort}`;
const reuseExistingServer = process.env['AURA_WEB_E2E_REUSE_SERVERS'] === '1';
const outputDir = process.env['AURA_WEB_E2E_OUTPUT_DIR'] ?? 'test-results/aura-web-storybook';

/** Storybook accessibility runs use a bounded desktop browser and no real API. */
export default defineConfig({
  testDir: 'src',
  testMatch: /storybook-a11y\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  outputDir,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  retries: 0,
  reporter: [['line']],
  use: {
    baseURL: storybookBaseUrl,
    // Avoid shared-worktree trace cleanup races (ENOENT) masking Storybook
    // accessibility results; diagnostics are not part of this matrix.
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
  webServer: {
    command: `cd ../.. && NX_DAEMON=false NX_ISOLATE_PLUGINS=false pnpm exec nx run storybook:storybook --host 127.0.0.1 --port ${storybookPort}`,
    url: storybookBaseUrl,
    reuseExistingServer,
    timeout: 180_000,
  },
  projects: [
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 } },
    },
  ],
});
