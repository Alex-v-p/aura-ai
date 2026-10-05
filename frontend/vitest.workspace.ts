import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  resolve: { alias: { '@aura/aura-api-client': fileURLToPath(new URL('./libs/platform/aura-api-client/src/index.ts', import.meta.url)) } },
  test: {
    environment: 'jsdom',
    include: ['apps/aura-web/src/**/*.spec.ts', 'libs/**/*.spec.ts'],
    exclude: ['apps/aura-web-e2e/**'],
    reporters: ['default'],
  },
});
