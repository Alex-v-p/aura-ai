import { Injectable, computed, inject, signal } from '@angular/core';
import { ReminderStore } from '@aura/aura/automation/reminders';
import { AuraNotificationsApi } from './notifications-api';
import { appendNotificationPage, type NotificationCollection, type NotificationRecord, type NotificationsApi, type NotificationStream, type NotificationStatus } from './notification-models';

const emptyCollection = (): NotificationCollection => ({ items: [], nextCursor: null, unacknowledgedCount: 0 });
const retryDelaysMs = [500, 1_000, 2_000, 4_000, 8_000, 15_000] as const;

@Injectable({ providedIn: 'root' })
export class NotificationStore {
  private readonly api: NotificationsApi = inject(AuraNotificationsApi);
  private readonly reminders: ReminderStore = inject(ReminderStore);
  readonly collection = signal<NotificationCollection>(emptyCollection());
  readonly selected = signal<NotificationRecord | null>(null);
  readonly loading = signal(false);
  readonly saving = signal(false);
  readonly notice = signal<string | null>(null);
  readonly errorStatus = signal<number | null>(null);
  readonly connectionState = signal<'offline' | 'connecting' | 'live'>('offline');
  readonly filter = signal<'all' | NotificationStatus>('all');
  readonly unreadCount = computed(() => this.collection().unacknowledgedCount);
  private stream: NotificationStream | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private started = false;
  private stopped = false;
  private retryAttempt = 0;
  private lastEventId: string | undefined;

  async start(): Promise<void> {
    if (this.started || typeof window === 'undefined') return;
    this.started = true; this.stopped = false;
    await this.refresh();
    this.connect();
  }

  stop(): void {
    this.stopped = true; this.started = false; this.stream?.close(); this.stream = null;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null; this.connectionState.set('offline');
  }

  async refresh(): Promise<void> {
    this.loading.set(true); this.notice.set(null); this.errorStatus.set(null);
    try {
      const page = await this.api.list(undefined, this.selectedStatus());
      this.collection.set(page);
      const selectedId = this.selected()?.id;
      if (selectedId) this.selected.set(page.items.find((item) => item.id === selectedId) ?? null);
    } catch (error: unknown) { this.setError(error, 'Notifications are temporarily unavailable.'); }
    finally { this.loading.set(false); }
  }

  async loadMore(): Promise<void> {
    const current = this.collection();
    if (!current.nextCursor || this.loading()) return;
    this.loading.set(true);
    try {
      const page = await this.api.list(current.nextCursor, this.selectedStatus());
      this.collection.set(appendNotificationPage(current, page));
    } catch (error: unknown) { this.setError(error, 'More notifications could not be loaded.'); }
    finally { this.loading.set(false); }
  }

  async inspect(id: string): Promise<NotificationRecord | null> {
    this.notice.set(null);
    try { const notification = await this.api.get(id); this.selected.set(notification); return notification; }
    catch (error: unknown) { this.setError(error, 'That notification could not be loaded.'); return null; }
  }

  async acknowledge(notification: NotificationRecord): Promise<boolean> {
    if (notification.status === 'acknowledged') return true;
    this.saving.set(true); this.notice.set(null);
    try {
      const updated = await this.api.acknowledge(notification.id, notification.version);
      this.collection.update((current) => ({ ...current, items: current.items.map((item) => item.id === updated.id ? updated : item), unacknowledgedCount: Math.max(0, current.unacknowledgedCount - (notification.status === 'delivered' ? 1 : 0)) }));
      this.selected.set(updated);
      return true;
    } catch (error: unknown) { this.setError(error, 'This notification could not be acknowledged.'); return false; }
    finally { this.saving.set(false); }
  }

  clearNotice(): void { this.notice.set(null); this.errorStatus.set(null); }

  async setFilter(filter: 'all' | NotificationStatus): Promise<void> {
    if (this.filter() === filter) return;
    this.filter.set(filter);
    await this.refresh();
  }

  private connect(): void {
    if (this.stopped) return;
    this.connectionState.set('connecting');
    this.stream?.close();
    this.stream = this.api.stream(this.lastEventId, (event) => {
      this.lastEventId = event.eventId; this.retryAttempt = 0; this.connectionState.set('live'); void this.refresh(); void this.reminders.load();
    }, (error) => {
      this.stream = null;
      if (this.stopped) return;
      this.connectionState.set('offline');
      if (!error.retryable) { this.setError(error, 'Live notifications are unavailable.'); return; }
      if (error.status === 410) { this.lastEventId = undefined; void this.refresh(); }
      const delay = retryDelaysMs[Math.min(this.retryAttempt, retryDelaysMs.length - 1)] ?? retryDelaysMs[retryDelaysMs.length - 1];
      this.retryAttempt += 1;
      this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; this.connect(); }, delay);
    });
  }

  private selectedStatus(): NotificationStatus | undefined {
    const filter = this.filter();
    return filter === 'all' ? undefined : filter;
  }

  private setError(error: unknown, fallback: string): void {
    const status = typeof error === 'object' && error !== null && 'status' in error && typeof error.status === 'number' ? error.status : null;
    this.errorStatus.set(status); this.notice.set(error instanceof Error && error.message ? error.message : fallback);
  }
}
