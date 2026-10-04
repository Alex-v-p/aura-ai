import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['apps/aura-web/src/**/*.spec.ts', 'libs/**/*.spec.ts'],
    exclude: ['apps/aura-web-e2e/**'],
    reporters: ['default'],
  },
});
