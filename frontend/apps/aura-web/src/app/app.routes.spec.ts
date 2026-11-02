import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const source = readFileSync('apps/aura-web/src/app/app.routes.ts', 'utf8');

describe('Aura Web workflow routes', () => {
  it('keeps the four workflow destinations in the application shell', () => {
    expect(source).toContain("path: 'workflows'");
    expect(source).toContain("path: 'workflows/new'");
    expect(source).toContain("path: 'workflows/:id'");
    expect(source).toContain("path: 'workflows/:id/instances/:instanceId'");
    expect(source.match(/loadComponent/g)?.length).toBeGreaterThanOrEqual(4);
  });

  it('lazy-loads the feature library instead of importing implementation into the shell', () => {
    expect(source).toContain("import('@aura/aura/automation/workflows')");
    expect(source).not.toContain("from '@aura/aura/automation/workflows'");
  });

  it('declares the instance route before the opaque workflow id route', () => {
    expect(source.indexOf("path: 'workflows/:id/instances/:instanceId'")).toBeLessThan(source.indexOf("path: 'workflows/:id'"));
  });
});
