import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './src',
  testMatch: /observatory-workflows-storybook\.spec\.ts/,
  fullyParallel: true,
  retries: 1,
  reporter: [['line']],
  use: { baseURL: 'http://127.0.0.1:6006', trace: 'retain-on-failure' },
  webServer: { command: 'cd ../.. && pnpm exec nx run storybook:storybook --host 127.0.0.1 --port 6006', url: 'http://127.0.0.1:6006', reuseExistingServer: true, timeout: 120_000 },
  projects: [{ name: 'storybook-desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 1000 } } }],
});
