import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

export default defineConfig({
  plugins: [{
    name: 'briefing-store-legacy-decorators',
    enforce: 'pre',
    transform(code, id) {
      if (!id.endsWith('/libs/aura/automation/briefings/src/lib/briefing-store.ts')) return undefined;
      const output = ts.transpileModule(code, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022, experimentalDecorators: true, useDefineForClassFields: false, sourceMap: true }, fileName: id });
      return { code: output.outputText, map: output.sourceMapText ? JSON.parse(output.sourceMapText) : null };
    },
  }],
  resolve: {
    alias: {
      '@aura/aura-api-client': fileURLToPath(
        new URL('./libs/platform/aura-api-client/src/index.ts', import.meta.url),
      ),
      '@aura/observatory-api-client': fileURLToPath(
        new URL('./libs/platform/observatory-api-client/src/index.ts', import.meta.url),
      ),
      '@aura/observatory': fileURLToPath(
        new URL('./libs/observatory/src/index.ts', import.meta.url),
      ),
      '@aura/aura/automation/reminders': fileURLToPath(
        new URL('./libs/aura/automation/reminders/src/index.ts', import.meta.url),
      ),
      '@aura/aura/automation/briefings': fileURLToPath(
        new URL('./libs/aura/automation/briefings/src/index.ts', import.meta.url),
      ),
      '@aura/aura/integrations/calendar': fileURLToPath(
        new URL('./libs/aura/integrations/calendar/src/lib/calendar-store.ts', import.meta.url),
      ),
      '@aura/aura/interaction/agents': fileURLToPath(
        new URL('./libs/aura/interaction/agents/src/lib/agent-store.ts', import.meta.url),
      ),
      '@aura/aura/knowledge/documents': fileURLToPath(
        new URL('./libs/aura/knowledge/documents/src/lib/documents-store.ts', import.meta.url),
      ),
    },
  },
  test: {
    environment: 'jsdom',
    include: [
      'apps/aura-web/src/**/*.spec.ts',
      'apps/observatory-web/src/**/*.spec.ts',
      'libs/**/*.spec.ts',
    ],
    exclude: ['apps/aura-web-e2e/**'],
    reporters: ['default'],
  },
});
