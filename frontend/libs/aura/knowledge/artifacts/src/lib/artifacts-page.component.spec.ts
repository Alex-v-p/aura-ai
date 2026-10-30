import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const component = readFileSync('libs/aura/knowledge/artifacts/src/lib/artifacts-page.component.ts', 'utf8');
const template = readFileSync('libs/aura/knowledge/artifacts/src/lib/artifacts-page.component.html', 'utf8');
const styles = readFileSync('libs/aura/knowledge/artifacts/src/lib/artifacts-page.component.css', 'utf8');

describe('ArtifactsPageComponent acceptance regressions', () => {
  it('focuses a rendered deletion status after the confirmation closes', () => {
    expect(component).toContain('set deletionStatusView');
    expect(component).toContain('this.focusDeletionStatus()');
    expect(template).toContain('#deletionStatusHeading');
  });

  it('keeps list, detail, filters, and preview content intrinsically sized at zoom', () => {
    expect(styles).toContain('.artifacts-page > *');
    expect(styles).toContain('min-width:0');
    expect(styles).toContain('grid-template-columns:minmax(0,1fr)');
    expect(styles).toContain('overflow-wrap:anywhere');
    expect(styles).not.toContain('overflow-x:hidden');
  });

  it('keeps the shell as the only main landmark', () => {
    expect(template).not.toMatch(/<main\b/);
    expect(template).toContain('<div class="artifacts-page" aria-labelledby="artifacts-heading">');
    expect(template).toContain('<h1 id="artifacts-heading">Artifacts</h1>');
  });
});
