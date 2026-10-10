import { Injectable, computed, inject, signal } from '@angular/core';
import { AuraApprovalsApi } from './approvals-api';
import { ApprovalApiError, appendApprovalPage, emptyApprovalCollection, type ApprovalCollection, type ApprovalDetail, type ApprovalStatus, type ApprovalStream, type ApprovalSummary } from './approval-models';

const retryDelaysMs = [500, 1_000, 2_000, 4_000, 8_000, 15_000] as const;

@Injectable({ providedIn: 'root' })
export class ApprovalStore {
  private readonly api = inject(AuraApprovalsApi);
  readonly collection = signal<ApprovalCollection>(emptyApprovalCollection());
  readonly selected = signal<ApprovalDetail | null>(null);
  readonly loading = signal(false);
  readonly saving = signal(false);
  readonly notice = signal<string | null>(null);
  readonly errorStatus = signal<number | null>(null);
  readonly connectionState = signal<'offline' | 'connecting' | 'live'>('offline');
  readonly filter = signal<'all' | ApprovalStatus>('all');
  readonly pendingCount = computed(() => this.collection().pendingCount);
  private stream: ApprovalStream | null = null; private reconnectTimer: ReturnType<typeof setTimeout> | null = null; private started = false; private stopped = false; private retryAttempt = 0; private lastEventId: string | undefined; private startPromise: Promise<void> | null = null;

  async start(): Promise<void> { if (typeof window === 'undefined') return; if (this.started) { await this.startPromise; return; } this.started = true; this.stopped = false; this.startPromise = this.refresh().then(() => { this.connect(); }).finally(() => { this.startPromise = null; }); await this.startPromise; }
  stop(): void { this.stopped = true; this.started = false; this.stream?.close(); this.stream = null; if (this.reconnectTimer) clearTimeout(this.reconnectTimer); this.reconnectTimer = null; this.connectionState.set('offline'); }
  async refresh(): Promise<void> { this.loading.set(true); this.notice.set(null); this.errorStatus.set(null); try { const page = await this.api.list(undefined, this.selectedStatus()); this.collection.set({ ...page, pendingCount: page.items.filter((item) => item.status === 'pending').length }); const selectedId = this.selected()?.id; if (selectedId) this.selected.set(page.items.some((item) => item.id === selectedId) ? this.selected() : null); } catch (error: unknown) { this.setError(error, 'Approvals are temporarily unavailable.'); } finally { this.loading.set(false); } }
  async loadMore(): Promise<void> { const current = this.collection(); if (!current.nextCursor || this.loading()) return; this.loading.set(true); try { this.collection.set(appendApprovalPage(current, await this.api.list(current.nextCursor, this.selectedStatus()))); } catch (error: unknown) { this.setError(error, 'More approvals could not be loaded.'); } finally { this.loading.set(false); } }
  async inspect(id: string): Promise<ApprovalDetail | null> { this.notice.set(null); try { const detail = await this.api.get(id); this.selected.set(detail); return detail; } catch (error: unknown) { this.setError(error, 'That approval could not be loaded.'); return null; } }
  async approve(approval: ApprovalSummary): Promise<boolean> { return this.decide(approval, 'approve'); }
  async reject(approval: ApprovalSummary): Promise<boolean> { return this.decide(approval, 'reject'); }
  async setFilter(filter: 'all' | ApprovalStatus): Promise<void> { if (this.filter() === filter) return; this.filter.set(filter); await this.refresh(); }
  clearNotice(): void { this.notice.set(null); this.errorStatus.set(null); }
  forConversation(conversationId: string): ReadonlyArray<ApprovalSummary> { return this.collection().items.filter((item) => item.conversationId === conversationId); }
  statusLabel(status: ApprovalStatus): string { return status[0].toUpperCase() + status.slice(1); }

  private async decide(approval: ApprovalSummary, action: 'approve' | 'reject'): Promise<boolean> { if (approval.status !== 'pending') return false; this.saving.set(true); this.notice.set(null); try { const updated = action === 'approve' ? await this.api.approve(approval.id, approval.version) : await this.api.reject(approval.id, approval.version); this.collection.update((current) => ({ ...current, items: current.items.map((item) => item.id === updated.id ? updated : item), pendingCount: Math.max(0, current.pendingCount - 1) })); if (this.selected()?.id === updated.id) this.selected.update((detail) => detail ? { ...detail, ...updated } : detail); return true; } catch (error: unknown) { this.setError(error, `This approval could not be ${action}d.`); return false; } finally { this.saving.set(false); } }
  private connect(): void { if (this.stopped) return; this.connectionState.set('connecting'); this.stream?.close(); this.stream = this.api.stream(this.lastEventId, (event) => { if (event.eventType === 'heartbeat') return; this.lastEventId = event.eventId; this.retryAttempt = 0; this.connectionState.set('live'); void this.refresh(); if (this.selected()?.id === event.approvalId) void this.inspect(event.approvalId); }, (error) => { this.stream = null; if (this.stopped) return; this.connectionState.set('offline'); if (!error.retryable) { this.setError(error, 'Live approvals are unavailable.'); return; } if (error.status === 410) this.lastEventId = undefined; const delay = retryDelaysMs[Math.min(this.retryAttempt, retryDelaysMs.length - 1)] ?? retryDelaysMs[retryDelaysMs.length - 1]; this.retryAttempt += 1; this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; this.connect(); }, delay); }); }
  private selectedStatus(): ApprovalStatus | undefined { const filter = this.filter(); return filter === 'all' ? undefined : filter; }
  private setError(error: unknown, fallback: string): void { this.errorStatus.set(error instanceof ApprovalApiError ? error.status : null); this.notice.set(error instanceof Error && error.message ? error.message : fallback); }
}
