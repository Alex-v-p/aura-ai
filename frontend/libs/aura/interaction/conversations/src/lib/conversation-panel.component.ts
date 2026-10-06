import { ChangeDetectionStrategy, Component, ElementRef, HostListener, ViewChild, effect, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { LoadingStateComponent, StatusMessageComponent } from '@aura/shared/ui';
import { ConversationStore } from './conversation-store';
import { AgentStore, type AgentReference } from '@aura/aura/interaction/agents';
import { PersonaStore } from '@aura/aura/interaction/personas';
import type { PersonaReference } from '@aura/aura-api-client';

@Component({
  selector: 'aura-conversation-panel',
  standalone: true,
  imports: [FormsModule, LoadingStateComponent, StatusMessageComponent],
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
  @ViewChild('firstMenuControl') private readonly firstMenuControl?: ElementRef<HTMLSelectElement>;
  @ViewChild('configurationCancel') private readonly configurationCancel?: ElementRef<HTMLButtonElement>;
  readonly optionsOpen = signal(false);
  readonly stagedAgent = signal<AgentReference | null>(null);
  readonly stagedPersona = signal<PersonaReference | null>(null);
  readonly stagedUseAgentDefaultPersona = signal(true);
  private hadPendingConfiguration = false;
  private pendingConfigurationOriginId: string | null = null;

  constructor() {
    effect(() => {
      const refreshRequest = this.store.agentRefreshRequested();
      if (refreshRequest > 0) void this.agentStore.load();
    });
    effect(() => {
      const runState = this.store.runState();
      const authState = this.store.authState();
      if (runState === 'working' || authState !== 'authenticated') this.optionsOpen.set(false);
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
        queueMicrotask(() => { if (originId && this.store.selectedId() === originId) this.optionsButton?.nativeElement.focus(); });
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
  controlsDisabled(): boolean { return this.store.runState() === 'working' || this.store.authState() !== 'authenticated'; }
  stagedConfigurationChanged(): boolean {
    const conversation = this.store.selected();
    return (this.stagedAgent()?.revisionId ?? null) !== (conversation.agent?.revisionId ?? null)
      || (this.stagedUseAgentDefaultPersona() ? conversation.personaOverride : (this.stagedPersona()?.revisionId ?? null) !== (conversation.persona?.revisionId ?? null) || !conversation.personaOverride);
  }

  toggleOptions(event?: Event): void {
    event?.stopPropagation();
    if (this.controlsDisabled()) return;
    if (this.optionsOpen()) this.closeOptions();
    else {
      this.stagedAgent.set(this.store.selected().agent);
      this.stagedPersona.set(this.store.selected().persona);
      this.stagedUseAgentDefaultPersona.set(!this.store.selected().personaOverride);
      this.optionsOpen.set(true);
      queueMicrotask(() => this.firstMenuControl?.nativeElement.focus());
    }
  }

  handleOptionsKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') { event.preventDefault(); this.closeOptions(); return; }
    if ((event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') && !this.optionsOpen()) { event.preventDefault(); this.toggleOptions(event); }
  }

  closeOptions(returnFocus = true): void {
    if (!this.optionsOpen()) return;
    this.optionsOpen.set(false);
    if (returnFocus) queueMicrotask(() => this.optionsButton?.nativeElement.focus());
  }

  cancelConfiguration(): void { this.store.cancelPendingConfiguration(); }
  async confirmConfiguration(): Promise<void> { await this.store.confirmConfigurationChange(); }

  @HostListener('document:keydown.escape') onDocumentEscape(): void { if (this.optionsOpen()) this.closeOptions(); else if (this.store.pendingConfiguration()) this.cancelConfiguration(); }
  @HostListener('document:click', ['$event']) onDocumentClick(event: MouseEvent): void {
    if (this.optionsOpen() && !this.triggerOrMenuContains(event.target)) this.closeOptions(false);
  }

  private applyDraftConfigurationIfNeeded(): void { if (this.store.selected().id.startsWith('draft-')) this.store.stageConfiguration(this.stagedAgent(), this.stagedPersona(), this.stagedUseAgentDefaultPersona()); }
  private triggerOrMenuContains(target: EventTarget | null): boolean { return target instanceof Node && (this.optionsButton?.nativeElement.contains(target) === true || this.optionsMenu?.nativeElement.contains(target) === true); }
}
