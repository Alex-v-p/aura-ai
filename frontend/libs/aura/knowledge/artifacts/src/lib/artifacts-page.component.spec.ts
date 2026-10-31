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

  it('uses semantic danger tokens for light and dark contrast', () => {
    expect(styles).toContain('background:var(--danger-surface)');
    expect(styles).toContain('color:var(--danger-text)');
    expect(styles).not.toContain('#8b1e2d');
  });

  it('keeps the shell as the only main landmark', () => {
    expect(template).not.toMatch(/<main\b/);
    expect(template).toContain('<div class="artifacts-page" aria-labelledby="artifacts-heading">');
    expect(template).toContain('<h1 id="artifacts-heading">Artifacts</h1>');
  });

  it('keeps cleanup actions disabled during saving and exposes live recovery copy', () => {
    expect(template).toContain('[disabled]="store.saving()"');
    expect(template).toContain("detail.contentState === 'deletion_failed' || detail.contentState === 'cleanup_unknown' ? 'assertive' : 'polite'");
    expect(component).toContain('await this.store.retryCleanup()');
    expect(component).toContain('await this.store.reconcileCleanup()');
    expect(component).toContain("recovery?.action==='retry_cleanup'");
    expect(component).toContain("recovery?.action==='reconcile_cleanup'");
  });

  it('keeps cleanup recovery states and support references in the page ownership zone', () => {
    expect(component).toContain('function artifactDeletionRecovery');
    expect(component).toContain('deletion_failed');
    expect(component).toContain('cleanup_unknown');
    expect(component).toContain('AR-');
  });
});
