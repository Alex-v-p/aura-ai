import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const component = readFileSync('libs/aura/knowledge/research/src/lib/research-page.component.ts', 'utf8');
const store = readFileSync('libs/aura/knowledge/research/src/lib/research-store.ts', 'utf8');
const template = readFileSync('libs/aura/knowledge/research/src/lib/research-page.component.html', 'utf8');

describe('ResearchPageComponent acceptance regressions', () => {
  it('clears bound draft signals only after authoritative admission succeeds', () => {
    expect(store).toContain('this.resetEpoch.update(value => value + 1)');
    expect(component).toContain('if (researchId) { this.discardDraft();');
    expect(component).toContain('this.fieldErrors.set({})');
    expect(component).toContain('this.store.create({ question: value');
  });

  it('focuses the rendered activity and deletion status headings after settlement', () => {
    expect(component).toContain('set activityHeadingView');
    expect(component).toContain('this.focusActivityHeading()');
    expect(component).toContain('set deletionStatusView');
    expect(component).toContain('this.focusDeletionStatus()');
    expect(template).toContain('#activityHeading tabindex="-1"');
    expect(template).toContain('#deletionStatusHeading');
  });

  it('consumes a one-shot id-keyed admission handoff only in the destination route', () => {
    expect(store).toContain('readonly admissionHandoff = signal<string | null>(null)');
    expect(store).toContain('consumeAdmissionHandoff(researchId: string)');
    expect(store).toContain('this.setAdmissionHandoff(admission.researchId)');
    expect(store).toContain('this.clearAdmissionHandoff(); this.api.clearSession()');
    expect(component).toContain('this.activityStarted.set(this.store.consumeAdmissionHandoff(id))');
    expect(component).toContain('if (this.activityStarted() && this.selectedId() === id)');
    expect(component).toContain('this.store.clearAdmissionHandoff(); this.store.clearSelection()');
  });

  it('keeps the shell as the only main landmark', () => {
    expect(template).not.toMatch(/<main\b/);
    expect(template).toContain('<div class="research-page" aria-labelledby="research-heading">');
    expect(template).toContain('<h1 id="research-heading">Research</h1>');
  });
});
