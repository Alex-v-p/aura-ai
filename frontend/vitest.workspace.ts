import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  resolve: { alias: { '@aura/aura-api-client': fileURLToPath(new URL('./libs/platform/aura-api-client/src/index.ts', import.meta.url)), '@aura/observatory-api-client': fileURLToPath(new URL('./libs/platform/observatory-api-client/src/index.ts', import.meta.url)), '@aura/observatory': fileURLToPath(new URL('./libs/observatory/src/index.ts', import.meta.url)), '@aura/aura/automation/reminders': fileURLToPath(new URL('./libs/aura/automation/reminders/src/index.ts', import.meta.url)) } },
  test: {
    environment: 'jsdom',
    include: ['apps/aura-web/src/**/*.spec.ts', 'apps/observatory-web/src/**/*.spec.ts', 'libs/**/*.spec.ts'],
    exclude: ['apps/aura-web-e2e/**'],
    reporters: ['default'],
  },
});
