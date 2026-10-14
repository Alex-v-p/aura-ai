import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { branchNavigationAvailable } from './branch-navigation';

const sourceRoot = resolve(process.cwd(), 'libs/aura/interaction/conversations/src/lib');
const template = readFileSync(resolve(sourceRoot, 'conversation-panel.component.html'), 'utf8');
const panelSource = readFileSync(resolve(sourceRoot, 'conversation-panel.component.ts'), 'utf8');
const storeSource = readFileSync(resolve(sourceRoot, 'conversation-store.ts'), 'utf8');
const apiSource = readFileSync(resolve(sourceRoot, 'conversation-api.ts'), 'utf8');
const branchNavigationSource = readFileSync(resolve(sourceRoot, 'branch-navigation.ts'), 'utf8');

describe('conversation branching web contract', () => {
  const styles = readFileSync(resolve(sourceRoot, 'conversation-panel.component.css'), 'utf8');

  it('disables previous and next exactly at the active branch boundaries', () => {
    const branches = [{ id: 'root' }, { id: 'alternative' }, { id: 'latest' }];
    expect(branchNavigationAvailable(branches, 'root', -1)).toBe(false);
    expect(branchNavigationAvailable(branches, 'root', 1)).toBe(true);
    expect(branchNavigationAvailable(branches, 'latest', -1)).toBe(true);
    expect(branchNavigationAvailable(branches, 'latest', 1)).toBe(false);
  });

  it('exposes keyboard-operable edit, regenerate, and branch navigation controls', () => {
    expect(template).toContain('Edit and branch');
    expect(template).toContain('Regenerate');
    expect(template).toContain('Previous conversation branch');
    expect(template).toContain('Next conversation branch');
    expect(template).toContain('This creates a new branch and keeps the original message unchanged.');
    expect(template).toContain('[disabled]="store.selectedBranch().loading || controlsDisabled()"');
    expect(panelSource).toContain('handleBranchEditKeydown');
    expect(panelSource).toContain('event.metaKey || event.ctrlKey');
    expect(panelSource).toContain('canNavigateBranch(direction');
    expect(branchNavigationSource).toContain('index > 0');
    expect(branchNavigationSource).toContain('index < branches.length - 1');
  });

  it('moves focus into edit mode and returns it to the initiating action', () => {
    expect(template).toContain('[id]="\'edit-branch-\' + turn.id"');
    expect(template).toContain('(click)="beginEditMessage(turn.id)"');
    expect(template).toContain('(click)="cancelBranchEdit()"');
    expect(panelSource).toContain("document.getElementById(`branch-edit-${messageId}`)?.focus()");
    expect(panelSource).toContain('private branchEditTriggerId: string | null = null');
    expect(panelSource).toContain('private previousBranchEditMessageId: string | null = null');
    expect(panelSource).toContain('if (wasEditing && !editingMessageId && !loading) this.requestBranchEditFocus()');
    expect(panelSource).toContain('implements AfterViewChecked');
    expect(panelSource).toContain('private readonly pendingBranchEditFocus = signal<BranchEditFocusRequest | null>(null)');
    expect(panelSource).toContain('this.changeDetector.markForCheck()');
    expect(panelSource).toContain('ngAfterViewChecked(): void');
    expect(panelSource).toContain('focusBranchEditTarget(request)');
    expect(readFileSync(resolve(sourceRoot, 'branch-edit-focus.ts'), 'utf8')).toContain('document.activeElement === target');
    expect(template).toContain('data-branch-edit-trigger');
    expect(template).toContain('id="branch-toolbar-heading" tabindex="-1"');
    expect(styles).toContain('var(--focus)');
    expect(styles).toContain('var(--text-muted)');
    expect(styles).toContain('var(--control-border)');
    expect(styles).not.toContain('var(--muted)');
    expect(styles).not.toContain('var(--border-strong');
  });

  it('keeps browser branch state opaque and out of durable client storage', () => {
    expect(panelSource).toContain("url.searchParams.set('branch', branchId)");
    expect(panelSource).toContain('window.history.replaceState');
    expect(panelSource).toContain('/^[A-Za-z0-9_-]{1,128}$/u');
    expect(panelSource).not.toMatch(/localStorage|sessionStorage|indexedDB/iu);
  });

  it('uses the generated branch operations with CSRF and idempotency headers', () => {
    expect(apiSource).toContain("this.client.execute('listConversationBranches'");
    expect(apiSource).toContain("this.client.execute('getConversationBranch'");
    expect(apiSource).toContain("this.client.execute('createConversationBranch'");
    expect(apiSource).toContain("this.client.execute('regenerateRun'");
    expect(apiSource).toContain("'X-CSRF-Token': this.csrfToken()");
    expect(apiSource).toContain("'Idempotency-Key': idempotencyKey");
    expect(apiSource).toContain('cursor?: string');
  });

  it('keeps list and action failures recoverable without pretending a list retry fixes them', () => {
    expect(template).toContain('(click)="retryBranchAction()"');
    expect(panelSource).toContain('await retryBranchActionAndFocus(action?.kind');
    expect(readFileSync(resolve(sourceRoot, 'branch-edit-focus.ts'), 'utf8')).toContain('const isEdit = actionKind === \'edit\';');
    expect(readFileSync(resolve(sourceRoot, 'branch-edit-focus.ts'), 'utf8')).toContain('const succeeded = await retry();');
    expect(readFileSync(resolve(sourceRoot, 'branch-edit-focus.ts'), 'utf8')).toContain('if (isEdit && succeeded) requestFocus();');
    expect(storeSource).toContain('async retryBranchAction(): Promise<boolean>');
    expect(storeSource).toContain("retryActionFor(error, { kind: 'list' })");
    expect(storeSource).toContain("errorAction: { kind: 'edit', messageId }");
    expect(storeSource).toContain("errorAction: { kind: 'regenerate', messageId }");
    expect(storeSource).toContain("retryActionFor(error, { kind: 'select', branchId })");
    expect(storeSource).toContain('page.nextCursor');
    expect(storeSource).toContain('retryActionFor(error, { kind: \'list\' })');
    expect(storeSource).toContain('retryActionFor(error, { kind: \'select\', branchId })');
    expect(storeSource).toContain('isDefinitiveBranchFailure');
    expect(storeSource).toContain('errorAction: null');
  });

  it('refreshes optimistic versions before offering a conflict retry', () => {
    expect(storeSource).toContain("await this.recoverBranchConflict(conversation.id, { kind: 'edit', messageId });");
    expect(storeSource).toContain("await this.recoverBranchConflict(conversation.id, { kind: 'regenerate', messageId });");
    expect(storeSource).toContain('const refreshed = await this.refresh(id);');
    expect(storeSource).toContain('await this.loadBranches(id);');
    expect(storeSource).toContain('latest version is loaded; review your draft and retry the branch action.');
  });

  it('keeps an opaque deep-link branch selected while its list and detail requests race', () => {
    expect(panelSource).toContain('if (!branch.loading) void this.store.selectBranch(requested);');
    expect(panelSource).toContain('Keep the requested id pending');
    expect(storeSource).toContain('const latest = this.find(conversationId);');
    expect(storeSource).toContain('const preferredActiveId = latest?.branch?.activeId ?? conversation.branch?.activeId;');
  });

  it('hydrates only the authorized branch path and preserves source turns', () => {
    expect(storeSource).toContain('fromBranchDetail');
    expect(storeSource).toContain('detail.messages');
    expect(storeSource).toContain('slice(0, Math.max(0');
    expect(storeSource).toContain('activeId: branch.id');
  });

  it('reuses intent keys across ambiguous transport retries and reconciles first', () => {
    expect(storeSource).toContain('const intent = this.branchIntent(conversation.id, {');
    expect(storeSource).toContain('Replay the exact original request after an unknown outcome.');
    expect(storeSource).toContain('intent.version');
    expect(storeSource).toContain('intent.sourceBranchId');
    expect(storeSource).toContain('const reconciled = await this.refresh(conversation.id);');
    expect(storeSource).toContain('unknownBranchOutcomeMessage(reconciled)');
    expect(storeSource).toContain('clearBranchIntent(conversation.id);');
  });

  it('gates send and configuration while branch state is loading without disabling drafting', () => {
    expect(storeSource).toContain('!this.selectedBranch().loading');
    expect(panelSource).toContain('selectedBranch().loading');
    expect(template).toContain('[value]="store.draft()"');
    expect(template).not.toContain('[disabled]="store.selectedBranch().loading" (input)');
  });

  it('maps branch failures to safe copy and uses a dedicated announcement', () => {
    expect(storeSource).toContain("private branchErrorMessage(error: unknown, action: 'list' | 'select' | 'edit' | 'regenerate' = 'list')");
    expect(storeSource).toContain('You do not have permission to edit this conversation.');
    expect(storeSource).toContain('You do not have permission to regenerate this response.');
    expect(storeSource).toContain('Aura could not complete that branch action.');
    expect(template).toContain('role="status" aria-live="polite"');
    expect(template).not.toContain('<div class="transcript" aria-live=');
    expect(template).toContain('summaries.length > 1 || store.selectedBranch().error || store.selectedBranch().loading');
  });

  it('restores navigation and regeneration focus and exposes touch-sized actions', () => {
    expect(panelSource).toContain('private restoreBranchActionFocus(trigger: HTMLButtonElement | null)');
    expect(readFileSync(resolve(sourceRoot, 'branch-action-focus.ts'), 'utf8')).toContain("document.getElementById(fallbackId)?.focus()");
    expect(template).toContain('(click)="navigateBranch(-1, $event)"');
    expect(template).toContain('(click)="regenerateAssistant(turn.id, $event)"');
    expect(styles).toContain('min-height: 2.25rem');
    expect(styles).toContain('min-width: 2.25rem');
  });
});
