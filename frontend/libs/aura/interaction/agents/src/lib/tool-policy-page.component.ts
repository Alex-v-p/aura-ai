import { ChangeDetectionStrategy, Component, computed, effect, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ActivatedRoute, RouterLink } from '@angular/router';
import type { AgentToolPolicy, ToolGrant } from '@aura/aura-api-client';
import { MEMORY_SEARCH_TOOL_ID, MEMORY_SEARCH_TOOL_VERSION } from './tool-policy-models';
import { ToolPolicyStore } from './tool-policy-store';

@Component({
  selector: 'aura-agent-tool-policy-page',
  standalone: true,
  imports: [DatePipe, FormsModule, RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="feature-page detail-page tool-policy-page">
      <a class="back-link" [routerLink]="['/agents', agentId]">← Agent configuration</a>
      <header class="feature-heading"><div><p class="eyebrow">Agent configuration</p><h1>Tool policy</h1><p class="lede">Control which read-only capabilities this agent may invoke during a run.</p></div></header>
      @if (store.notice(); as notice) { <p class="notice" role="alert">{{ notice }}</p> }
      @if (store.loading()) { <p class="empty-state" role="status">Loading tool policy…</p> } @else {
        <section class="detail-card editor">
          <h2>Create policy revision</h2>
          <p class="form-help">Policies are immutable. Saving creates a revision and attaches it to this agent with the current version check.</p>
          @for (tool of store.catalog(); track tool.id + ':' + tool.version) {
            <label class="check-label tool-option"><input type="checkbox" [name]="tool.id" [checked]="selectedToolIds().has(tool.id + ':' + tool.version)" (change)="toggle(tool.id, tool.version, $any($event.target).checked)" /><span><strong>{{ tool.displayName }} <small>v{{ tool.version }}</small></strong><small>{{ tool.description }}</small><em>{{ tool.classification }} · {{ tool.permissionScope }}</em></span></label>
          } @empty { <p class="empty-state">No safe tools are available.</p> }
          <p class="form-help">The initial tool is read-only memory search. Queries, arguments, and memory content never appear in run activity.</p>
          <button class="primary-button" type="button" [disabled]="store.saving() || !store.catalog().length" (click)="save()">{{ store.saving() ? 'Saving…' : 'Save and attach revision' }}</button>
        </section>
        <section class="detail-card">
          <h2>Policy history</h2>
          <p class="muted">Attached revision: {{ store.attachedPolicyRevisionId() || 'none' }}</p>
          @for (policy of store.policies(); track policy.id) {
            <article class="policy-row"><div><strong>Revision {{ policy.revision }}</strong><span>{{ policy.createdAt | date:'medium' }}</span></div><p>{{ summary(policy) }}</p><p class="muted">{{ policy.maxCallsPerRun }} call per run · {{ policy.timeoutMs / 1000 }}s timeout · {{ policy.retryLimit }} retries</p><span class="mode-badge">{{ policy.id === store.attachedPolicyRevisionId() ? 'Attached' : 'Historical' }}</span></article>
          } @empty { <p class="empty-state">No policy revisions yet.</p> }
        </section>
      }
    </section>
  `,
  styleUrl: './agent-pages.component.css',
})
export class AgentToolPolicyPageComponent {
  private readonly route = inject(ActivatedRoute);
  readonly store = inject(ToolPolicyStore);
  readonly agentId = this.route.snapshot.paramMap.get('id') ?? '';
  readonly selectedToolIds = signal<ReadonlySet<string>>(new Set());
  private readonly attachedPolicy = computed(() => this.store.policies().find((policy) => policy.id === this.store.attachedPolicyRevisionId()) ?? null);
  private readonly initialResetEpoch = this.store.resetEpoch();

  constructor() {
    effect(() => {
      const attached = this.attachedPolicy();
      if (attached) this.selectedToolIds.set(new Set(attached.grants.map((grant) => `${grant.toolId}:${grant.toolVersion}`)));
    });
    effect(() => { if (this.store.resetEpoch() !== this.initialResetEpoch) this.selectedToolIds.set(new Set()); });
    if (this.agentId) void this.store.load(this.agentId);
  }

  toggle(toolId: string, toolVersion: number, enabled: boolean): void {
    const next = new Set(this.selectedToolIds());
    const key = `${toolId}:${toolVersion}`;
    if (enabled) next.add(key); else next.delete(key);
    this.selectedToolIds.set(next);
  }

  async save(): Promise<void> {
    const grants: ReadonlyArray<ToolGrant> = [...this.selectedToolIds()].map((key) => {
      const [toolId, version] = key.split(':');
      return { toolId, toolVersion: Number(version) };
    });
    await this.store.createAndAttach(this.agentId, grants);
  }

  summary(policy: AgentToolPolicy): string {
    return policy.grants.some((grant) => grant.toolId === MEMORY_SEARCH_TOOL_ID && grant.toolVersion === MEMORY_SEARCH_TOOL_VERSION) ? 'Memory search enabled' : 'No tools granted';
  }
}
