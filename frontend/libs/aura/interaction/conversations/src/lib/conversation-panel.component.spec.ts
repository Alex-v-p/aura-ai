import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { focusBranchAction } from './branch-action-focus';
import { selectedDocumentIsPending } from './conversation-attachment-readiness';

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

  it('renders a branch retry only when the store exposes a retryable action', () => {
    expect(template).toContain('@if (store.selectedBranch().errorAction)');
    expect(template).not.toContain('{{ branchError }} <button type="button" class="inline-action" (click)="retryBranchAction()">');
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
    expect(template).toContain('[disabled]="!store.canSend() || !defaultAgentReady() || submissionBlocked()"');
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
    expect(componentSource).toContain('this.handoffLiveComposerDraft()');
    expect(componentSource).toContain("if (!this.attachmentPickerOpen() || this.attachmentPickerView() !== 'images') return;");
    expect(componentSource).toContain('modelVisionRequiredFor');
    expect(componentSource).toContain('attachmentAcceptanceConversationId');
    expect(componentSource).toContain('acceptedConversationId === this.store.selectedId()');
    expect(componentSource).toContain('modelOptionLabel');
    expect(componentSource).toContain('failed image-understanding verification');
    expect(template).toContain('{{ modelOptionLabel(model) }}');
    expect(componentSource).toContain('this.store.setDraftAttachments(selected)');
    expect(componentSource).toContain('if (accepted) { if (this.store.branchEditing())');
    expect(template).toContain("attachmentStore.errorKind() === 'storage' && !attachmentStore.images().length");
  });
});
