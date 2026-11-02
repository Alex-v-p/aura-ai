import { defineConfig, devices } from '@playwright/test';

/**
 * Real-stack configuration.  It deliberately has no route interception and
 * no webServer: Compose owns Aura Web, Core, the worker, PostgreSQL, NATS,
 * Valkey, Ollama, and the selected OIDC provider.
 *
 * Required at runtime:
 *   BASE_URL or AURA_E2E_BASE_URL       Aura Web origin
 *   AURA_E2E_IDENTITY_MODES             local-identity,external-oidc (or one)
 *   AURA_E2E_<MODE>_ISSUER              configured stable OIDC issuer
 *   AURA_E2E_<MODE>_IDENTITY_URL        browser-reachable login origin
 *   AURA_E2E_<MODE>_OWNER_USERNAME/PASSWORD
 *
 * The Compose fixture should expose at least two selectable chat model IDs
 * (the current fixture uses `fixture-chat` plus a second chat model) and one
 * disabled embedding-only ID (`embed` or the configured equivalent), with
 * chat returning a stable response after one or more streamed deltas.  A
 * prompt beginning `background-run-` must remain running for several seconds
 * so navigation/background execution is observable.  It should keep its browser-facing Aura origin in
 * `BASE_URL` and make the identity origin reachable from the Playwright
 * container.  No provider URL or credential is committed here.
 */
export default defineConfig({
  testDir: 'src',
  testMatch: /full-stack\.spec\.ts/,
  fullyParallel: false,
  timeout: 120_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: process.env['AURA_E2E_BASE_URL'] ?? process.env['BASE_URL'],
    trace: 'retain-on-failure',
  },
  projects: [
    { name: 'full-stack-desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
  ],
});
