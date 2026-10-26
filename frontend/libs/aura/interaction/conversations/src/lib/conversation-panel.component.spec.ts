import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { focusBranchAction } from './branch-action-focus';
import { selectedDocumentIsPending } from './conversation-attachment-readiness';
import { attachmentSelectionForHydration, shouldClearComposerDraftAfterAcceptance } from './conversation-store';

const template = readFileSync(resolve(process.cwd(), 'libs/aura/interaction/conversations/src/lib/conversation-panel.component.html'), 'utf8');
const componentSource = readFileSync(resolve(process.cwd(), 'libs/aura/interaction/conversations/src/lib/conversation-panel.component.ts'), 'utf8');

describe('conversation configuration select bindings', () => {
  it('reconciles selected document readiness instead of permanently gating on stale upload rows', () => {
    expect(selectedDocumentIsPending('doc-processing', [{ id: 'doc-processing', status: 'processing' }])).toBe(true);
    expect(selectedDocumentIsPending('doc-failed', [{ id: 'doc-failed', status: 'failed' }])).toBe(true);
    expect(selectedDocumentIsPending('doc-ready', [{ id: 'doc-ready', status: 'ready' }])).toBe(false);
    expect(selectedDocumentIsPending('doc-missing', [])).toBe(true);
  });
  it('exposes the attachment picker, bounded limits, and vision guidance in the composer', () => {
    expect(template).toContain('attachment-picker');
    expect(template).toContain('Upload');
    expect(template).toContain('Documents');
    expect(template).toContain('Images');
    expect(template).toContain('attachmentStore.selected()');
    expect(template).toContain('composer-upload-status');
    expect(template).toContain('documentsStore.retryUpload(upload.key)');
    expect(template).toContain('modelVisionRequired');
    expect(componentSource).toContain('VISION_RECOMMENDATION');
    expect(template).toContain("['/documents', attachment.sourceId]");
    expect(template).toContain('cdkTrapFocus');
    expect(componentSource).toContain('attachmentsFor');
    expect(componentSource).toContain('deleteReusableImage');
    expect(componentSource).toContain("this.removeAttachment({ kind: 'image', attachmentId: imageId })");
    expect(componentSource).toContain("deleted === 'accepted' ?");
  });
  it('keeps branch focus on the action when connected and falls back after replacement', () => {
    const fallback = document.createElement('button');
    fallback.id = 'branch-toolbar-heading';
    document.body.append(fallback);
    const trigger = document.createElement('button');
    document.body.append(trigger);

    focusBranchAction(trigger);
    expect(document.activeElement).toBe(trigger);

    trigger.disabled = true;
    focusBranchAction(trigger);
    expect(document.activeElement).toBe(fallback);

    trigger.remove();
    focusBranchAction(trigger);
    expect(document.activeElement).toBe(fallback);
    fallback.remove();
  });

  it('binds Agent and Persona controls through ngModel so async options retain persisted revisions', () => {
    expect(template).toContain('[ngModel]="configurationRevisionValue(stagedAgent())"');
    expect(template).toContain('(ngModelChange)="chooseAgent($event)"');
    expect(template).toContain('[ngModel]="configurationRevisionValue(stagedPersona(), stagedUseAgentDefaultPersona())"');
    expect(template).toContain('(ngModelChange)="choosePersona($event)"');
    expect(template).not.toContain('[value]="stagedPersona()?.revisionId');
  });

  it('keeps the agent-default sentinel explicit for the Persona control', () => {
    expect(template).toContain('configurationRevisionValue(stagedPersona(), stagedUseAgentDefaultPersona())');
  });

  it('renders live model thinking in a separate inert accessible disclosure', () => {
    expect(template).toContain('<details class="model-thinking"');
    expect(template).toContain('<summary>Model thinking</summary>');
    expect(template).toContain('<pre>{{ thinking.text }}</pre>');
    expect(template).not.toContain('aura-safe-markdown [content]="thinking.text"');
    expect(template).not.toContain('[innerHTML]="thinking.text"');
  });

  it('falls back to the visible shell navigation trigger when the saved opener is hidden', () => {
    expect(componentSource).toContain('export function visibleFocusTarget');
    expect(componentSource).toContain("document.querySelector<HTMLButtonElement>('[aria-label=\"Open navigation\"]')");
    expect(componentSource).toContain('visibleFocusTarget(opener, navigationTrigger)?.focus()');
    expect(componentSource).toContain('if (returnFocus) setTimeout(() => {');
  });

  it('routes conversation approvals to governed review without inline decisions', () => {
    expect(template).toContain(`[routerLink]="['/approvals', approval.id]"`);
    expect(template).toContain('>Review and decide</a>');
    expect(template).not.toContain('(click)="approvalStore.approve');
    expect(template).not.toContain('(click)="approvalStore.reject');
  });

  it('renders validated document citations as internal accessible source links', () => {
    expect(template).toContain('document-citations');
    expect(template).toContain(`[routerLink]="['/documents', citation.documentId]"`);
    expect(template).toContain('[attr.aria-label]="citationAccessibleLabel(citation)"');
    expect(template).toContain('{{ citationLocator(citation) }}');
    expect(template).not.toContain('citation.url');
    expect(template).toContain('(click)="focusCitationDestination()"');
    expect(componentSource).toContain("document.getElementById('detail-heading') ?? document.getElementById('main-content')");
    expect(componentSource).toContain('target.focus()');
  });

  it('exposes an accessible bootstrap-ready seam before draft input is accepted', () => {
    expect(template).toContain('[attr.data-conversation-ready]="conversationReady() ? \'true\' : \'false\'"');
    expect(template).toContain('[attr.aria-busy]="conversationReady() ? \'false\' : \'true\'"');
    expect(componentSource).toContain('this.store.models().some((model) => model.selectable)');
    expect(componentSource).toContain('!this.agentStore.loading() && this.defaultAgentReady()');
  });

  it('keeps an immediately typed draft across bootstrap selection changes', () => {
    expect(template).toContain('[value]="composerDraft()"');
    expect(template).toContain('(input)="updateComposerDraft($any($event.target).value)"');
    expect(componentSource).toContain("readonly composerDraft = signal('')");
    expect(componentSource).toContain('this.composerDraftDirty = true');
    expect(componentSource).toContain('this.store.updateDraft(value)');
    expect(componentSource).toContain('this.store.updateDraftFor(conversationId, value)');
    expect(componentSource).toContain('untracked(() => this.handoffLiveComposerDraft(previousComposerConversationId))');
    expect(componentSource).toContain("previousComposerConversationId.startsWith('draft-')");
  });

  it('clears the local composer only after the store confirms acceptance', () => {
    expect(shouldClearComposerDraftAfterAcceptance('Send this once', '', 'Send this once', 'working')).toBe(true);
    expect(shouldClearComposerDraftAfterAcceptance('Keep this after rejection', 'Keep this after rejection', 'Keep this after rejection', 'error')).toBe(false);
    expect(shouldClearComposerDraftAfterAcceptance('Keep this after uncertainty', 'Keep this after uncertainty', 'Keep this after uncertainty', 'interrupted')).toBe(false);
    expect(shouldClearComposerDraftAfterAcceptance('A newer draft', '', 'Submitted draft', 'working')).toBe(false);
  });

  it('resets the local composer mirror when acceptance clears the submitted store draft', () => {
    expect(componentSource).toContain('shouldClearComposerDraftAfterAcceptance');
    expect(componentSource).toContain('this.composerSubmissionDraft = submittedDraft');
  });

  it('renders a branch retry only when the store exposes a retryable action', () => {
    expect(template).toContain('@if (store.selectedBranch().errorAction)');
    expect(template).not.toContain('{{ branchError }} <button type="button" class="inline-action" (click)="retryBranchAction()">');
    expect(componentSource).toContain('this.store.retryBranchAction(this.branchAttachments())');
    expect(componentSource).toContain("if (succeeded && action?.kind === 'edit') this.finishBranchEdit(conversationId, action.messageId);");
  });

  it('keeps attachment picker controls keyboard-addressable and sibling-native', () => {
    expect(template).toContain('role="tablist"');
    expect(template).toContain('role="tab"');
    expect(template).toContain('[attr.aria-selected]="attachmentPickerView()');
    expect(template).toContain('attachmentTabKeydown($event');
    expect(template).toContain('attachment-select-button');
    expect(template).toContain("[attr.aria-label]=\"'Delete ' + image.displayFilename\"");
    expect(template).not.toContain('role="button" tabindex="0" aria-label="Delete image"');
  });

  it('shares one submission blocker between the button and keyboard/form submission', () => {
    expect(componentSource).toContain('submissionBlocked(): boolean');
    expect(template).toContain('[disabled]="!store.canSend() || !defaultAgentReady() || submissionBlocked() || branchEditActive()"');
    expect(componentSource).toContain('if (!this.defaultAgentReady() || this.submissionBlocked()) return;');
    expect(componentSource).toContain("['validating', 'creating', 'uploading'].includes(item.state)");
    expect(componentSource).toContain('this.store.modelVisionRequiredFor()');
    expect(componentSource).toContain("upload?.state === 'ready' || upload?.state === 'duplicate'");
  });

  it('keeps owner and model admission state scoped and annotates verified models', () => {
    expect(componentSource).toContain('principal.issuer');
    expect(componentSource).toContain('const principalChanged = this.attachmentPrincipal !== null && principal !== this.attachmentPrincipal');
    expect(componentSource).toContain('const authorizationEnded = this.store.authState() !== \'authenticated\'');
    expect(componentSource).toContain('untracked(() => this.store.attachmentsFor(conversationId))');
    expect(componentSource).toContain('switchedConversation && !loading');
    expect(componentSource).toContain('this.handoffLiveComposerDraft(previousConversationId)');
    expect(componentSource).toContain("if (!this.attachmentPickerOpen() || this.attachmentPickerView() !== 'images') return;");
    expect(componentSource).toContain('modelVisionRequiredFor');
    expect(componentSource).toContain('attachmentAcceptanceConversationId');
    expect(componentSource).toContain('acceptedConversationId === this.store.selectedId()');
    expect(componentSource).toContain('modelOptionLabel');
    expect(componentSource).toContain('failed image-understanding verification');
    expect(template).toContain('{{ modelOptionLabel(model) }}');
    expect(componentSource).toContain('this.store.setDraftAttachments(selected)');
    expect(componentSource).toContain('if (accepted) { if (this.branchEditActive())');
    expect(template).toContain("attachmentStore.errorKind() === 'storage' && !attachmentStore.images().length");
  });

  it('hard-blocks the ordinary composer while preserving the branch picker path', () => {
    expect(componentSource).toContain('if (this.branchEditActive()) return;');
    expect(componentSource).toContain('|| this.branchEditActive()');
    expect(template).toContain('[disabled]="!!store.selected().archivedAt || branchEditActive()"');
    expect(template).toContain('[disabled]="controlsDisabled()"');
    expect(template).toContain("branchEditActive() ? 'Add branch attachments' : 'Add attachments'");
    expect(template).toContain('class="send-button" type="submit" [disabled]="!store.canSend() || !defaultAgentReady() || submissionBlocked() || branchEditActive()"');
    expect(template).toContain('class="selected-attachments branch-edit-attachments"');
    expect(componentSource).toContain('branchEditActive(): boolean { return this.store.selectedBranch().editingMessageId !== null; }');
  });

  it('keeps an explicit branch attachment set during refresh hydration', () => {
    expect(componentSource).toContain('if (switchedConversation && editingMessageId)');
    expect(componentSource).toContain('this.attachmentStore.setSelected(branchSelection);');
    expect(componentSource).toContain('this.branchAttachments.set(branchSelection);');
    expect(componentSource).toContain('this.branchAttachments.set(this.store.attachmentsForTurn(messageId));');
    expect(componentSource).toContain('this.store.editAndBranch(this.branchAttachments())');
  });

  it('uses full edit cleanup after a successful retry so branch attachments do not enter the composer draft', () => {
    expect(componentSource).toContain('private finishBranchEdit(conversationId: string, messageId: string | null): void');
    expect(componentSource).toContain('this.attachmentStore.setSelected(ordinarySelection);');
    expect(componentSource).toContain('this.branchAttachments.set([]);');
    expect(componentSource).toContain("this.store.setDraftAttachments(selected);");
    const ordinaryComposerSet = [{ kind: 'image', attachmentId: 'ordinary-x' }] as const;
    const branchSet = [{ kind: 'image', attachmentId: 'branch-y' }] as const;
    const scopedRestore = new Map<string, ReadonlyArray<unknown>>([['conversation-a\u0000message-a', ordinaryComposerSet]]);
    expect(scopedRestore.get('conversation-a\u0000message-a')).toEqual(ordinaryComposerSet);
    expect(scopedRestore.get('conversation-a\u0000message-a')).not.toEqual(branchSet);
  });

  it('keeps branch additions and removals scoped across conversation switches', () => {
    expect(componentSource).toContain('private readonly branchAttachmentSelections = new Map');
    expect(componentSource).toContain('this.rememberBranchAttachments(conversationId, messageId, selection);');
    expect(componentSource).toContain('this.rememberBranchAttachments(this.store.selectedId(), messageId, selected);');
    expect(componentSource).toContain('const rememberedBranchSelection = branchKey ? this.branchAttachmentSelections.get(branchKey) : undefined;');
    expect(componentSource).toContain('this.rememberBranchAttachments(this.store.selectedId(), editingMessageId, selected);');
    expect(componentSource).toContain("if (succeeded && action?.kind === 'edit') this.finishBranchEdit(conversationId, action.messageId);");
    expect(componentSource).toContain('private clearBranchAttachmentSelection(conversationId: string, messageId: string): void');
    expect(componentSource).toContain('this.branchAttachmentSelections.clear();');
    expect(componentSource).toContain('this.ordinaryAttachmentSelections.clear();');
    expect(componentSource).toContain('this.branchAttachments.set([]);');
    expect(componentSource).toContain('ngOnDestroy(): void { this.closePreview(false); this.clearImagePreviews(); this.attachmentStore.reset(); this.branchAttachmentSelections.clear(); this.ordinaryAttachmentSelections.clear(); this.branchAttachments.set([]); }');
    expect(componentSource).toContain('if (succeeded) this.finishBranchEdit(conversationId, messageId);');
    expect(componentSource).toContain('this.clearBranchAttachmentSelection(conversationId, messageId);');
    expect(componentSource).toContain('private readonly ordinaryAttachmentSelections = new Map');
    expect(componentSource).toContain('this.ordinaryAttachmentSelections.set(this.branchAttachmentKey(conversationId, messageId), [...this.attachmentStore.selected()]);');
    expect(componentSource).toContain('this.store.setDraftAttachmentsFor(conversationId, ordinarySelection);');

    const aSource = ['a1', 'a2', 'a3', 'a4', 'a5'];
    const aAfterRemoval = aSource.slice(0, 4);
    const bSelection = ['b1', 'b2'];
    const selections = new Map<string, ReadonlyArray<string>>();
    selections.set('conversation-a\u0000message-a', aAfterRemoval);
    selections.set('conversation-b\u0000message-b', bSelection);
    expect(selections.get('conversation-a\u0000message-a')).toEqual(aAfterRemoval);
    expect(selections.get('conversation-a\u0000message-a')).not.toContain('a5');
    expect(selections.get('conversation-b\u0000message-b')).toEqual(bSelection);
  });

  it('renders one attachment surface while editing and focuses the post-render branch textarea', () => {
    expect(template).toContain('@if (!branchEditActive() && attachmentStore.selected().length)');
    expect(template).toContain('class="selected-attachments branch-edit-attachments"');
    expect(componentSource).toContain('this.requestBranchEditInputFocus(messageId);');
    expect(componentSource).toContain("triggerId: `branch-edit-${messageId}`");
    expect(componentSource).toContain('this.requestBranchEditFocus();');
  });

  it('rebuilds retained branch selection from the destination turn across A-to-B-to-A', () => {
    const a = [{ kind: 'image', attachmentId: 'image-a' }] as const;
    const b = [{ kind: 'image', attachmentId: 'image-b' }] as const;

    expect(attachmentSelectionForHydration(true, 'message-a', b, a)).toEqual(a);
    expect(attachmentSelectionForHydration(true, 'message-b', a, b)).toEqual(b);
    expect(attachmentSelectionForHydration(false, 'message-a', b, a)).toEqual(b);
  });

  it('re-requests the URL branch after navigation and clears an orphaned edit safely', () => {
    expect(componentSource).toContain('if (conversationId !== this.lastRoutedConversationId)');
    expect(componentSource).toContain('const requestedSummary = branch.summaries.find((item) => item.id === requested);');
    expect(componentSource).toContain('if (requestedSummary && branch.error)');
    expect(componentSource).toContain('this.requestedBranchId = null;');
    expect(componentSource).toContain('Keep the store\'s explicit retry action visible');
    expect(componentSource).toContain('if (branch.activeId !== requested || !branchContentLoaded)');
    expect(componentSource).toContain('const sourceStillVisible = !editingMessageId || this.store.selected().turns.some((turn) => turn.id === editingMessageId && turn.role === \'user\');');
    expect(componentSource).toContain('if (editingMessageId && sourceStillVisible === false && !branch.loading && !this.requestedBranchId)');
    expect(componentSource).toContain('this.store.recoverOrphanedEdit();');
    expect(componentSource).toContain('this.restoreOrdinaryComposerSelection(conversationId, editingMessageId);');
    expect(componentSource).toContain('restoreOrphanedEditDraft(replace = false)');
    expect(template).toContain('Replace current draft');
    expect(template).toContain('Confirm replace current draft');
    expect(template).toContain('Confirm discard recovered edit');
    expect(template).toContain('Discard recovered edit');
    expect(componentSource).toContain("private readonly pendingOrphanedEditFocus = signal<'confirmation' | 'composer' | 'trigger' | null>(null)");
    expect(componentSource).toContain("document.querySelector<HTMLButtonElement>('[data-orphaned-confirm-cancel]')");
    expect(componentSource).toContain('this.pendingOrphanedEditFocus.set(\'composer\')');
    expect(componentSource).toContain('this.pendingOrphanedEditFocus.set(\'trigger\')');
    expect(template).toContain('aria-label="Recovered branch edit"');
    expect(template).toContain('data-orphaned-confirm-cancel');
    expect(componentSource).toContain('if (switchedConversation) { this.orphanedEditAction.set(null);');
    expect(componentSource).toContain('this.orphanedEditActionTrigger = null;');
    const recoveryStart = template.indexOf('@if (store.orphanedEditDraftFor())');
    const recoveryBanner = template.slice(recoveryStart, template.indexOf('<form class="composer-wrap"', recoveryStart));
    expect(recoveryBanner).toContain('role="status"');
    expect(recoveryBanner).not.toContain('role="alert"');
    expect(recoveryBanner).not.toContain('aria-invalid');
    expect(componentSource).toContain('this.store.selectedBranch().editingMessageId !== null');
    expect(componentSource).toContain('if (wasEditing && !editingMessageId && !loading)');
  });
});
