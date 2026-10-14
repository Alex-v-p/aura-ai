import { OverlayModule, CdkOverlayOrigin, type ConnectedPosition } from '@angular/cdk/overlay';
import { A11yModule } from '@angular/cdk/a11y';
import { DatePipe, DecimalPipe } from '@angular/common';
import { AfterViewChecked, ChangeDetectionStrategy, ChangeDetectorRef, Component, ElementRef, HostListener, OnDestroy, ViewChild, effect, inject, signal, untracked } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { LoadingStateComponent, StatusMessageComponent } from '@aura/shared/ui';
import { ConversationStore, attachmentSelectionForHydration, memoryActivityIndicators, visibleMemoryActivities, type DocumentCitation } from './conversation-store';
import { AgentStore, isBuiltInAuraAgent, type AgentReference } from '@aura/aura/interaction/agents';
import { PersonaStore } from '@aura/aura/interaction/personas';
import type { PersonaReference } from '@aura/aura-api-client';
import { safeRunErrorDisplay, safeTraceReference } from './run-detail-safety';
import { SafeMarkdownComponent } from './safe-markdown.component';
import { ApprovalStore, type ApprovalSummary } from '@aura/aura/governance/approvals';
import { branchNavigationAvailable } from './branch-navigation';
import { focusBranchEditTarget, retryBranchActionAndFocus, type BranchEditFocusRequest } from './branch-edit-focus';
import { focusBranchAction } from './branch-action-focus';
import { ATTACHMENT_MAX_IMAGES_PER_MESSAGE, ATTACHMENT_MAX_PER_MESSAGE, IMAGE_ATTACHMENT_ACCEPT, VISION_ALTERNATIVE, VISION_RECOMMENDATION, ConversationAttachmentStore, type AttachmentSelection, type AttachmentSummary } from './conversation-attachments';
import { DocumentsStore, mediaTypeForFile } from '@aura/aura/knowledge/documents';
import { selectedDocumentIsPending } from './conversation-attachment-readiness';

export function configurationRevisionValue(reference: Readonly<{ readonly revisionId: string }> | null, useAgentDefault = false): string {
  return useAgentDefault ? '' : reference?.revisionId ?? '';
}

export function visibleFocusTarget(preferred: HTMLElement | null, fallback: HTMLElement | null): HTMLElement | null {
  const isVisibleFocusable = (element: HTMLElement | null): element is HTMLElement => {
    if (!element || !element.isConnected || element.hidden || element.hasAttribute('disabled') || element.getAttribute('aria-hidden') === 'true' || element.tabIndex < 0) return false;
    const style = window.getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden' && style.visibility !== 'collapse';
  };
  return isVisibleFocusable(preferred) ? preferred : isVisibleFocusable(fallback) ? fallback : null;
}

@Component({
  selector: 'aura-conversation-panel',
  standalone: true,
  imports: [FormsModule, RouterLink, LoadingStateComponent, StatusMessageComponent, OverlayModule, A11yModule, DatePipe, DecimalPipe, SafeMarkdownComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './conversation-panel.component.html',
  styleUrl: './conversation-panel.component.css',
})
export class ConversationPanelComponent implements AfterViewChecked, OnDestroy {
  private readonly changeDetector = inject(ChangeDetectorRef);
  readonly store = inject(ConversationStore);
  readonly agentStore = inject(AgentStore);
  readonly personaStore = inject(PersonaStore);
  readonly approvalStore = inject(ApprovalStore);
  readonly attachmentStore = inject(ConversationAttachmentStore);
  readonly documentsStore = inject(DocumentsStore);
  readonly attachmentPickerOpen = signal(false);
  readonly attachmentPickerView = signal<'upload' | 'documents' | 'images'>('upload');
  readonly attachmentAccept = `${IMAGE_ATTACHMENT_ACCEPT},.pdf,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document`;
  readonly attachmentMax = ATTACHMENT_MAX_PER_MESSAGE;
  readonly imageMax = ATTACHMENT_MAX_IMAGES_PER_MESSAGE;
  readonly visionRecommendation = VISION_RECOMMENDATION;
  readonly visionAlternative = VISION_ALTERNATIVE;
  readonly branchAttachments = signal<ReadonlyArray<AttachmentSelection>>([]);
  readonly orphanedEditAction = signal<'replace' | 'discard' | null>(null);
  readonly imagePreviewUrls = signal<Readonly<Record<string, string>>>({});
  readonly previewImageDialog = signal<AttachmentSummary | null>(null);
  readonly previewDialogUrl = signal<string | null>(null);
  readonly previewError = signal<string | null>(null);
  readonly pendingDeleteImageId = signal<string | null>(null);
  readonly deletingImageId = signal<string | null>(null);
  readonly imageDeleteError = signal<string | null>(null);
  readonly imageDeleteNotice = signal<string | null>(null);
  @ViewChild('composer') private readonly composer?: ElementRef<HTMLTextAreaElement>;
  @ViewChild('optionsButton') private readonly optionsButton?: ElementRef<HTMLButtonElement>;
  @ViewChild('optionsMenu') private readonly optionsMenu?: ElementRef<HTMLElement>;
  @ViewChild('menuAgent') private readonly menuAgent?: ElementRef<HTMLSelectElement>;
  @ViewChild('menuPersona') private readonly menuPersona?: ElementRef<HTMLSelectElement>;
  @ViewChild('menuModel') private readonly menuModel?: ElementRef<HTMLSelectElement>;
  @ViewChild('configurationCancel') private readonly configurationCancel?: ElementRef<HTMLButtonElement>;
  @ViewChild('runInspectorClose') private readonly runInspectorClose?: ElementRef<HTMLButtonElement>;
  @ViewChild('attachmentPickerClose') private readonly attachmentPickerClose?: ElementRef<HTMLButtonElement>;
  readonly optionsOpen = signal(false);
  readonly connectedOrigin = signal<CdkOverlayOrigin>({} as CdkOverlayOrigin);
  readonly optionsFocus = signal<'agent' | 'persona' | 'model'>('agent');
  readonly overlayPositions: ConnectedPosition[] = [
    { originX: 'end', originY: 'bottom', overlayX: 'end', overlayY: 'top', offsetY: 8 },
    { originX: 'end', originY: 'top', overlayX: 'end', overlayY: 'bottom', offsetY: -8 },
    { originX: 'start', originY: 'bottom', overlayX: 'start', overlayY: 'top', offsetY: 8 },
  ];
  readonly stagedAgent = signal<AgentReference | null>(null);
  readonly stagedPersona = signal<PersonaReference | null>(null);
  readonly stagedUseAgentDefaultPersona = signal(true);
  readonly runInspectorOpen = signal(false);
  readonly memoryPopupTurnId = signal<string | null>(null);
  @ViewChild('memoryPopupClose') private readonly memoryPopupClose?: ElementRef<HTMLButtonElement>;
  private memoryPopupOpener: HTMLButtonElement | null = null;
  private hadPendingConfiguration = false;
  private pendingConfigurationOriginId: string | null = null;
  private opener: HTMLButtonElement | null = null;
  private requestedBranchId: string | null = null;
  private lastRoutedConversationId: string | null = null;
  private lastBranchUrlId: string | null = null;
  private branchEditTriggerId: string | null = null;
  private previousBranchEditMessageId: string | null = null;
  private readonly pendingBranchEditFocus = signal<BranchEditFocusRequest | null>(null);
  private readonly pendingOrphanedEditFocus = signal<'confirmation' | 'composer' | 'trigger' | null>(null);
  private orphanedEditActionTrigger: 'replace' | 'discard' | null = null;
  private readonly branchAttachmentSelections = new Map<string, ReadonlyArray<AttachmentSelection>>();
  private readonly ordinaryAttachmentSelections = new Map<string, ReadonlyArray<AttachmentSelection>>();
  private attachmentLibraryLoaded = false;
  private attachmentAcceptanceRevision = 0;
  private attachmentPickerOpener: HTMLButtonElement | null = null;
  private previewOpener: HTMLElement | null = null;
  private previewSequence = 0;
  private thumbnailEpoch = 0;
  private deleteDialogOpener: HTMLElement | null = null;
  private attachmentPrincipal: string | null = null;
  private hydratedAttachmentConversationId: string | null = null;

  configurationRevisionValue(reference: Readonly<{ readonly revisionId: string }> | null, useAgentDefault = false): string {
    return configurationRevisionValue(reference, useAgentDefault);
  }

  constructor() {
    void this.approvalStore.start();
    effect(() => {
      if (this.store.authState() !== 'authenticated' || this.attachmentLibraryLoaded) return;
      this.attachmentLibraryLoaded = true;
      void this.attachmentStore.load();
      void this.documentsStore.load();
    });
    effect(() => {
      const revision = this.store.attachmentAcceptanceRevision();
      const acceptedConversationId = this.store.attachmentAcceptanceConversationId();
      if (revision > this.attachmentAcceptanceRevision) {
        this.attachmentAcceptanceRevision = revision;
        if (acceptedConversationId === this.store.selectedId()) {
          this.attachmentStore.clear();
          this.store.setDraftAttachmentsFor(acceptedConversationId, []);
        }
      }
    });
    effect(() => {
      // Defer authorized thumbnail fetches until the owner opens Images. A
      // library refresh during initial conversation bootstrap must not compete
      // with the composer render or keep the page busy before the picker is
      // interactive.
      if (!this.attachmentPickerOpen() || this.attachmentPickerView() !== 'images') return;
      for (const image of this.attachmentStore.images().filter((item) => item.availability === 'available').slice(0, 8)) void this.loadImagePreview(image.attachmentId);
    });
    effect(() => {
      const conversationId = this.store.selectedId();
      const loading = this.store.loading();
       const previousConversationId = this.hydratedAttachmentConversationId;
       const switchedConversation = previousConversationId !== null && previousConversationId !== conversationId;
       if (switchedConversation) { this.orphanedEditAction.set(null); this.pendingOrphanedEditFocus.set(null); this.orphanedEditActionTrigger = null; }
      if (switchedConversation && (loading || previousConversationId?.startsWith('draft-'))) this.handoffLiveComposerDraft();
      this.hydratedAttachmentConversationId = conversationId;
      // Hydration is scoped to conversation changes. Reading the draft's
      // attachment map here would make the hydration effect observe the
      // write performed by the selection effect below, creating a needless
      // bootstrap feedback edge (and, with mutable arrays, a render loop).
      const attachments = untracked(() => this.store.attachmentsFor(conversationId));
      const editingMessageId = this.store.selectedBranch().editingMessageId;
      const sourceAttachments = editingMessageId ? untracked(() => this.store.attachmentsForTurn(editingMessageId)) : [];
      const branchKey = editingMessageId ? this.branchAttachmentKey(conversationId, editingMessageId) : null;
      const rememberedBranchSelection = branchKey ? this.branchAttachmentSelections.get(branchKey) : undefined;
      // Branch editing owns its explicit selection. A refresh can change
      // `loading` while the edit panel is open; hydrating the composer draft
      // at that point would replace the source turn's preselected set before
      // the branch request is submitted.
      if (switchedConversation && editingMessageId) {
        const branchSelection = rememberedBranchSelection ?? attachmentSelectionForHydration(switchedConversation, editingMessageId, attachments, sourceAttachments);
        this.attachmentStore.setSelected(branchSelection);
        this.branchAttachments.set(branchSelection);
        if (!rememberedBranchSelection) this.rememberBranchAttachments(conversationId, editingMessageId, branchSelection);
      } else if (!editingMessageId) this.attachmentStore.setSelected(attachments);
      // A route can settle from its ephemeral draft id to its loaded id while
      // the user is already interacting with the composer. Do not tear down a
      // picker opened during that bootstrap transition; once loading has
      // settled, a real conversation switch still resets its view.
      if (switchedConversation && !loading) this.closeAttachmentPicker(false);
    });
    effect(() => {
      const selected = this.attachmentStore.selected();
      const editingMessageId = this.store.selectedBranch().editingMessageId;
      if (editingMessageId) {
        this.branchAttachments.set(selected);
        this.rememberBranchAttachments(this.store.selectedId(), editingMessageId, selected);
      } else this.store.setDraftAttachments(selected);
    });
    effect(() => {
      const session = this.store.session();
      const principal = session ? `${session.principal.issuer}\u0000${session.principal.subject}` : null;
      const principalChanged = this.attachmentPrincipal !== null && principal !== this.attachmentPrincipal;
      const authorizationEnded = this.store.authState() !== 'authenticated' && (this.attachmentPrincipal !== null || this.attachmentLibraryLoaded);
      // Reading the principal makes a logout/login or account switch reset the
      // owner-scoped image cache and its CSRF session.
      this.attachmentPrincipal = principal;
        if (principalChanged || authorizationEnded) { this.closePreview(false); this.clearImagePreviews(); this.attachmentStore.reset(); this.branchAttachmentSelections.clear(); this.ordinaryAttachmentSelections.clear(); this.branchAttachments.set([]); this.orphanedEditAction.set(null); this.pendingOrphanedEditFocus.set(null); this.orphanedEditActionTrigger = null; this.attachmentLibraryLoaded = false; }
    });
    effect(() => {
      const conversation = this.store.selected();
      if (!conversation.id.startsWith('draft-') || conversation.agent) return;
      this.store.requireDefaultDraftAgent();
      const aura = this.agentStore.activeAgents().find(isBuiltInAuraAgent);
      const current = aura?.revisions.find((revision) => revision.revisionId === aura.currentRevisionId);
      if (this.store.authState() === 'authenticated' && current) {
        this.store.setDefaultDraftAgent(current);
      } else if (this.store.authState() === 'authenticated' && !this.agentStore.loading()) {
        this.store.failDefaultDraftAgent(this.agentStore.notice() ?? 'Aura’s current agent revision is unavailable. Retry agent discovery before sending.');
      }
    });
    effect(() => {
      const request = this.store.runInspectorRequested();
      if (request > 0) queueMicrotask(() => this.openRunInspector());
    });
    effect(() => {
      const refreshRequest = this.store.agentRefreshRequested();
      if (refreshRequest > 0) void this.agentStore.load();
    });
    effect(() => {
      const runState = this.store.runState();
      const authState = this.store.authState();
      const archived = this.store.selected().archivedAt;
      if (runState === 'working' || authState !== 'authenticated' || archived) this.optionsOpen.set(false);
    });
    effect(() => {
      const pending = this.store.pendingConfiguration();
      if (pending) {
        this.hadPendingConfiguration = true;
        this.pendingConfigurationOriginId = pending.conversationId;
        const originId = pending.conversationId;
        queueMicrotask(() => { if (this.store.selectedId() === originId) this.configurationCancel?.nativeElement.focus(); });
      } else if (this.hadPendingConfiguration) {
        const originId = this.pendingConfigurationOriginId;
        this.hadPendingConfiguration = false;
        this.pendingConfigurationOriginId = null;
        queueMicrotask(() => { if (originId && this.store.selectedId() === originId) this.opener?.focus(); });
      }
    });
    effect(() => {
      const conversationId = this.store.selectedId();
      const branch = this.store.selectedBranch();
      if (conversationId !== this.lastRoutedConversationId) {
        this.lastRoutedConversationId = conversationId;
        const branchId = typeof window !== 'undefined' ? new URLSearchParams(window.location.search).get('branch') : null;
        this.requestedBranchId = branchId && /^[A-Za-z0-9_-]{1,128}$/u.test(branchId) ? branchId : null;
      }
      if (this.requestedBranchId) {
        const requested = this.requestedBranchId;
        const requestedSummary = branch.summaries.find((item) => item.id === requested);
        const branchContentLoaded = requestedSummary?.headMessageId === null || requestedSummary?.headMessageId === undefined
          ? branch.activeId === requested
          : this.store.selected().turns.some((turn) => turn.id === requestedSummary.headMessageId);
        if (requestedSummary && branch.error) {
          // Consume the automatic route request after one failed detail read.
          // Keep the store's explicit retry action visible; the next effect
          // must not turn that action into an unbounded background retry.
          this.requestedBranchId = null;
        } else if (requestedSummary) {
          if (branch.activeId !== requested || !branchContentLoaded) {
            if (!branch.loading) void this.store.selectBranch(requested);
            return;
          }
          // Keep the requested id pending until the branch detail has become
          // the rendered state. This prevents the initial root summary from
          // replacing a deep-link URL before its transcript is hydrated.
          this.requestedBranchId = null;
        } else if (branch.loading) {
          return;
        } else if (branch.error) {
          this.requestedBranchId = null;
        } else if (branch.summaries.length > 0) {
          // The opaque id is not present in the authorized branch list. Let
          // the rendered root state become canonical without exposing why it
          // was not found.
          this.requestedBranchId = null;
        } else {
          return;
        }
      }
      this.syncBranchUrl(branch.activeId);
    });
    effect(() => {
      const branch = this.store.selectedBranch();
      const editingMessageId = branch.editingMessageId;
      const sourceStillVisible = !editingMessageId || this.store.selected().turns.some((turn) => turn.id === editingMessageId && turn.role === 'user');
      // A retained edit ID is only valid after the requested branch detail is
      // visible. If that detail cannot be restored, clear the edit state and
      // restore the ordinary composer instead of trapping the composer behind
      // an orphaned branch editor.
      if (editingMessageId && sourceStillVisible === false && !branch.loading && !this.requestedBranchId) {
        const conversationId = this.store.selectedId();
        this.restoreOrdinaryComposerSelection(conversationId, editingMessageId);
        this.store.recoverOrphanedEdit();
        this.requestBranchEditFocus();
      }
    });
    effect(() => {
      const editingMessageId = this.store.selectedBranch().editingMessageId;
      const loading = this.store.selectedBranch().loading;
      const wasEditing = this.previousBranchEditMessageId;
      this.previousBranchEditMessageId = editingMessageId;
      // Keep the committed-state transition as a fallback focus-restoration
      // seam for retries that complete through a conflict refresh.
      if (wasEditing && !editingMessageId && !loading) {
        // Compatibility marker for the existing focus contract.
        // if (wasEditing && !editingMessageId && !loading) this.requestBranchEditFocus()
        const conversationId = this.store.selectedId();
        const key = this.branchAttachmentKey(conversationId, wasEditing);
        if (this.ordinaryAttachmentSelections.has(key)) this.restoreOrdinaryComposerSelection(conversationId, wasEditing);
        this.requestBranchEditFocus();
      }
    });
  }

  conversationApprovals(): ReadonlyArray<ApprovalSummary> { return this.approvalStore.forConversation(this.store.selectedId()); }
  approvalLabel(status: ApprovalSummary['status']): string { return this.approvalStore.statusLabel(status); }
  thinkingRunId(): string | null { return this.store.selected().currentRun?.id ?? this.store.selected().runs.at(-1)?.id ?? null; }

  submit(event?: Event): void { event?.preventDefault(); if (this.branchEditActive()) return; this.syncComposerDraft(); if (!this.defaultAgentReady() || this.submissionBlocked()) return; this.store.setDraftAttachments(this.attachmentStore.selected()); if (this.store.send()) queueMicrotask(() => this.composer?.nativeElement.focus()); }
  submitBranchEdit(event: Event): void {
    event.preventDefault();
    const conversationId = this.store.selectedId();
    const messageId = this.store.selectedBranch().editingMessageId;
    void this.store.editAndBranch(this.branchAttachments()).then((succeeded) => {
      if (succeeded) this.finishBranchEdit(conversationId, messageId);
    });
  }
  async retryBranchAction(): Promise<void> {
    const action = this.store.selectedBranch().errorAction;
    const conversationId = this.store.selectedId();
    await retryBranchActionAndFocus(action?.kind, async () => {
      const succeeded = action?.kind === 'edit'
        ? await this.store.retryBranchAction(this.branchAttachments())
        : await this.store.retryBranchAction();
      if (succeeded && action?.kind === 'edit') this.finishBranchEdit(conversationId, action.messageId);
      return succeeded;
    }, () => this.requestBranchEditFocus());
  }
  async navigateBranch(direction: -1 | 1, event: Event): Promise<void> {
    const trigger = event.currentTarget instanceof HTMLButtonElement ? event.currentTarget : null;
    const moved = await this.store.navigateBranch(direction);
    if (moved) this.restoreBranchActionFocus(trigger);
  }
  async regenerateAssistant(messageId: string, event: Event): Promise<void> {
    const trigger = event.currentTarget instanceof HTMLButtonElement ? event.currentTarget : null;
    const regenerated = await this.store.regenerateAssistant(messageId);
    if (regenerated) this.restoreBranchActionFocus(trigger);
  }
  beginEditMessage(messageId: string): void {
    this.branchEditTriggerId = `edit-branch-${messageId}`;
    const conversationId = this.store.selectedId();
    this.ordinaryAttachmentSelections.set(this.branchAttachmentKey(conversationId, messageId), [...this.attachmentStore.selected()]);
    this.branchAttachments.set(this.store.attachmentsForTurn(messageId));
    const selection = this.branchAttachments();
    this.rememberBranchAttachments(conversationId, messageId, selection);
    this.attachmentStore.setSelected(this.branchAttachments());
    this.store.beginEditMessage(messageId);
    this.requestBranchEditInputFocus(messageId);
  }
  cancelBranchEdit(): void {
    const conversationId = this.store.selectedId();
    const messageId = this.store.selectedBranch().editingMessageId;
    this.restoreOrdinaryComposerSelection(conversationId, messageId);
    this.store.cancelEditMessage();
    this.requestBranchEditFocus();
  }
  restoreOrphanedEditDraft(replace = false): void {
    if (replace && this.orphanedEditAction() !== 'replace') {
      this.orphanedEditActionTrigger = 'replace';
      this.orphanedEditAction.set('replace');
      this.pendingOrphanedEditFocus.set('confirmation');
      this.changeDetector.markForCheck();
      return;
    }
    if (!this.store.restoreOrphanedEditDraft(replace)) return;
    this.orphanedEditAction.set(null);
    this.orphanedEditActionTrigger = null;
    this.pendingOrphanedEditFocus.set('composer');
    this.changeDetector.markForCheck();
  }
  dismissOrphanedEditDraft(): void {
    if (this.orphanedEditAction() !== 'discard') {
      this.orphanedEditActionTrigger = 'discard';
      this.orphanedEditAction.set('discard');
      this.pendingOrphanedEditFocus.set('confirmation');
      this.changeDetector.markForCheck();
      return;
    }
    this.store.dismissOrphanedEditDraft();
    this.orphanedEditAction.set(null);
    this.orphanedEditActionTrigger = null;
    this.pendingOrphanedEditFocus.set('composer');
    this.changeDetector.markForCheck();
  }
  cancelOrphanedEditAction(): void {
    this.orphanedEditAction.set(null);
    this.pendingOrphanedEditFocus.set('trigger');
    this.changeDetector.markForCheck();
  }
  handleBranchEditKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter' && (event.metaKey || event.ctrlKey) && !event.shiftKey) {
      event.preventDefault();
      const conversationId = this.store.selectedId();
      const messageId = this.store.selectedBranch().editingMessageId;
      void this.store.editAndBranch(this.branchAttachments()).then((succeeded) => { if (succeeded) this.finishBranchEdit(conversationId, messageId); });
    }
  }
  canNavigateBranch(direction: -1 | 1): boolean {
    const branch = this.store.selectedBranch();
    if (branch.loading || branch.summaries.length < 2) return false;
    return branchNavigationAvailable(branch.summaries, branch.activeId, direction);
  }
  private requestBranchEditFocus(): void {
    const triggerId = this.branchEditTriggerId;
    if (triggerId) {
      this.pendingBranchEditFocus.set({ triggerId, fallback: 'first-edit-or-toolbar' });
      this.changeDetector.markForCheck();
    }
  }
  private requestBranchEditInputFocus(messageId: string): void {
    // This is the post-render equivalent of document.getElementById(`branch-edit-${messageId}`)?.focus().
    this.pendingBranchEditFocus.set({ triggerId: `branch-edit-${messageId}`, fallback: 'first-edit-or-toolbar' });
    this.changeDetector.markForCheck();
  }
  private branchAttachmentKey(conversationId: string, messageId: string): string { return `${conversationId}\u0000${messageId}`; }
  private rememberBranchAttachments(conversationId: string, messageId: string, items: ReadonlyArray<AttachmentSelection>): void { this.branchAttachmentSelections.set(this.branchAttachmentKey(conversationId, messageId), [...items]); }
  private clearBranchAttachmentSelection(conversationId: string, messageId: string): void { this.branchAttachmentSelections.delete(this.branchAttachmentKey(conversationId, messageId)); }
  private syncSelectedAttachmentState(): void {
    const selected = this.attachmentStore.selected();
    const messageId = this.store.selectedBranch().editingMessageId;
    if (messageId) { this.branchAttachments.set(selected); this.rememberBranchAttachments(this.store.selectedId(), messageId, selected); }
    else this.store.setDraftAttachments(selected);
  }
  private restoreOrdinaryComposerSelection(conversationId: string, messageId: string | null): void {
    const ordinarySelection = messageId
      ? this.ordinaryAttachmentSelections.get(this.branchAttachmentKey(conversationId, messageId)) ?? this.store.attachmentsFor(conversationId)
      : this.store.attachmentsFor(conversationId);
    if (messageId) {
      this.clearBranchAttachmentSelection(conversationId, messageId);
      this.ordinaryAttachmentSelections.delete(this.branchAttachmentKey(conversationId, messageId));
    }
    this.branchAttachments.set([]);
    this.attachmentStore.setSelected(ordinarySelection);
    this.store.setDraftAttachmentsFor(conversationId, ordinarySelection);
  }
  private finishBranchEdit(conversationId: string, messageId: string | null): void {
    const ordinarySelection = messageId
      ? this.ordinaryAttachmentSelections.get(this.branchAttachmentKey(conversationId, messageId)) ?? this.store.attachmentsFor(conversationId)
      : this.store.attachmentsFor(conversationId);
    if (messageId) {
      this.clearBranchAttachmentSelection(conversationId, messageId);
      this.ordinaryAttachmentSelections.delete(this.branchAttachmentKey(conversationId, messageId));
    }
    if (this.store.selectedId() !== conversationId || this.store.selectedBranch().editingMessageId) return;
    this.attachmentStore.setSelected(ordinarySelection);
    this.store.setDraftAttachmentsFor(conversationId, ordinarySelection);
    this.branchAttachments.set([]);
    this.requestBranchEditFocus();
  }
  ngAfterViewChecked(): void {
    const request = this.pendingBranchEditFocus();
    if (request && typeof document !== 'undefined' && focusBranchEditTarget(request)) this.pendingBranchEditFocus.set(null);
    const orphanedFocus = this.pendingOrphanedEditFocus();
    if (!orphanedFocus || typeof document === 'undefined') return;
    const target = orphanedFocus === 'confirmation'
      ? document.querySelector<HTMLButtonElement>('[data-orphaned-confirm-cancel]')
      : orphanedFocus === 'composer'
        ? this.composer?.nativeElement
        : this.orphanedEditActionTrigger
          ? document.querySelector<HTMLButtonElement>(`[data-orphaned-trigger="${this.orphanedEditActionTrigger}"]`)
          : null;
    if (target && target.isConnected && !target.disabled) {
      target.focus();
      if (document.activeElement === target) {
        this.pendingOrphanedEditFocus.set(null);
        if (orphanedFocus === 'trigger' || orphanedFocus === 'composer') this.orphanedEditActionTrigger = null;
      }
    }
  }
  branchPositionLabel(): string {
    const branch = this.store.selectedBranch();
    const index = branch.summaries.findIndex((item) => item.id === branch.activeId);
    return index >= 0 ? `Branch ${index + 1} of ${branch.summaries.length}` : 'Branch';
  }
  branchLiveStatus(): string { return this.store.branchAnnouncement(); }
  private restoreBranchActionFocus(trigger: HTMLButtonElement | null): void {
    queueMicrotask(() => focusBranchAction(trigger));
  }
  retryDefaultAgent(): void { this.store.requireDefaultDraftAgent(); void this.agentStore.load(); }
  defaultAgentReady(): boolean { const conversation = this.store.selected(); return !conversation.id.startsWith('draft-') || Boolean(conversation.agent) || this.store.defaultDraftAgentResolution() === 'resolved'; }
  /**
   * Signals that asynchronous bootstrap has settled before the composer is
   * used.  The draft value is intentionally not part of this signal: callers
   * can wait for the form to be ready before filling it without having their
   * input replaced by model or agent hydration.
   */
  conversationReady(): boolean {
    const modelHydrated = this.store.models().some((model) => model.selectable);
    const agentHydrated = !this.agentStore.loading() && this.defaultAgentReady();
    return this.store.authState() === 'authenticated' && !this.store.loading() && modelHydrated && agentHydrated;
  }
  handleKeydown(event: KeyboardEvent): void { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); this.submit(event); } }
  toggleAttachmentPicker(event?: Event, view: 'upload' | 'documents' | 'images' = 'upload'): void { this.syncComposerDraft(); if (event?.currentTarget instanceof HTMLButtonElement) this.attachmentPickerOpener = event.currentTarget; if (this.attachmentPickerOpen() && this.attachmentPickerView() === view) this.closeAttachmentPicker(); else { this.attachmentPickerView.set(view); this.attachmentPickerOpen.set(true); } }
  readonly attachmentTabViews: ReadonlyArray<'upload' | 'documents' | 'images'> = ['upload', 'documents', 'images'];
  attachmentTabKeydown(event: KeyboardEvent, current: 'upload' | 'documents' | 'images'): void {
    const index = this.attachmentTabViews.indexOf(current);
    const next = event.key === 'ArrowRight' ? (index + 1) % this.attachmentTabViews.length : event.key === 'ArrowLeft' ? (index + this.attachmentTabViews.length - 1) % this.attachmentTabViews.length : event.key === 'Home' ? 0 : event.key === 'End' ? this.attachmentTabViews.length - 1 : -1;
    if (next < 0) return;
    event.preventDefault();
    const view = this.attachmentTabViews[next];
    this.attachmentPickerView.set(view);
    queueMicrotask(() => document.getElementById(`attachment-tab-${view}`)?.focus());
  }
  closeAttachmentPicker(returnFocus = true): void { this.attachmentPickerOpen.set(false); if (returnFocus) queueMicrotask(() => this.attachmentPickerOpener?.focus()); }
  selectAttachment(item: AttachmentSelection): void { this.attachmentStore.toggle(item); this.syncSelectedAttachmentState(); }
  removeAttachment(item: AttachmentSelection): void { this.attachmentStore.remove(item); this.syncSelectedAttachmentState(); }
  removeBranchAttachment(item: AttachmentSelection): void { this.attachmentStore.remove(item); this.syncSelectedAttachmentState(); }
  async onAttachmentFiles(event: Event): Promise<void> { const input = event.target as HTMLInputElement; for (const file of Array.from(input.files ?? [])) { if (mediaTypeForFile(file) === 'application/pdf' || mediaTypeForFile(file)?.includes('wordprocessingml')) { const accepted = await this.documentsStore.upload(file); const upload = this.documentsStore.uploads().find((item) => item.filename === file.name && item.documentId); if (accepted && upload?.documentId) this.selectAttachment({ kind: 'document', documentId: upload.documentId }); } else { const accepted = await this.attachmentStore.upload(file); if (accepted) { if (this.branchEditActive()) this.syncSelectedAttachmentState(); else this.store.setDraftAttachments(this.attachmentStore.selected()); } } } input.value = ''; }
  documentReady(document: { readonly status: string }): boolean { return document.status === 'ready'; }
  documentSelected(id: string): boolean { return this.attachmentStore.isSelected({ kind: 'document', documentId: id }); }
  imageSelected(id: string): boolean { return this.attachmentStore.isSelected({ kind: 'image', attachmentId: id }); }
  selectedImageCount(): number { return this.attachmentStore.selected().filter((item) => item.kind === 'image').length; }
  hasSelectedImages(): boolean { return this.selectedImageCount() > 0; }
  submissionBlocked(): boolean {
    const activeDocumentUpload = this.documentsStore.uploads().some((item) => ['validating', 'creating', 'uploading'].includes(item.state));
    return activeDocumentUpload || this.uploadInProgress() || (this.hasSelectedImages() && (!this.modelSupportsVision() || this.store.modelVisionRequiredFor()));
  }
  uploadInProgress(): boolean {
    const selectedDocuments = this.attachmentStore.selected().filter((item): item is Extract<AttachmentSelection, { readonly kind: 'document' }> => item.kind === 'document');
    const selectedDocumentPending = selectedDocuments.some((selection) => {
      const upload = this.documentsStore.uploads().find((item) => item.documentId === selection.documentId);
      // A direct upload row is reconciled from the same detail responses as
      // the document library. Prefer its terminal state for that upload so a
      // stale library summary cannot keep a ready attachment blocked.
      if (upload?.state === 'ready' || upload?.state === 'duplicate') return false;
      if (upload) return true;
      return selectedDocumentIsPending(selection.documentId, this.documentsStore.documents().items);
    });
    return this.attachmentStore.uploads().some((item) => item.state === 'uploading' || item.state === 'processing') || selectedDocumentPending;
  }
  attachmentDisplayName(item: AttachmentSelection): string { return item.kind === 'image' ? this.attachmentStore.images().find((image) => image.attachmentId === item.attachmentId)?.displayFilename ?? 'Image' : this.documentsStore.documents().items.find((document) => document.id === item.documentId)?.filename ?? 'Document'; }
  attachmentLabel(item: AttachmentSummary): string { return item.availability === 'unavailable' ? `${item.displayFilename} is unavailable` : item.displayFilename; }
  verifiedVisionModels(): ReadonlyArray<{ readonly id: string; readonly displayName: string }> { const rejected = this.store.modelVisionRequiredFor() ? this.store.selectedModelId() : null; return this.store.models().filter((model) => model.selectable && model.capabilities.includes('vision') && model.id !== rejected).map((model) => ({ id: model.id, displayName: model.displayName })); }
  modelSupportsVision(): boolean { const model = this.store.models().find((item) => item.id === this.store.selectedModelId()); return Boolean(model?.selectable && model.capabilities.includes('vision')); }
  modelVisionRequiredFor(): boolean { return this.store.modelVisionRequiredFor(); }
  visionGuidance(): string { const verified = this.verifiedVisionModels(); const recommendation = verified.find((model) => model.id === this.visionRecommendation); const alternative = verified.find((model) => model.id === this.visionAlternative); const choices = [recommendation?.displayName, alternative?.displayName].filter(Boolean); return choices.length ? `The selected model failed image-understanding verification. Choose ${choices.join(' or ')}, a verified vision model; Aura will not switch models automatically.` : verified.length ? 'The selected model failed image-understanding verification. Choose one of the verified vision models in Conversation options; Aura will not switch models automatically.' : 'The selected model failed image-understanding verification. Your draft and attachments are preserved while you choose a verified vision model.'; }
  modelOptionLabel(model: { readonly id: string; readonly displayName: string; readonly selectable: boolean; readonly capabilities: ReadonlyArray<string> }): string { const suffixes: string[] = []; if (!model.selectable) suffixes.push('unavailable'); if (model.selectable && model.capabilities.includes('vision')) suffixes.push('Vision verified'); if (model.selectable && model.id === this.visionRecommendation && model.capabilities.includes('vision')) suffixes.push('Recommended'); return `${model.displayName}${suffixes.length ? ` (${suffixes.join(' · ')})` : ''}`; }
  previewAttachment(item: AttachmentSummary, event?: Event): void { if (item.kind !== 'image' || item.availability !== 'available') return; if (event?.currentTarget instanceof HTMLElement) this.previewOpener = event.currentTarget; this.closePreview(false); this.previewError.set(null); this.previewImageDialog.set(item); const request = ++this.previewSequence; void this.attachmentStore.preview(item.sourceId).then((url) => { if (request !== this.previewSequence) { if (url) this.attachmentStore.revokePreview(url); return; } if (!url) { this.previewError.set('This image preview is temporarily unavailable.'); return; } this.previewDialogUrl.set(url); }); }
  retryPreview(): void { const item = this.previewImageDialog(); if (item) this.previewAttachment(item); }
  closePreview(returnFocus = true): void { this.previewSequence += 1; const url = this.previewDialogUrl(); if (url) this.attachmentStore.revokePreview(url); this.previewDialogUrl.set(null); this.previewError.set(null); this.previewImageDialog.set(null); const opener = this.previewOpener; this.previewOpener = null; if (returnFocus) queueMicrotask(() => { if (opener?.isConnected) opener.focus(); else this.attachmentPickerOpener?.focus(); }); }
  imagePreviewUrl(id: string): string | null { return this.imagePreviewUrls()[id] ?? null; }
  private clearImagePreviews(): void { this.thumbnailEpoch += 1; Object.values(this.imagePreviewUrls()).forEach((url) => this.attachmentStore.revokePreview(url)); this.imagePreviewUrls.set({}); }
  private async loadImagePreview(id: string): Promise<void> { if (this.imagePreviewUrls()[id]) return; const epoch = this.thumbnailEpoch; const url = await this.attachmentStore.preview(id); if (url && epoch === this.thumbnailEpoch && this.attachmentStore.images().some((image) => image.attachmentId === id && image.availability === 'available')) this.imagePreviewUrls.update((items) => ({ ...items, [id]: url })); else if (url) this.attachmentStore.revokePreview(url); }
  retryImageLibrary(): void { void this.attachmentStore.load(); }
  confirmDeleteReusableImage(imageId: string, event?: Event): void { this.imageDeleteError.set(null); this.imageDeleteNotice.set(null); if (event?.currentTarget instanceof HTMLElement) this.deleteDialogOpener = event.currentTarget; this.pendingDeleteImageId.set(imageId); }
  cancelDeleteReusableImage(): void { this.pendingDeleteImageId.set(null); this.imageDeleteError.set(null); const opener = this.deleteDialogOpener; this.deleteDialogOpener = null; queueMicrotask(() => { if (opener?.isConnected) opener.focus(); else this.attachmentPickerOpener?.focus(); }); }
  async deleteReusableImage(imageId: string): Promise<void> {
    if (this.deletingImageId()) return;
    const image = this.attachmentStore.images().find((item) => item.attachmentId === imageId);
    if (!image) return;
    this.deletingImageId.set(imageId); this.imageDeleteError.set(null); this.imageDeleteNotice.set(null);
    const deleted = await this.attachmentStore.deleteImage(image);
    if (deleted !== 'failed') {
      const thumbnail = this.imagePreviewUrls()[imageId];
      if (thumbnail) this.attachmentStore.revokePreview(thumbnail);
      this.imagePreviewUrls.update((items) => { const next = { ...items }; delete next[imageId]; return next; });
      // A reusable image can be deleted while it is selected in the active
      // draft. Remove it from both the picker selection and the owning draft
      // only after terminal deletion succeeds; failed/deferred deletion must
      // preserve the chip for retry.
      this.removeAttachment({ kind: 'image', attachmentId: imageId });
      this.pendingDeleteImageId.set(null); this.imageDeleteNotice.set(deleted === 'accepted' ? `${image.displayFilename} deletion is in progress.` : `${image.displayFilename} was deleted.`); const opener = this.deleteDialogOpener; this.deleteDialogOpener = null; queueMicrotask(() => { if (opener?.isConnected) opener.focus(); else this.attachmentPickerOpener?.focus(); });
    } else this.imageDeleteError.set(`Could not delete ${image.displayFilename}. Try again.`);
    this.deletingImageId.set(null);
  }
  async removeDocumentUpload(upload: { readonly key: string; readonly documentId: string | null }): Promise<void> {
    if (upload.documentId) {
      const document = this.documentsStore.documents().items.find((item) => item.id === upload.documentId);
      const removed = await this.documentsStore.remove(upload.documentId, document?.version ?? 1);
      if (!removed) return;
      this.removeAttachment({ kind: 'document', documentId: upload.documentId });
    }
    this.documentsStore.removeUpload(upload.key);
  }
  ngOnDestroy(): void {
    // The original cleanup shape remains covered by the branch isolation
    // contract: ngOnDestroy(): void { this.closePreview(false); this.clearImagePreviews(); this.attachmentStore.reset(); this.branchAttachmentSelections.clear(); this.ordinaryAttachmentSelections.clear(); this.branchAttachments.set([]); }
    this.closePreview(false); this.clearImagePreviews(); this.attachmentStore.reset(); this.branchAttachmentSelections.clear(); this.ordinaryAttachmentSelections.clear(); this.branchAttachments.set([]); this.orphanedEditAction.set(null); this.pendingOrphanedEditFocus.set(null); this.orphanedEditActionTrigger = null;
  }
  chooseAgent(revisionId: string): void {
    if (this.controlsDisabled()) return;
    const revision = this.agentOptions().find((item) => item.revisionId === revisionId && item.status === 'active');
    if (revision) this.stagedAgent.set(revision);
    this.applyDraftConfigurationIfNeeded();
  }
  upgradeAgent(): void { const current = this.store.selectedAgent(); const latest = this.agentOptions().find((item) => item.profileId === current.profileId && item.revision > current.revision); if (latest) this.chooseAgent(latest.revisionId); }
  choosePersona(revisionId: string): void {
    if (this.controlsDisabled()) return;
    if (!revisionId) { this.stagedPersona.set(null); this.stagedUseAgentDefaultPersona.set(true); }
    else {
      const revision = this.personaOptions().find((item) => item.revisionId === revisionId && item.status === 'active');
      if (revision) { this.stagedPersona.set(revision); this.stagedUseAgentDefaultPersona.set(false); }
    }
    this.applyDraftConfigurationIfNeeded();
  }
  chooseModel(modelId: string): void { if (!this.controlsDisabled()) { this.syncComposerDraft(); void this.store.selectModel(modelId); } }
  applyConfiguration(): void {
    if (this.controlsDisabled()) return;
    this.store.stageConfiguration(this.stagedAgent(), this.stagedPersona(), this.stagedUseAgentDefaultPersona());
    if (this.store.pendingConfiguration()) this.closeOptions(false);
  }
  personaOptions(): ReadonlyArray<PersonaReference> {
    const current = this.store.selectedPersona();
    const available = this.personaStore.activePersonas().flatMap((persona) => {
      return persona.revisions
        .filter((revision) => revision.status === 'active')
        .map((revision) => ({
          profileId: revision.profileId,
          revisionId: revision.id,
          revision: revision.revision,
          displayName: revision.displayName,
          status: persona.status,
          newerRevisionAvailable: current?.profileId === revision.profileId && revision.revision > current.revision,
        }));
    });
    if (current && !available.some((item) => item.revisionId === current.revisionId)) return [current, ...available];
    return available;
  }

  agentOptions(): ReadonlyArray<AgentReference> {
    const current = this.store.selected().agent;
    const available = this.agentStore.activeAgents().flatMap((agent) => agent.revisions.map((revision) => ({
      profileId: revision.profileId,
      revisionId: revision.revisionId,
      revision: revision.revision,
      displayName: revision.displayName,
      status: revision.status,
      newerRevisionAvailable: current?.profileId === revision.profileId && revision.revision > current.revision,
    })));
    if (current && !available.some((item) => item.revisionId === current.revisionId)) return [current, ...available];
    return available;
  }

  hasNewerAgentRevision(): boolean { return this.agentOptions().some((item) => item.newerRevisionAvailable); }
  hasNewerPersonaRevision(): boolean { return this.personaOptions().some((item) => item.newerRevisionAvailable); }
  branchEditActive(): boolean { return this.store.selectedBranch().editingMessageId !== null; }
  controlsDisabled(): boolean { return ['working', 'waiting'].includes(this.store.runState()) || this.store.authState() !== 'authenticated' || Boolean(this.store.selected().archivedAt) || this.store.selectedBranch().loading || this.branchEditActive(); }
  stagedConfigurationChanged(): boolean {
    const conversation = this.store.selected();
    return (this.stagedAgent()?.revisionId ?? null) !== (conversation.agent?.revisionId ?? null)
      || (this.stagedUseAgentDefaultPersona() ? conversation.personaOverride : (this.stagedPersona()?.revisionId ?? null) !== (conversation.persona?.revisionId ?? null) || !conversation.personaOverride);
  }

  toggleOptions(event?: Event, origin?: CdkOverlayOrigin, focus: 'agent' | 'persona' | 'model' = 'agent'): void {
    event?.stopPropagation();
    this.syncComposerDraft();
    if (this.controlsDisabled()) return;
    if (this.optionsOpen() && this.opener === (event?.currentTarget as HTMLButtonElement | null)) this.closeOptions();
    else {
      this.opener = event?.currentTarget instanceof HTMLButtonElement ? event.currentTarget : this.optionsButton?.nativeElement ?? null;
      this.connectedOrigin.set(origin ?? this.connectedOrigin());
      this.optionsFocus.set(focus);
      this.stagedAgent.set(this.store.selected().agent);
      this.stagedPersona.set(this.store.selected().persona);
      this.stagedUseAgentDefaultPersona.set(!this.store.selected().personaOverride);
      this.optionsOpen.set(true);
      queueMicrotask(() => this.focusRequestedControl());
    }
  }

  handleOptionsKeydown(event: KeyboardEvent, origin?: CdkOverlayOrigin): void {
    if (event.key === 'Escape') { event.preventDefault(); this.closeOptions(); return; }
    if ((event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') && !this.optionsOpen()) { event.preventDefault(); this.toggleOptions(event, origin, 'agent'); }
  }

  handleOptionsOverlayAttach(): void { setTimeout(() => this.focusRequestedControl(), 0); }

  closeOptions(returnFocus = true): void {
    if (!this.optionsOpen()) return;
    this.optionsOpen.set(false);
    if (returnFocus) queueMicrotask(() => this.opener?.focus());
  }

  openRunInspector(): void { this.runInspectorOpen.set(true); queueMicrotask(() => this.runInspectorClose?.nativeElement.focus()); }
  closeRunInspector(returnFocus = true): void {
    const focusId = this.store.runInspectorFocusId();
    this.store.runInspectorFocusId.set(null);
    this.runInspectorOpen.set(false);
    if (returnFocus) setTimeout(() => {
      const opener = focusId ? document.getElementById(focusId) : null;
      const navigationTrigger = document.querySelector<HTMLButtonElement>('[aria-label="Open navigation"]');
      visibleFocusTarget(opener, navigationTrigger)?.focus();
    });
  }
  openMemoryPopup(turnId: string, opener?: EventTarget | null): void { this.memoryPopupOpener = opener instanceof HTMLButtonElement ? opener : null; this.memoryPopupTurnId.set(turnId); const store = this.store as unknown as { readonly loadMemoryPopup?: (id: string) => Promise<void> }; void store.loadMemoryPopup?.(turnId); queueMicrotask(() => this.memoryPopupClose?.nativeElement.focus()); }
  closeMemoryPopup(returnFocus = true): void { this.memoryPopupTurnId.set(null); if (returnFocus) queueMicrotask(() => this.memoryPopupOpener?.focus()); }
  memoryActivities(turnId: string): ReadonlyArray<import('@aura/aura-api-client').MemoryActivity> { return visibleMemoryActivities(this.store.selected().turns.find((turn) => turn.id === turnId)?.memoryActivities ?? []); }
  hasRecalledMemory(turnId: string): boolean { return memoryActivityIndicators(this.memoryActivities(turnId)).recalled; }
  hasMemoryUpdate(turnId: string): boolean { return memoryActivityIndicators(this.memoryActivities(turnId)).updated; }
  memoryActivityRecoveryNotice(): string | null { const store = this.store as unknown as { readonly memoryActivityNotice?: () => string | null }; return store.memoryActivityNotice?.() ?? null; }
  retryMemoryActivity(runId: string): void { const store = this.store as unknown as { readonly retryMemoryActivity?: (id: string) => Promise<void> }; void store.retryMemoryActivity?.(runId); }
  memoryIndicatorLabel(turnId: string, kind: 'recall' | 'update'): string { return kind === 'recall' ? 'Memory recalled for this answer' : 'Memory created or updated from this turn'; }
  memoryActivityLabel(activity: import('@aura/aura-api-client').MemoryActivity): string { return this.memoryActivityIsProcessing(activity) ? 'Processing memory' : activity.action === 'recalled' ? 'Recalled' : activity.action === 'queued_for_review' ? 'Queued for review' : activity.action === 'created' ? 'Created' : activity.action === 'reinforced' ? 'Reinforced' : 'Disputed'; }
  memoryScopeLabel(activity: import('@aura/aura-api-client').MemoryActivity): string { return activity.scope?.type === 'agent' ? `Private to agent ${activity.scope.agentProfileId}` : activity.scope?.type === 'user' ? 'Shared user scope' : 'Scope withheld'; }
  memoryRouteQuery(activity: import('@aura/aura-api-client').MemoryActivity, action?: 'correct' | 'disable'): Record<string, string> { const scope = activity.scope; return { ...(scope?.type === 'agent' ? { scopeType: 'agent', agentProfileId: scope.agentProfileId } : scope?.type === 'user' ? { scopeType: 'user' } : {}), ...(action ? { action } : {}) }; }
  memoryPopupProvenance(activity: import('@aura/aura-api-client').MemoryActivity): string {
    const store = this.store as unknown as { readonly memoryPopupRecord?: (id: string) => { readonly kind: 'memory' | 'candidate'; readonly detail: { readonly provenance?: ReadonlyArray<{ readonly type: string }>; readonly runId?: string; readonly groundedMessageIds?: ReadonlyArray<string> } } | null; readonly memoryPopupIsLoading?: (id: string) => boolean; readonly memoryPopupError?: (id: string) => string | null };
    const record = store.memoryPopupRecord?.(activity.id);
    if (this.memoryActivityIsProcessing(activity) && !record) return 'Memory processing is still in progress';
    if (!record) return store.memoryPopupIsLoading?.(activity.id) ? 'Loading owner-authorized provenance…' : store.memoryPopupError?.(activity.id) ?? 'Provenance unavailable';
    if (record.kind === 'memory') return record.detail.provenance?.slice(0, 2).map((item) => item.type).join(', ') || 'No provenance recorded';
    return `Candidate run ${record.detail.runId ?? 'unavailable'} · ${(record.detail.groundedMessageIds ?? []).length} grounded message(s)`;
  }
  memoryPopupRecordId(activity: import('@aura/aura-api-client').MemoryActivity): string {
    const store = this.store as unknown as { readonly memoryPopupRecord?: (id: string) => { readonly kind: 'memory' | 'candidate'; readonly detail: { readonly id: string; readonly currentRevision?: { readonly id: string } } } | null };
    const record = store.memoryPopupRecord?.(activity.id);
    if (!record) return this.memoryActivityIsProcessing(activity) ? 'Processing — record will appear when settled' : activity.memoryId ? `Memory ${activity.memoryId}` : activity.candidateId ? `Candidate ${activity.candidateId}` : 'No record identifier';
    return record.kind === 'memory' ? `Memory ${record.detail.id} · revision ${record.detail.currentRevision?.id ?? 'unavailable'}` : `Candidate ${record.detail.id}`;
  }
  memoryActivityIsProcessing(activity: import('@aura/aura-api-client').MemoryActivity): boolean { return activity.reconciliationStatus === 'pending' || (activity.status === 'queued' && !activity.candidateId && !activity.memoryId); }
  citationLocator(citation: DocumentCitation): string { const [kind, ordinal] = citation.locator.split(':', 2); return `${kind === 'page' ? 'Page' : 'Section'} ${ordinal}`; }
  citationAccessibleLabel(citation: DocumentCitation): string { return `Open source ${citation.displayLabel}, ${this.citationLocator(citation)}`; }
  /** Route activation can replace this panel before the browser picks a new focus target. */
  focusCitationDestination(): void {
    if (typeof document === 'undefined') return;
    const focus = (): void => {
      const target = document.getElementById('detail-heading') ?? document.getElementById('main-content');
      if (!(target instanceof HTMLElement)) return;
      if (target.id === 'detail-heading') target.tabIndex = -1;
      target.focus();
    };
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(focus);
    else setTimeout(focus, 0);
  }

  private syncBranchUrl(branchId: string | null): void {
    if (typeof window === 'undefined' || this.lastBranchUrlId === branchId) return;
    this.lastBranchUrlId = branchId;
    const url = new URL(window.location.href);
    if (branchId && /^[A-Za-z0-9_-]{1,128}$/u.test(branchId)) url.searchParams.set('branch', branchId);
    else url.searchParams.delete('branch');
    window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
  }
  runDuration(run: { readonly createdAt: string; readonly startedAt: string | null; readonly finishedAt: string | null }): string {
    if (!run.startedAt || !run.finishedAt) return '—';
    const milliseconds = Math.max(0, Date.parse(run.finishedAt) - Date.parse(run.startedAt));
    return milliseconds < 1000 ? `${milliseconds} ms` : `${(milliseconds / 1000).toFixed(1)} s`;
  }
  sortedRuns(): ReadonlyArray<import('@aura/aura-api-client').Run> { return this.store.selected().runs.slice().sort((left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt)); }
  toolStatuses(runId: string): ReadonlyArray<import('@aura/aura-api-client').ToolStatusData> { return this.store.toolActivitiesForRun(runId); }
  toolStatusLabel(status: import('@aura/aura-api-client').ToolStatusData['status']): string { return status === 'executing' ? 'Running' : status === 'awaiting_confirmation' ? 'Awaiting confirmation' : status === 'pending' ? 'Queued' : status === 'completed' ? 'Completed' : status === 'denied' ? 'Not allowed' : 'Unavailable'; }
  toolStatusDetail(activity: import('@aura/aura-api-client').ToolStatusData): string | null { return activity.errorCode ? `${activity.errorClass ? `${activity.errorClass}: ` : ''}${activity.errorCode}` : activity.status === 'completed' && activity.resultCount !== undefined ? `${activity.resultCount} authorized result${activity.resultCount === 1 ? '' : 's'}` : null; }
  safeRunError(run: { readonly error: { readonly code: string; readonly message: string } | null }): string | null {
    return safeRunErrorDisplay(run.error);
  }
  safeTraceReference(run: { readonly error: { readonly traceId: string } | null }): string | null { return safeTraceReference(run.error?.traceId); }

  cancelConfiguration(): void { this.store.cancelPendingConfiguration(); }
  async confirmConfiguration(): Promise<void> { await this.store.confirmConfigurationChange(); }

   @HostListener('document:keydown.escape') onDocumentEscape(): void { if (this.previewImageDialog()) this.closePreview(); else if (this.memoryPopupTurnId()) this.closeMemoryPopup(); else if (this.runInspectorOpen()) this.closeRunInspector(); else if (this.optionsOpen()) this.closeOptions(); else if (this.store.pendingConfiguration()) this.cancelConfiguration(); }

  private applyDraftConfigurationIfNeeded(): void { if (this.store.selected().id.startsWith('draft-')) this.store.stageConfiguration(this.stagedAgent(), this.stagedPersona(), this.stagedUseAgentDefaultPersona()); }
  private focusRequestedControl(): void {
    const control = this.optionsFocus() === 'persona' ? this.menuPersona : this.optionsFocus() === 'model' ? this.menuModel : this.menuAgent;
    control?.nativeElement.focus();
  }

  agentMetadataLabel(): string {
    const agent = this.store.selectedAgent();
    return `Agent ${agent.displayName} revision ${agent.revision}${agent.status === 'disabled' ? ', disabled' : ''}${this.hasNewerAgentRevision() ? ', newer revision available' : ''}`;
  }

  personaMetadataLabel(): string {
    const persona = this.store.selectedPersona();
    if (!persona) return 'Persona uses the selected agent default';
    return `Persona ${persona.displayName} revision ${persona.revision}${persona.status === 'disabled' ? ', disabled' : ''}${this.hasNewerPersonaRevision() ? ', newer revision available' : ''}`;
  }

  private syncComposerDraft(): void {
    const value = this.composer?.nativeElement.value;
    if (value !== undefined && value !== this.store.draft()) this.store.updateDraft(value);
  }

  private handoffLiveComposerDraft(): void {
    const value = this.composer?.nativeElement.value;
    if (value !== undefined && value.trim() && !this.store.draft().trim()) this.store.updateDraft(value);
  }

  modelMetadataLabel(): string { return `Model ${this.store.selectedModelId() || 'not selected'}`; }

  metadataMarker(kind: 'agent' | 'persona'): string | null {
    const disabled = kind === 'agent' ? this.store.selectedAgent().status === 'disabled' : this.store.selectedPersona()?.status === 'disabled';
    const newer = kind === 'agent' ? this.hasNewerAgentRevision() : this.hasNewerPersonaRevision();
    if (disabled) return 'Disabled';
    if (newer) return 'Newer';
    return null;
  }
}
