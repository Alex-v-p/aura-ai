import { defineConfig, devices } from '@playwright/test';

const baseURL = process.env['AURA_OBSERVATORY_FULL_STACK_BASE_URL'] ?? 'http://observatory.localhost:4300';
const browserHostResolverRules = process.env['AURA_OBSERVATORY_BROWSER_HOST_RESOLVER_RULES']?.trim() ?? '';
const hasControlCharacter = [...browserHostResolverRules].some((character) => {
  const codePoint = character.codePointAt(0) ?? 0;
  return codePoint <= 0x1f || codePoint === 0x7f;
});

if (browserHostResolverRules.length > 512 || hasControlCharacter) {
  throw new Error('AURA_OBSERVATORY_BROWSER_HOST_RESOLVER_RULES must be a bounded single-line value');
}

const browserLaunchOptions = browserHostResolverRules
  ? { launchOptions: { args: [`--host-resolver-rules=${browserHostResolverRules}`] } }
  : {};
const browserStorageState = process.env['AURA_OBSERVATORY_LOAD_STORAGE_STATE'] === '1'
  ? process.env['AURA_OBSERVATORY_STORAGE_STATE_PATH']
  : undefined;

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
    ...(browserStorageState ? { storageState: browserStorageState } : {}),
  },
  projects: [
    {
      name: 'observatory-full-stack',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 900 },
        ...browserLaunchOptions,
      },
    },
  ],
});
