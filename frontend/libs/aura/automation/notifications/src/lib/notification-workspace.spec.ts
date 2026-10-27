import { readFileSync } from 'node:fs';
import { describe, expect, it, vi } from 'vitest';
import type { Notification } from '@aura/aura-api-client';
import { appendNotificationPage, isValidNotificationEvent, loadUnreadProjection, notificationBriefingResult, notificationSource, NotificationApiError, NotificationMutationGuard, reconcileAcknowledged, type NotificationCollection, type NotificationsApi } from './notification-models';
import { NotificationSessionHydrator } from './notification-session-hydration';

const page = readFileSync('libs/aura/automation/notifications/src/lib/notifications-page.component.html', 'utf8');
const store = readFileSync('libs/aura/automation/notifications/src/lib/notification-store.ts', 'utf8');
const api = readFileSync('libs/aura/automation/notifications/src/lib/notifications-api.ts', 'utf8');

describe('notification centre contracts', () => {
  it('keeps the server-authoritative unread count when loading another page', () => {
    const first: Notification = { id: 'notification-1', reminderId: 'reminder-1', status: 'delivered', version: 1, createdAt: '2026-10-08T09:00:00.000Z', updatedAt: '2026-10-08T09:00:00.000Z', deliveredAt: '2026-10-08T09:00:00.000Z', acknowledgedAt: null };
    const second: Notification = { ...first, id: 'notification-2', reminderId: 'reminder-2' };
    const current: NotificationCollection = { items: [first], nextCursor: 'cursor-1', unacknowledgedCount: 4 };
    const next: NotificationCollection = { items: [second], nextCursor: null, unacknowledgedCount: 3 };
    const merged = appendNotificationPage(current, next);
    expect(merged.items.map((item) => item.id)).toEqual(['notification-1', 'notification-2']);
    expect(merged.unacknowledgedCount).toBe(3);
  });

  it('keeps the inbox actions accessible and durable', () => {
    expect(page).toContain('aria-labelledby="notifications-heading"');
    expect(page).toContain('Activity · delivery updates');
    expect(page).toContain('When something needs your attention, its notification will appear here.');
    expect(page).toContain('Acknowledge');
    expect(page).toContain('Mark reminder complete');
    expect(page).toContain('<ul class="notification-list"');
    expect(page).toContain('role="dialog"');
  });

  it('reconciles durable state after metadata-only SSE updates with bounded retry', () => {
    expect(store).toContain('void this.refresh()');
    expect(store).toContain('this.lastEventId = event.eventId');
    expect(store).toContain('retryDelaysMs');
    expect(store).toContain('this.api.stream(this.lastEventId');
    expect(store).toContain('private streamGeneration = 0');
    expect(store).toContain('await this.refresh()');
    expect(store).toContain('async refresh(): Promise<boolean>');
    expect(store).toContain('if (!reconciled) { this.connectionState.set(\'unavailable\'); return; }');
    expect(store).toContain('private async reconcileAndConnect()');
  });

  it('keeps Today on a stable unread projection when the workspace changes view', () => {
    expect(store).toContain('readonly unreadCollection = signal<NotificationCollection | null>(null)');
    expect(store).toContain('if (view === \'unread\') {');
    expect(store).toContain('this.unreadCollection.set(page)');
    expect(store).toContain('this.unreadCollection.set(null)');
    expect(store).toContain('loadUnreadProjection(this.api)');
    expect(store).toContain("reconcileAcknowledged(current, updated, 'unread')");
    expect(readFileSync('libs/aura/daily-action/src/lib/daily-action-page.component.ts', 'utf8')).toContain('this.notifications.unreadCollection()');
  });

  it('does not consume a second unread request while switching to History', () => {
    expect(store).not.toContain("else {\n        try {\n          const unread = await loadUnreadProjection(this.api);");
    expect(store).toContain("if (this.view() === 'history') void this.refreshUnreadProjection()");
    expect(store).toContain('async refreshUnreadProjection(): Promise<boolean>');
  });

  it('exposes an explicit Today reconciliation without changing the inbox view', () => {
    expect(store).toContain('async refreshUnreadProjection(): Promise<boolean>');
    expect(store).toContain('this.unreadCollection.set(unread)');
    expect(store).toContain('readonly unreadProjectionLoading = signal(false)');
    expect(store).toContain('readonly unreadProjectionErrorStatus = signal<number | null>(null)');
    expect(store).toContain('readonly unreadProjectionNotice = signal<string | null>(null)');
    expect(store).toContain('retryUnreadProjection(): void');
    expect(store).not.toContain("this.setError(error, 'Unread notifications are temporarily unavailable.')");
  });

  it('keeps Today projection recovery separate from the active History connection', () => {
    const daily = readFileSync('libs/aura/daily-action/src/lib/daily-action-page.component.ts', 'utf8');
    expect(daily).toContain('historyWorkspaceActive ? this.notifications.unreadProjectionLoading() : this.notifications.loading()');
    expect(daily).toContain('historyWorkspaceActive ? this.notifications.unreadProjectionErrorStatus() : this.notifications.errorStatus()');
    expect(daily).toContain('historyWorkspaceActive ? this.notifications.unreadProjectionNotice() : this.notifications.notice()');
    expect(daily).toContain('historyWorkspaceActive ? null : this.notifications.connectionState()');
    expect(daily).toContain("if (this.notifications.view() === 'history') this.notifications.retryUnreadProjection(); else this.notifications.retryConnection();");
    expect(daily).toContain('readonly notificationLiveState = computed<DailySourceState>');
    expect(daily).toContain('retryNotificationConnection(): void');
    expect(page).toContain('Retry live updates');
  });

  it('falls back to the notifications heading when an acknowledged opener no longer exists', () => {
    const component = readFileSync('libs/aura/automation/notifications/src/lib/notifications-page.component.ts', 'utf8');
    expect(component).toContain('if (await this.store.acknowledge(notification) && !detailOpen) this.scheduleHeadingFocus()');
    expect(component).toContain('focusNotificationReturnTarget(this.returnFocusState.returnFocusId)');
    expect(component).toContain('private scheduleHeadingFocus(): void');
    expect(readFileSync('libs/aura/automation/notifications/src/lib/notification-focus.ts', 'utf8')).toContain('const target = opener ??');
  });

  it('does not present a deferred briefing destination as busy', () => {
    expect(page).toContain('class="secondary-button deferred-action"');
    expect(readFileSync('libs/aura/automation/notifications/src/lib/notifications-page.component.css', 'utf8')).toContain('button.deferred-action:disabled { cursor: default; }');
  });

  it('fetches a fresh unread projection without changing the active workspace view', async () => {
    const delivered: Notification = { id: 'notification-1', reminderId: 'reminder-1', status: 'delivered', version: 1, createdAt: '2026-10-08T09:00:00.000Z', updatedAt: '2026-10-08T09:00:00.000Z', deliveredAt: '2026-10-08T09:00:00.000Z', acknowledgedAt: null };
    const laterDelivered: Notification = { ...delivered, id: 'notification-2', reminderId: 'reminder-2' };
    const unreadPage = (items: Notification[]): NotificationCollection => ({ items, nextCursor: null, unacknowledgedCount: items.length });
    const historyPage: NotificationCollection = { items: [], nextCursor: null, unacknowledgedCount: 0 };
    let latestUnread = unreadPage([delivered]);
    const api: NotificationsApi = {
      list: vi.fn(async (_cursor?: string, status?: Notification['status']) => status === 'delivered' ? latestUnread : historyPage),
      get: vi.fn(),
      acknowledge: vi.fn(),
      stream: vi.fn(() => ({ close: vi.fn() })),
    };
    latestUnread = unreadPage([delivered, laterDelivered]);
    const refreshed = await loadUnreadProjection(api);
    expect(refreshed.items.map((item) => item.id)).toEqual(['notification-1', 'notification-2']);
    expect(api.list).toHaveBeenCalledTimes(1);
    expect(api.list).toHaveBeenCalledWith(undefined, 'delivered');
  });

  it('uses the generated notification operations and owner mutation headers', () => {
    expect(api).toContain("this.client.execute('listNotifications'");
    expect(api).toContain("this.client.execute('getNotification'");
    expect(api).toContain("this.client.execute('acknowledgeNotification'");
    expect(api).toContain("'Last-Event-ID': lastEventId");
    expect(api).toContain("'X-CSRF-Token': this.session.currentToken()");
    expect(api).toContain("'Idempotency-Key': idempotencyKey()");
    expect(api).toContain('if (signal.aborted) return;');
    expect(api).toContain('isValidNotificationEvent(payload)');
  });

  it('removes acknowledged rows from Unread before authoritative reconciliation', () => {
    const unread = { id: 'notification-1', reminderId: 'reminder-1', status: 'delivered', version: 1, createdAt: '2026-10-08T09:00:00.000Z', updatedAt: '2026-10-08T09:00:00.000Z', deliveredAt: '2026-10-08T09:00:00.000Z', acknowledgedAt: null } satisfies Notification;
    const acknowledged = { ...unread, status: 'acknowledged', version: 2, acknowledgedAt: '2026-10-08T09:01:00.000Z' } satisfies Notification;
    const result = reconcileAcknowledged({ items: [unread], nextCursor: null, unacknowledgedCount: 1 }, acknowledged, 'unread');
    expect(result.items).toEqual([]);
    expect(result.unacknowledgedCount).toBe(0);
  });

  it('rejects metadata events without a bounded UUID event id', () => {
    const base = { schemaVersion: 1, eventType: 'notification.delivered', notificationId: 'notification-1', reminderId: 'reminder-1', status: 'delivered', notificationVersion: 1, occurredAt: '2026-10-08T09:00:00.000Z' } as const;
    expect(isValidNotificationEvent({ ...base, eventId: 'notification-event-1' })).toBe(false);
    expect(isValidNotificationEvent({ ...base, eventId: '550e8400-e29b-41d4-a716-446655440000' })).toBe(true);
  });

  it('recognizes briefing sources without inventing a reminder identity or exposing content', () => {
    const briefing = { id: 'notification-briefing', briefingScheduleId: 'schedule-1', briefingOccurrenceId: 'occurrence-1', briefingResultId: 'result-1', status: 'delivered', version: 1, createdAt: '2026-10-08T09:00:00.000Z', updatedAt: '2026-10-08T09:00:00.000Z', deliveredAt: '2026-10-08T09:00:00.000Z', acknowledgedAt: null } as unknown as Notification;
    expect(notificationSource(briefing)).toEqual({ kind: 'briefing', briefingResultId: 'result-1', destination: 'briefing_result' });
    expect(notificationBriefingResult(briefing)?.destination).toBe('briefing_result');
    expect(notificationSource(briefing)).not.toHaveProperty('reminderId');
    expect(isValidNotificationEvent({ schemaVersion: 1, eventId: '550e8400-e29b-41d4-a716-446655440000', eventType: 'notification.delivered', notificationId: 'notification-briefing', briefingScheduleId: 'schedule-1', briefingOccurrenceId: 'occurrence-1', briefingResultId: 'result-1', status: 'delivered', notificationVersion: 1, occurredAt: '2026-10-08T09:00:00.000Z' })).toBe(true);
  });

  it('rejects incomplete, mixed, and non-contract notification event sources', () => {
    const base = { schemaVersion: 1, eventId: '550e8400-e29b-41d4-a716-446655440000', eventType: 'notification.delivered', notificationId: 'notification-briefing', status: 'delivered', notificationVersion: 1, occurredAt: '2026-10-08T09:00:00.000Z' } as const;
    expect(isValidNotificationEvent({ ...base, briefingResultId: 'result-1' })).toBe(false);
    expect(isValidNotificationEvent({ ...base, reminderId: 'reminder-1', briefingScheduleId: 'schedule-1', briefingOccurrenceId: 'occurrence-1', briefingResultId: 'result-1' })).toBe(false);
    expect(isValidNotificationEvent({ ...base, source: { kind: 'briefing', briefingResultId: 'result-1', destination: 'briefing_result' } })).toBe(false);
    expect(isValidNotificationEvent({ ...base, reminderId: 'reminder-1', extra: 'not-in-contract' })).toBe(false);
  });

  it('does not let an old hydration repopulate after clearSession and new hydration', async () => {
    let resolveOld!: (token: string) => void;
    let resolveNew!: (token: string) => void;
    const hydrator = new NotificationSessionHydrator(() => new Promise<string>((resolve) => {
      if (!resolveOld) resolveOld = resolve;
      else resolveNew = resolve;
    }), () => new NotificationApiError(401, 'Session changed.'));
    const old = hydrator.ensure();
    hydrator.clearSession();
    const replacement = hydrator.ensure();
    resolveNew('new-token');
    await replacement;
    resolveOld('old-token');
    await expect(old).rejects.toMatchObject({ status: 401 });
    expect(hydrator.currentToken()).toBe('new-token');
  });

  it('invalidates delayed acknowledgement publication across route and session changes', async () => {
    const guard = new NotificationMutationGuard();
    guard.routeChanged('notification-a');
    const routeA = guard.begin('notification-a');
    const delayed = Promise.resolve().then(() => guard.isSessionMutationCurrent(routeA) && guard.isRouteCurrent(routeA));
    guard.routeChanged('notification-b');
    expect(await delayed).toBe(false);
    const routeB = guard.begin('notification-b');
    guard.resetSession();
    expect(guard.isSessionMutationCurrent(routeB)).toBe(false);
    expect(guard.isRouteCurrent(routeB)).toBe(false);
  });

  it('keeps list-origin acknowledgement errors scoped to the inbox surface', () => {
    const guard = new NotificationMutationGuard();
    const listToken = guard.begin('notification-a');
    expect(guard.isRouteCurrent(listToken)).toBe(true);
    guard.routeChanged('notification-b');
    expect(guard.isRouteCurrent(listToken)).toBe(false);
    guard.clearRoute();
    const replacement = guard.begin('notification-c');
    expect(guard.isRouteCurrent(replacement)).toBe(true);
  });
});
