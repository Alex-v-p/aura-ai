import { OverlayModule, CdkOverlayOrigin, type ConnectedPosition } from '@angular/cdk/overlay';
import { A11yModule } from '@angular/cdk/a11y';
import { DatePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, ElementRef, HostListener, ViewChild, effect, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { LoadingStateComponent, StatusMessageComponent } from '@aura/shared/ui';
import { ConversationStore } from './conversation-store';
import { AgentStore, type AgentReference } from '@aura/aura/interaction/agents';
import { PersonaStore } from '@aura/aura/interaction/personas';
import type { PersonaReference } from '@aura/aura-api-client';
import { safeRunErrorDisplay, safeTraceReference } from './run-detail-safety';

@Component({
  selector: 'aura-conversation-panel',
  standalone: true,
  imports: [FormsModule, LoadingStateComponent, StatusMessageComponent, OverlayModule, A11yModule, DatePipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './conversation-panel.component.html',
  styleUrl: './conversation-panel.component.css',
})
export class ConversationPanelComponent {
  readonly store = inject(ConversationStore);
  readonly agentStore = inject(AgentStore);
  readonly personaStore = inject(PersonaStore);
  @ViewChild('composer') private readonly composer?: ElementRef<HTMLTextAreaElement>;
  @ViewChild('optionsButton') private readonly optionsButton?: ElementRef<HTMLButtonElement>;
  @ViewChild('optionsMenu') private readonly optionsMenu?: ElementRef<HTMLElement>;
  @ViewChild('menuAgent') private readonly menuAgent?: ElementRef<HTMLSelectElement>;
  @ViewChild('menuPersona') private readonly menuPersona?: ElementRef<HTMLSelectElement>;
  @ViewChild('menuModel') private readonly menuModel?: ElementRef<HTMLSelectElement>;
  @ViewChild('configurationCancel') private readonly configurationCancel?: ElementRef<HTMLButtonElement>;
  @ViewChild('runInspectorTrigger') private readonly runInspectorTrigger?: ElementRef<HTMLButtonElement>;
  @ViewChild('runInspectorClose') private readonly runInspectorClose?: ElementRef<HTMLButtonElement>;
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
  private hadPendingConfiguration = false;
  private pendingConfigurationOriginId: string | null = null;
  private opener: HTMLButtonElement | null = null;

  constructor() {
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
  }

  submit(event?: Event): void { event?.preventDefault(); if (this.store.send()) queueMicrotask(() => this.composer?.nativeElement.focus()); }
  handleKeydown(event: KeyboardEvent): void { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); this.submit(event); } }
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
  chooseModel(modelId: string): void { if (!this.controlsDisabled()) void this.store.selectModel(modelId); }
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
  controlsDisabled(): boolean { return this.store.runState() === 'working' || this.store.authState() !== 'authenticated' || Boolean(this.store.selected().archivedAt); }
  stagedConfigurationChanged(): boolean {
    const conversation = this.store.selected();
    return (this.stagedAgent()?.revisionId ?? null) !== (conversation.agent?.revisionId ?? null)
      || (this.stagedUseAgentDefaultPersona() ? conversation.personaOverride : (this.stagedPersona()?.revisionId ?? null) !== (conversation.persona?.revisionId ?? null) || !conversation.personaOverride);
  }

  toggleOptions(event?: Event, origin?: CdkOverlayOrigin, focus: 'agent' | 'persona' | 'model' = 'agent'): void {
    event?.stopPropagation();
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
    if (returnFocus) queueMicrotask(() => {
      const target = focusId ? document.getElementById(focusId) : null;
      if (target instanceof HTMLElement) target.focus();
      else this.runInspectorTrigger?.nativeElement.focus();
    });
  }
  runDuration(run: { readonly createdAt: string; readonly startedAt: string | null; readonly finishedAt: string | null }): string {
    if (!run.startedAt || !run.finishedAt) return '—';
    const milliseconds = Math.max(0, Date.parse(run.finishedAt) - Date.parse(run.startedAt));
    return milliseconds < 1000 ? `${milliseconds} ms` : `${(milliseconds / 1000).toFixed(1)} s`;
  }
  sortedRuns(): ReadonlyArray<import('@aura/aura-api-client').Run> { return this.store.selected().runs.slice().sort((left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt)); }
  safeRunError(run: { readonly error: { readonly code: string; readonly message: string } | null }): string | null {
    return safeRunErrorDisplay(run.error);
  }
  safeTraceReference(run: { readonly error: { readonly traceId: string } | null }): string | null { return safeTraceReference(run.error?.traceId); }

  cancelConfiguration(): void { this.store.cancelPendingConfiguration(); }
  async confirmConfiguration(): Promise<void> { await this.store.confirmConfigurationChange(); }

  @HostListener('document:keydown.escape') onDocumentEscape(): void { if (this.runInspectorOpen()) this.closeRunInspector(); else if (this.optionsOpen()) this.closeOptions(); else if (this.store.pendingConfiguration()) this.cancelConfiguration(); }

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

  modelMetadataLabel(): string { return `Model ${this.store.selectedModelId() || 'not selected'}`; }

  metadataMarker(kind: 'agent' | 'persona'): string | null {
    const disabled = kind === 'agent' ? this.store.selectedAgent().status === 'disabled' : this.store.selectedPersona()?.status === 'disabled';
    const newer = kind === 'agent' ? this.hasNewerAgentRevision() : this.hasNewerPersonaRevision();
    if (disabled) return 'Disabled';
    if (newer) return 'Newer';
    return null;
  }
}
