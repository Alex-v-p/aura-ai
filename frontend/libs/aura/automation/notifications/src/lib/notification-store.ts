import { Injectable, computed, inject, signal } from '@angular/core';
import { ReminderStore } from '@aura/aura/automation/reminders';
import { AuraNotificationsApi } from './notifications-api';
import { appendNotificationPage, loadUnreadProjection, NotificationMutationGuard, reconcileAcknowledged, type NotificationCollection, type NotificationRecord, type NotificationsApi, type NotificationStream, type NotificationStatus, type NotificationView, type NotificationApiError } from './notification-models';

const emptyCollection = (): NotificationCollection => ({ items: [], nextCursor: null, unacknowledgedCount: 0 });
const retryDelaysMs = [500, 1_000, 2_000, 4_000, 8_000, 15_000] as const;
function statusOf(error: unknown): number | null { const status = (error as { readonly status?: unknown })?.status; return typeof status === 'number' ? status : null; }
function safeMessage(error: unknown, fallback: string): string { const status = statusOf(error); if (status === 401) return 'Your session has expired. Sign in again to view notifications.'; if (status === 403) return 'You do not have permission to view these notifications.'; if (status === 404) return 'That notification is no longer available.'; if (status === 409) return 'This notification changed elsewhere. Refresh and try again.'; if (status === 410) return 'The live inbox cursor expired. Aura is catching up safely.'; return fallback; }

@Injectable({ providedIn: 'root' })
export class NotificationStore {
  private readonly api: NotificationsApi = inject(AuraNotificationsApi);
  private readonly reminders: ReminderStore = inject(ReminderStore);
  readonly collection = signal<NotificationCollection>(emptyCollection());
  readonly selected = signal<NotificationRecord | null>(null);
  readonly loading = signal(false);
  readonly detailLoading = signal(false);
  readonly detailError = signal<string | null>(null);
  readonly saving = signal(false);
  readonly notice = signal<string | null>(null);
  readonly errorStatus = signal<number | null>(null);
  readonly connectionState = signal<'offline' | 'connecting' | 'reconnecting' | 'live' | 'unavailable'>('offline');
  readonly view = signal<NotificationView>('unread');
  readonly loadedView = signal<NotificationView | null>(null);
  /** Stable unread projection for cross-surface consumers such as Today. */
  readonly unreadCollection = signal<NotificationCollection | null>(null);
  readonly unreadLoaded = computed(() => this.unreadCollection() !== null);
  /** Recovery for Today must not mutate the active History/SSE workspace. */
  readonly unreadProjectionLoading = signal(false);
  readonly unreadProjectionErrorStatus = signal<number | null>(null);
  readonly unreadProjectionNotice = signal<string | null>(null);
  /** Kept as a compatibility projection for existing consumers. */
  readonly filter = signal<'all' | NotificationStatus>('delivered');
  readonly unreadCount = computed(() => this.collection().unacknowledgedCount);
  readonly resetEpoch = signal(0);
  private stream: NotificationStream | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private started = false;
  private stopped = false;
  private retryAttempt = 0;
  private lastEventId: string | undefined;
  private sessionGeneration = 0;
  private listGeneration = 0;
  private unreadProjectionGeneration = 0;
  private streamGeneration = 0;
  private detailGeneration = 0;
  private detailKey = '';
  private readonly acknowledgeGuard = new NotificationMutationGuard();
  private sessionResetHandler: ((status?: 401 | 403) => void) | null = null;

  async start(): Promise<void> { if (this.started || typeof window === 'undefined') return; this.started = true; this.stopped = false; const reconciled = await this.refresh(); if (reconciled && !this.stopped) this.connect(); else if (!this.stopped) this.connectionState.set('unavailable'); }
  stop(): void { this.stopped = true; this.started = false; ++this.streamGeneration; this.stream?.close(); this.stream = null; if (this.reconnectTimer) clearTimeout(this.reconnectTimer); this.reconnectTimer = null; this.connectionState.set('offline'); }
  retryConnection(): void { void this.reconcileAndConnect(); }

  async refresh(): Promise<boolean> {
    const session = this.sessionGeneration; const generation = ++this.listGeneration; const view = this.view();
    this.loading.set(true); this.notice.set(null); this.errorStatus.set(null);
    try {
      const page = await this.api.list(undefined, this.selectedStatus());
      if (session !== this.sessionGeneration || generation !== this.listGeneration || view !== this.view()) return false;
      this.collection.set(page); this.loadedView.set(view);
      if (view === 'unread') {
        this.unreadCollection.set(page);
        this.clearUnreadProjectionError();
      }
      return true;
    } catch (error: unknown) {
      if (session === this.sessionGeneration && generation === this.listGeneration && view === this.view()) { this.collection.set(emptyCollection()); this.loadedView.set(null); this.setError(error, 'Notifications are temporarily unavailable.'); }
      return false;
    } finally { if (session === this.sessionGeneration && generation === this.listGeneration) this.loading.set(false); }
  }

  async loadMore(): Promise<void> {
    const current = this.collection(); if (!current.nextCursor || this.loading()) return;
    const session = this.sessionGeneration; const generation = this.listGeneration; const view = this.view(); this.loading.set(true);
    try {
      const page = await this.api.list(current.nextCursor, this.selectedStatus());
      if (session === this.sessionGeneration && generation === this.listGeneration && view === this.view()) {
        const merged = appendNotificationPage(current, page);
        this.collection.set(merged);
        if (view === 'unread') {
          this.unreadCollection.set(merged);
          this.clearUnreadProjectionError();
        }
      }
    }
    catch (error: unknown) { if (session === this.sessionGeneration && generation === this.listGeneration && view === this.view()) this.setError(error, 'More notifications could not be loaded.'); }
    finally { if (session === this.sessionGeneration && generation === this.listGeneration) this.loading.set(false); }
  }

  async inspect(id: string): Promise<NotificationRecord | null> {
    const session = this.sessionGeneration; const generation = ++this.detailGeneration; this.detailKey = id; this.acknowledgeGuard.routeChanged(id); this.selected.set(null); this.detailLoading.set(true); this.detailError.set(null); this.notice.set(null); this.saving.set(false);
    try { const notification = await this.api.get(id); if (session !== this.sessionGeneration || generation !== this.detailGeneration || this.detailKey !== id) return null; this.selected.set(notification); return notification; }
    catch (error: unknown) { if (session === this.sessionGeneration && generation === this.detailGeneration && this.detailKey === id) { const message = safeMessage(error, 'That notification could not be loaded.'); if (statusOf(error) === 401 || statusOf(error) === 403) this.setError(error, message); else this.errorStatus.set(statusOf(error)); this.detailError.set(message); } return null; }
    finally { if (session === this.sessionGeneration && generation === this.detailGeneration && this.detailKey === id) this.detailLoading.set(false); }
  }

  clearDetail(): void { ++this.detailGeneration; this.detailKey = ''; this.acknowledgeGuard.clearRoute(); this.selected.set(null); this.detailLoading.set(false); this.detailError.set(null); this.saving.set(false); }

  async acknowledge(notification: NotificationRecord): Promise<boolean> {
    if (notification.status === 'acknowledged') return true;
    const listGeneration = this.listGeneration; const token = this.acknowledgeGuard.begin(notification.id);
    const sameRoute = (): boolean => this.acknowledgeGuard.isRouteCurrent(token);
    this.saving.set(true); this.notice.set(null); this.errorStatus.set(null);
    try {
      const updated = await this.api.acknowledge(notification.id, notification.version);
      if (!this.acknowledgeGuard.isSessionMutationCurrent(token)) return false;
      const currentView = this.view();
      // Do not apply an optimistic decrement onto a collection that was
      // replaced by an intervening authoritative refresh (for example, SSE).
      // The accepted mutation still performs its own authoritative refresh.
      if (listGeneration === this.listGeneration) {
        this.collection.update((current) => reconcileAcknowledged(current, updated, currentView));
        this.unreadCollection.update((current) => current ? reconcileAcknowledged(current, updated, 'unread') : current);
      }
      if (sameRoute()) this.selected.set(updated);
      if (this.acknowledgeGuard.isSessionMutationCurrent(token)) await this.refresh();
      return this.acknowledgeGuard.isSessionMutationCurrent(token);
    } catch (error: unknown) { if (this.acknowledgeGuard.isSessionMutationCurrent(token) && sameRoute()) this.setError(error, statusOf(error) === null ? 'The acknowledgement outcome is unknown. Check the notification before trying again.' : 'This notification could not be acknowledged.'); return false; }
    finally { if (this.acknowledgeGuard.isSessionMutationCurrent(token) && sameRoute()) this.saving.set(false); }
  }

  clearNotice(): void { this.notice.set(null); this.errorStatus.set(null); }
  registerSessionResetHandler(handler: ((status?: 401 | 403) => void) | null): void { this.sessionResetHandler = handler; }
  async setView(view: NotificationView): Promise<void> { if (this.view() === view) return; this.view.set(view); this.filter.set(view === 'unread' ? 'delivered' : 'acknowledged'); this.loadedView.set(null); this.collection.set(emptyCollection()); await this.refresh(); }
  async setFilter(filter: 'all' | NotificationStatus): Promise<void> { this.filter.set(filter); this.view.set(filter === 'acknowledged' ? 'history' : 'unread'); this.loadedView.set(null); this.collection.set(emptyCollection()); await this.refresh(); }

  resetSessionState(): void { ++this.sessionGeneration; ++this.listGeneration; ++this.detailGeneration; ++this.unreadProjectionGeneration; this.detailKey = ''; this.acknowledgeGuard.resetSession(); this.api.clearSession?.(); this.stop(); this.collection.set(emptyCollection()); this.unreadCollection.set(null); this.loadedView.set(null); this.clearDetail(); this.loading.set(false); this.unreadProjectionLoading.set(false); this.saving.set(false); this.notice.set(null); this.errorStatus.set(null); this.clearUnreadProjectionError(); this.lastEventId = undefined; this.retryAttempt = 0; this.resetEpoch.update((value) => value + 1); }
  /** Reconnect only after the application has accepted a new session. */
  resumeAfterAcceptedSession(): void { void this.start(); }

  private connect(): void {
    if (this.stopped || this.stream) return;
    const session = this.sessionGeneration; const streamGeneration = ++this.streamGeneration;
    this.connectionState.set(this.retryAttempt > 0 ? 'reconnecting' : 'connecting');
    this.stream = this.api.stream(this.lastEventId, (event) => {
      if (session !== this.sessionGeneration || streamGeneration !== this.streamGeneration || this.stopped) return;
      this.lastEventId = event.eventId; this.retryAttempt = 0; this.connectionState.set('live'); void this.refresh(); if (this.view() === 'history') void this.refreshUnreadProjection(); void this.reminders.load();
    }, (error) => { if (session === this.sessionGeneration && streamGeneration === this.streamGeneration) void this.handleStreamError(error, session, streamGeneration); });
  }

  private async reconcileAndConnect(): Promise<void> {
    this.stopped = false; this.started = true; this.retryAttempt = 0; this.connectionState.set('connecting');
    const session = this.sessionGeneration;
    const reconciled = await this.refresh();
    if (session !== this.sessionGeneration || this.stopped) return;
    if (!reconciled) { this.connectionState.set('unavailable'); return; }
    this.connect();
  }

  private async handleStreamError(error: NotificationApiError, session: number, streamGeneration: number): Promise<void> {
    if (session !== this.sessionGeneration || streamGeneration !== this.streamGeneration) return;
    this.stream = null; if (this.stopped) return;
    if (!error.retryable) { this.connectionState.set('unavailable'); this.setError(error, 'Live notifications are unavailable.'); return; }
    this.connectionState.set('reconnecting');
    if (error.status === 410) {
      this.lastEventId = undefined;
      const reconciled = await this.refresh();
      if (session !== this.sessionGeneration || streamGeneration !== this.streamGeneration || this.stopped) return;
      if (!reconciled) { this.connectionState.set('unavailable'); return; }
    }
    if (this.retryAttempt >= retryDelaysMs.length) { this.connectionState.set('unavailable'); this.notice.set('Live notifications stopped after several attempts. Retry when you are ready.'); this.errorStatus.set(null); return; }
    const delay = retryDelaysMs[this.retryAttempt]; this.retryAttempt += 1;
    this.reconnectTimer = setTimeout(() => { this.reconnectTimer = null; if (session === this.sessionGeneration && streamGeneration === this.streamGeneration && !this.stopped) this.connect(); }, delay);
  }

  private selectedStatus(): NotificationStatus | undefined { const filter = this.filter(); return filter === 'all' ? undefined : filter; }
  /** Refresh Today’s unread projection without changing the active inbox view. */
  async refreshUnreadProjection(): Promise<boolean> {
    const session = this.sessionGeneration; const generation = ++this.unreadProjectionGeneration;
    this.unreadProjectionLoading.set(true); this.clearUnreadProjectionError();
    try {
      const unread = await loadUnreadProjection(this.api);
      if (session !== this.sessionGeneration || generation !== this.unreadProjectionGeneration) return false;
      this.unreadCollection.set(unread);
      return true;
    } catch (error: unknown) {
      if (session === this.sessionGeneration && generation === this.unreadProjectionGeneration) {
        const status = statusOf(error);
        if (status === 401 || status === 403) {
          const message = status === 401 ? 'Your session has expired. Sign in again to view notifications.' : 'You do not have permission to view notifications.';
          this.resetSessionState();
          this.sessionResetHandler?.(status);
          this.unreadProjectionErrorStatus.set(status);
          this.unreadProjectionNotice.set(message);
        } else {
          this.unreadProjectionErrorStatus.set(status);
          this.unreadProjectionNotice.set(safeMessage(error, 'Unread notifications are temporarily unavailable.'));
        }
      }
      return false;
    } finally { if (session === this.sessionGeneration && generation === this.unreadProjectionGeneration) this.unreadProjectionLoading.set(false); }
  }
  retryUnreadProjection(): void { void this.refreshUnreadProjection(); }
  private clearUnreadProjectionError(): void { this.unreadProjectionErrorStatus.set(null); this.unreadProjectionNotice.set(null); }
  private setError(error: unknown, fallback: string): void {
    const status = statusOf(error);
    if (status === 401 || status === 403) { const message = status === 401 ? 'Your session has expired. Sign in again to view notifications.' : 'You do not have permission to view notifications.'; this.resetSessionState(); this.sessionResetHandler?.(status); this.errorStatus.set(status); this.notice.set(message); return; }
    this.errorStatus.set(status); this.notice.set(safeMessage(error, fallback));
  }
}
