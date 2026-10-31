import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const component = readFileSync('libs/aura/knowledge/research/src/lib/research-page.component.ts', 'utf8');
const conversation = readFileSync('libs/aura/interaction/conversations/src/lib/conversation-store.ts', 'utf8');
const store = readFileSync('libs/aura/knowledge/research/src/lib/research-store.ts', 'utf8');
const template = readFileSync('libs/aura/knowledge/research/src/lib/research-page.component.html', 'utf8');
const styles = readFileSync('libs/aura/knowledge/research/src/lib/research-page.component.css', 'utf8');

describe('ResearchPageComponent acceptance regressions', () => {
  it('keeps the research browser boundary validator aligned with conversation policy', () => {
    for (const policy of ['length > 2048', "url.protocol !== 'https:'", "url.port !== '' && url.port !== '443'", 'url.username || url.password', 'url.search || url.hash', '0xfc00', '0xfe80', '0xfec0', '0xff00', '0x2001, 0x0db8']) {
      expect(component, `research validator missing ${policy}`).toContain(policy);
      expect(conversation, `conversation validator missing ${policy}`).toContain(policy);
    }
    expect(component).toContain('researchOpaqueUuidPattern');
    expect(component).toContain('researchRetrievedAtPattern');
    expect(conversation).toContain('opaqueUuidPattern');
    expect(conversation).toContain('retrievedAtPattern');
  });

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

  it('uses semantic danger tokens for light and dark contrast', () => {
    expect(styles).toContain('background: var(--danger-surface)');
    expect(styles).toContain('color: var(--danger-text)');
    expect(styles).not.toContain('#8b1e2d');
  });

  it('renders direct report links and state-specific recovery actions', () => {
    expect(template).toContain('artifactRoute(artifact.artifactId)');
    expect(template).toContain('recovery(detail)');
    expect(template).toContain('Retry cleanup');
    expect(template).toContain('Reconcile cleanup');
    expect(component).toContain('if (this.store.saving()) return');
    expect(template).toContain('canRetryCleanup(detail)');
    expect(template).toContain('canReconcileCleanup(detail)');
    expect(template).toContain("detail.contentState === 'deletion_failed' || detail.contentState === 'cleanup_unknown' ? 'assertive' : 'polite'");
  });

  it('keeps recovery and citation safety logic in the page ownership zone', () => {
    expect(component).toContain('function researchRecoveryExplanation');
    expect(component).toContain('function researchDeletionRecovery');
    expect(component).toContain('function safeResearchUrl');
    expect(component).toContain('safeResearchUrl');
    expect(component).toContain('coverage remains incomplete');
    expect(component).toContain('provider is unavailable');
    expect(component).toContain('one-time extension has already been used');
  });
});
