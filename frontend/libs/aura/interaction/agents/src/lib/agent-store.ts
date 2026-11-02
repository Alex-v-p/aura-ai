import { Injectable, computed, inject, signal } from '@angular/core';
import { AGENT_API, type AgentApi, type AgentDraft, type AgentProfile, type AgentStatus } from './agent-models';

const emptyDraft = (): AgentDraft => ({ displayName: '', purpose: '', behavioralInstructions: '', personaRevisionId: '' });
type SessionAuthorizationStatus = 401 | 403;

@Injectable({ providedIn: 'root' })
export class AgentStore {
  private readonly api: AgentApi = inject(AGENT_API);
  readonly agents = signal<ReadonlyArray<AgentProfile>>([]);
  readonly loading = signal(true);
  readonly notice = signal<string | null>(null);
  readonly loadFailed = signal(false);
  readonly detailLoading = signal(false);
  readonly detailError = signal<string | null>(null);
  readonly resetEpoch = signal(0);
  readonly statusSaving = signal<ReadonlySet<string>>(new Set());
  readonly activeAgents = computed(() => this.agents().filter((agent) => agent.status === 'active'));
  readonly builderDraft = signal<AgentDraft>(emptyDraft());
  private requestGeneration = 0;
  private listRequestGeneration = 0;
  private detailRequestGeneration = 0;
  private sessionResetHandler: ((status?: SessionAuthorizationStatus) => void) | null = null;

  constructor() { void this.load(); }

  async load(): Promise<void> {
    const sessionGeneration = this.requestGeneration;
    const generation = ++this.listRequestGeneration;
    this.loading.set(true);
    try { const profiles = await this.api.listAgents(); const agents = await Promise.all(profiles.map((profile) => this.api.getAgent(profile.id))); if (sessionGeneration !== this.requestGeneration || generation !== this.listRequestGeneration) return; this.agents.set(agents); this.notice.set(null); this.loadFailed.set(false); }
    catch (error: unknown) { if (sessionGeneration !== this.requestGeneration || generation !== this.listRequestGeneration) return; this.loadFailed.set(true); this.notice.set(this.message(error)); this.handleAuthFailure(error); }
    finally { if (sessionGeneration === this.requestGeneration && generation === this.listRequestGeneration) this.loading.set(false); }
  }
  async loadDetail(id: string): Promise<void> { const sessionGeneration = this.requestGeneration; const generation = ++this.detailRequestGeneration; this.detailLoading.set(true); this.detailError.set(null); try { const profile = await this.api.getAgent(id); if (sessionGeneration !== this.requestGeneration || generation !== this.detailRequestGeneration) return; this.replace(profile); } catch (error: unknown) { if (sessionGeneration !== this.requestGeneration || generation !== this.detailRequestGeneration) return; const message = this.message(error); this.detailError.set(message); this.notice.set(message); this.handleAuthFailure(error); } finally { if (sessionGeneration === this.requestGeneration && generation === this.detailRequestGeneration) this.detailLoading.set(false); } }
  async retryDetail(id: string): Promise<void> { await this.loadDetail(id); }

  async create(draft: AgentDraft): Promise<boolean> { const generation = this.requestGeneration; this.notice.set(null); try { const created = await this.api.createAgent(draft); if (generation !== this.requestGeneration) return false; this.agents.update((items) => [...items, created]); this.notice.set(null); return true; } catch (error: unknown) { if (generation !== this.requestGeneration) return false; this.notice.set(this.message(error)); this.handleAuthFailure(error); return false; } }
  setBuilderDraft(draft: AgentDraft): void { this.builderDraft.set({ ...draft }); }
  resetBuilderDraft(): void { this.builderDraft.set(emptyDraft()); }
  registerSessionResetHandler(handler: ((status?: SessionAuthorizationStatus) => void) | null): void { this.sessionResetHandler = handler; }
  resetSessionState(): void { ++this.requestGeneration; this.api.clearSession?.(); this.agents.set([]); this.loading.set(false); this.notice.set(null); this.loadFailed.set(false); this.detailLoading.set(false); this.detailError.set(null); this.builderDraft.set(emptyDraft()); this.statusSaving.set(new Set()); this.resetEpoch.update((value) => value + 1); }
  async revise(id: string, draft: AgentDraft): Promise<boolean> { const generation = this.requestGeneration; this.notice.set(null); const profile = this.find(id); if (!profile) { this.notice.set('We could not find this agent. Your changes are still here.'); return false; } try { const updated = await this.api.createRevision(id, draft, profile.version); if (generation !== this.requestGeneration) return false; this.replace(updated); this.notice.set(null); return true; } catch (error: unknown) { if (generation !== this.requestGeneration) return false; this.notice.set(this.message(error)); this.handleAuthFailure(error); return false; } }
  async setStatus(id: string, status: AgentStatus): Promise<boolean> {
    if (this.statusSaving().has(id)) return false;
    const profile = this.find(id);
    if (!profile) return false;
    const generation = this.requestGeneration;
    this.statusSaving.update((ids) => new Set(ids).add(id));
    try {
      const updated = await this.api.setStatus(id, status, profile.version);
      if (generation !== this.requestGeneration) return false;
      this.replace(updated);
      return true;
    } catch (error: unknown) {
      if (generation !== this.requestGeneration) return false;
      this.notice.set(this.message(error));
      this.handleAuthFailure(error);
      return false;
    } finally {
      this.statusSaving.update((ids) => { const next = new Set(ids); next.delete(id); return next; });
    }
  }
  isStatusSaving(id: string): boolean { return this.statusSaving().has(id); }
  find(id: string): AgentProfile | undefined { return this.agents().find((agent) => agent.id === id); }
  private replace(updated: AgentProfile): void { this.agents.update((items) => items.map((item) => item.id === updated.id ? updated : item)); }
  private handleAuthFailure(error: unknown): void {
    const status = (error as { status?: unknown })?.status;
    if (status !== 401 && status !== 403) return;
    this.resetSessionState();
    this.sessionResetHandler?.(status);
    const recovery = status === 401 ? 'Your session has expired. Sign in again to view your agents.' : 'You do not have permission to view these agents.';
    this.loadFailed.set(true);
    this.notice.set(recovery);
    this.detailError.set(recovery);
  }
  private message(error: unknown): string { return error instanceof Error ? error.message : 'We could not save this agent. Your changes are still here.'; }
}
