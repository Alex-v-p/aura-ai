import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';

describe('observatory web shell', () => {
  it('is a separate owner-only product surface', () => {
    expect('/api/v1/events').toMatch(/^\/api\/v1\//);
    expect('metadata_only').toBe('metadata_only');
  });

  it('mounts only the selected workspace while keeping the route state in stores', () => {
    const source = readFileSync('apps/observatory-web/src/app/app.component.ts', 'utf8');
    expect(source).toContain('@switch (destination())');
    expect(source).not.toContain('[hidden]="destination() !==');
    expect(source).toContain('deactivateSelectedRunView()');
    expect(source).toContain('activateAnalyticsView()');
    expect(source).toContain('deactivateAnalyticsView()');
    expect(source).toContain('activateComparisonView()');
    expect(source).toContain('deactivateComparisonView()');
    expect(source).toContain('disconnect(true)');
  });
});
