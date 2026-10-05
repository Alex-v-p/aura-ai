import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: 'apps/aura-web-e2e/src',
  fullyParallel: true,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: { baseURL: 'http://127.0.0.1:4200', trace: 'on-first-retry' },
  webServer: [
    { command: 'pnpm nx serve aura-web --host 0.0.0.0 --port 4200', url: 'http://127.0.0.1:4200', reuseExistingServer: true },
    { command: 'pnpm nx run storybook:storybook --host 0.0.0.0 --port 6006', url: 'http://127.0.0.1:6006', reuseExistingServer: true },
  ],
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    { name: 'tablet', use: { ...devices['iPad Mini'] } },
    { name: 'mobile', use: { ...devices['iPhone 13'] } },
  ],
});
