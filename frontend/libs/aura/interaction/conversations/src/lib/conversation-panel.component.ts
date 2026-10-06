import { ChangeDetectionStrategy, Component, ElementRef, ViewChild, effect, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { LoadingStateComponent, StatusMessageComponent } from '@aura/shared/ui';
import { ConversationStore } from './conversation-store';
import { AgentStore, type AgentReference } from '@aura/aura/interaction/agents';

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
  @ViewChild('composer') private readonly composer?: ElementRef<HTMLTextAreaElement>;

  constructor() {
    effect(() => {
      const refreshRequest = this.store.agentRefreshRequested();
      if (refreshRequest > 0) void this.agentStore.load();
    });
  }

  submit(event?: Event): void { event?.preventDefault(); if (this.store.send()) queueMicrotask(() => this.composer?.nativeElement.focus()); }
  handleKeydown(event: KeyboardEvent): void { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); this.submit(event); } }
  chooseAgent(revisionId: string): void { const revision = this.agentStore.activeAgents().flatMap((agent) => agent.revisions).find((item) => item.revisionId === revisionId); if (revision) this.store.requestAgent(revision as AgentReference); }
  upgradeAgent(): void { const current = this.store.selectedAgent(); const latest = this.agentStore.activeAgents().find((agent) => agent.id === current.profileId)?.revisions.at(-1); if (latest && latest.revision > current.revision) this.store.requestAgent(latest); }
}
