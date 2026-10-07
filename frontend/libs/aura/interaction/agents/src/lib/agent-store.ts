import { Injectable, computed, inject, signal } from '@angular/core';
import { AGENT_API, AgentApi, AgentDraft, AgentProfile, AgentStatus } from './agent-models';
import { AuraAgentApi } from './agent-api';

@Injectable({ providedIn: 'root' })
export class AgentStore {
  private readonly api: AgentApi = inject(AGENT_API, { optional: true }) ?? new AuraAgentApi();
  readonly agents = signal<ReadonlyArray<AgentProfile>>([]);
  readonly loading = signal(true);
  readonly notice = signal<string | null>(null);
  readonly activeAgents = computed(() => this.agents().filter((agent) => agent.status === 'active'));

  constructor() { void this.load(); }

  async load(): Promise<void> {
    this.loading.set(true);
    try { const profiles = await this.api.listAgents(); this.agents.set(await Promise.all(profiles.map((profile) => this.api.getAgent(profile.id)))); this.notice.set(null); }
    catch (error: unknown) { this.notice.set(error instanceof Error ? error.message : 'We could not load agents.'); }
    finally { this.loading.set(false); }
  }
  async loadDetail(id: string): Promise<void> { try { this.replace(await this.api.getAgent(id)); } catch (error: unknown) { this.notice.set(this.message(error)); } }

  async create(draft: AgentDraft): Promise<void> { try { const created = await this.api.createAgent(draft); this.agents.update((items) => [...items, created]); } catch (error: unknown) { this.notice.set(this.message(error)); } }
  async revise(id: string, draft: AgentDraft): Promise<void> { const profile = this.find(id); if (!profile) return; try { const updated = await this.api.createRevision(id, draft, profile.version); this.replace(updated); } catch (error: unknown) { this.notice.set(this.message(error)); } }
  async setStatus(id: string, status: AgentStatus): Promise<void> { const profile = this.find(id); if (!profile) return; try { this.replace(await this.api.setStatus(id, status, profile.version)); } catch (error: unknown) { this.notice.set(this.message(error)); } }
  find(id: string): AgentProfile | undefined { return this.agents().find((agent) => agent.id === id); }
  private replace(updated: AgentProfile): void { this.agents.update((items) => items.map((item) => item.id === updated.id ? updated : item)); }
  private message(error: unknown): string { return error instanceof Error ? error.message : 'We could not save this agent. Your changes are still here.'; }
}
