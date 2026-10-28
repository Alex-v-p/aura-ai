import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ReminderStore } from '@aura/aura/automation/reminders';
import { notificationSource, NotificationStore } from '@aura/aura/automation/notifications';
import { ApprovalStore, type ApprovalSummary } from '@aura/aura/governance/approvals';
import { ConversationStore, type AuthState, type Conversation } from '@aura/aura/interaction/conversations';
import {
  attentionHasMore,
  attentionOverflowMessage,
  buildReminderItems,
  classifyDailySourceState,
  classifyConversationSourceState,
  dailyAuthenticationNotice,
  dailySourceMessage,
  limitDailyItems,
  MAX_NEEDS_ATTENTION,
  type DailyReminderItem,
  type DailySourceState,
} from './daily-action-aggregation';
import { calendarConflictsForEvent, calendarEventIdentity, DAILY_CALENDAR_AGENDA, finalIncludedCalendarDate, type DailyCalendarAgendaStatus, type DailyCalendarConflictProjection, type DailyCalendarEventProjection } from './daily-calendar-agenda';
export { calendarConflictForEvent, calendarConflictsForEvent, calendarEventIdentity, DAILY_CALENDAR_AGENDA, finalIncludedCalendarDate, invertCalendarConflictRelationship, type DailyCalendarAgendaProjection, type DailyCalendarAgendaStatus, type DailyCalendarConflictMatch, type DailyCalendarConflictProjection, type DailyCalendarEventProjection } from './daily-calendar-agenda';
export { attentionHasMore, attentionOverflowMessage, buildReminderItems, classifyConversationSourceState, classifyDailySourceState, dailyAuthenticationNotice, dailySourceMessage, isTerminalReminder, limitDailyItems, type DailyAuthenticationNotice, type DailyAuthenticationState, type DailyReminderItem, type DailySourceState } from './daily-action-aggregation';

type DailyConnectionState = 'offline' | 'connecting' | 'reconnecting' | 'live' | 'unavailable' | null;
const MAX_CONTINUE = 5;
const MAX_LATER = 4;
const MAX_TODAY = 6;

const dayEnd = (date = new Date()): number => new Date(date.getFullYear(), date.getMonth(), date.getDate(), 23, 59, 59, 999).getTime();

@Component({
  selector: 'aura-daily-action-page',
  standalone: true,
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './daily-action-page.component.html',
  styleUrl: './daily-action-page.component.css',
})
export class DailyActionPageComponent {
  readonly reminders = inject(ReminderStore);
  readonly notifications = inject(NotificationStore);
  readonly approvals = inject(ApprovalStore);
  readonly conversations = inject(ConversationStore);
  /** Calendar is an optional public projection; Today must not depend on it for reminders. */
  readonly calendar = inject(DAILY_CALENDAR_AGENDA);

  readonly now = new Date();
  readonly dayEnd = dayEnd(this.now);
  readonly reminderSeriesIds = computed(() => new Set(this.reminders.series().items.map((series) => series.nextReminderId).filter((id): id is string => Boolean(id))));
  readonly pendingApprovals = computed(() => this.approvals.pendingCollection().items.filter((item) => item.status === 'pending').sort((left, right) => Date.parse(left.expiresAt) - Date.parse(right.expiresAt)));
  readonly deliveredNotifications = computed(() => (this.notifications.unreadCollection()?.items ?? []).filter((item) => item.status === 'delivered').sort((left, right) => Date.parse(left.deliveredAt) - Date.parse(right.deliveredAt)));
  readonly needsAttention = computed(() => [
    ...this.pendingApprovals().map((approval) => ({ kind: 'approval' as const, approval })),
    ...this.deliveredNotifications().map((notification) => ({ kind: 'notification' as const, notification })),
  ].slice(0, MAX_NEEDS_ATTENTION));
  readonly attentionOverflow = computed(() => attentionOverflowMessage(
    this.needsAttention().length,
    attentionHasMore(this.needsAttention().length, this.approvals.pendingCollection().nextCursor, this.notifications.unreadCollection()?.nextCursor ?? null),
  ));

  readonly todayItems = computed<ReadonlyArray<DailyReminderItem>>(() => limitDailyItems(buildReminderItems(this.reminders.upcoming().items, this.reminders.series().items, this.dayEnd, false, this.reminderSeriesIds()), MAX_TODAY));
  readonly laterItems = computed<ReadonlyArray<DailyReminderItem>>(() => buildReminderItems(this.reminders.upcoming().items, this.reminders.series().items, this.dayEnd, true, this.reminderSeriesIds()).slice(0, MAX_LATER));
  readonly recentConversations = computed(() => this.conversations.conversations()
    .filter((conversation) => !conversation.id.startsWith('draft-') && !conversation.archivedAt)
    .sort((left, right) => right.updatedAt - left.updatedAt)
    .slice(0, MAX_CONTINUE));

  readonly authenticationState = computed<AuthState>(() => this.conversations.authState());
  readonly authenticationNotice = computed(() => dailyAuthenticationNotice(this.authenticationState()));
  readonly reminderState = computed<DailySourceState>(() => this.sourceState(this.reminders.loading(), this.reminders.errorStatus(), this.reminders.upcoming().items.length > 0 || this.reminders.series().items.length > 0, this.reminders.notice()));
  readonly notificationState = computed<DailySourceState>(() => {
    const historyWorkspaceActive = this.notifications.view() === 'history';
    return this.sourceState(
      historyWorkspaceActive ? this.notifications.unreadProjectionLoading() : this.notifications.loading(),
      historyWorkspaceActive ? this.notifications.unreadProjectionErrorStatus() : this.notifications.errorStatus(),
      (this.notifications.unreadCollection()?.items.length ?? 0) > 0,
      historyWorkspaceActive ? this.notifications.unreadProjectionNotice() : this.notifications.notice(),
      this.notifications.unreadLoaded(),
      historyWorkspaceActive ? null : this.notifications.connectionState(),
    );
  });
  readonly notificationLiveState = computed<DailySourceState>(() => {
    if (this.notifications.view() !== 'history') return 'ready';
    return this.sourceState(false, this.notifications.errorStatus(), true, this.notifications.notice(), true, this.notifications.connectionState());
  });
  readonly approvalState = computed<DailySourceState>(() => {
    return this.sourceState(
      this.approvals.loading(),
      this.approvals.errorStatus(),
      this.approvals.pendingCollection().items.length > 0,
      this.approvals.notice(),
      this.approvals.loadedView() !== null,
      this.approvals.connectionState(),
    );
  });
  readonly conversationState = computed<DailySourceState>(() => {
    return classifyConversationSourceState(this.authenticationState(), Boolean(this.conversations.libraryError()));
  });
  readonly calendarStatus = computed<DailyCalendarAgendaStatus>(() => this.calendar?.status() ?? 'disconnected');
  readonly calendarEvents = computed<ReadonlyArray<DailyCalendarEventProjection>>(() => {
    const events = this.calendar?.events() ?? [];
    return [...events].sort((left, right) => {
      if (left.kind !== right.kind) return left.kind === 'all_day' ? -1 : 1;
      const leftStart = left.kind === 'all_day' ? left.startDate ?? '' : left.startInstant ?? '';
      const rightStart = right.kind === 'all_day' ? right.startDate ?? '' : right.startInstant ?? '';
      return leftStart.localeCompare(rightStart) || left.title.localeCompare(right.title);
    });
  });
  readonly calendarAllDayEvents = computed(() => this.calendarEvents().filter((event) => event.kind === 'all_day'));
  readonly calendarTimedEvents = computed(() => this.calendarEvents().filter((event) => event.kind === 'timed'));
  readonly calendarConflicts = computed<ReadonlyArray<DailyCalendarConflictProjection>>(() => this.calendar?.conflicts() ?? []);
  readonly calendarVisibleDate = computed(() => this.calendar?.visibleDate() ?? this.localDate(this.now));
  readonly calendarDisplayTimeZone = computed(() => this.calendar?.displayTimeZone() ?? this.browserTimeZone());
  readonly calendarNotice = computed(() => this.calendar?.notice() ?? null);

  constructor() {
    // Reminder reads are scoped to this feature. Shell-owned notification and
    // approval streams are intentionally not started a second time here. The
    // calendar projection owns an idempotent connection/discovery start at
    // this same authenticated Today boundary.
    void this.reminders.load();
    // History is a separate inbox view, so reconcile Today’s stable unread
    // projection when the owner returns to this route.
    if (this.notifications.view() === 'history') void this.notifications.refreshUnreadProjection();
    void this.calendar?.start();
  }

  retryReminders(): void { void this.reminders.load(); }
  retryNotifications(): void { if (this.notifications.view() === 'history') this.notifications.retryUnreadProjection(); else this.notifications.retryConnection(); }
  retryNotificationConnection(): void { this.notifications.retryConnection(); }
  notificationErrorStatus(): number | null { return this.notifications.view() === 'history' ? this.notifications.unreadProjectionErrorStatus() : this.notifications.errorStatus(); }
  retryApprovals(): void { this.approvals.retryConnection(); }
  retryConversations(): void { void this.conversations.load(); }
  signIn(): void { this.conversations.login(); }
  itemTimestamp(item: DailyReminderItem): number { return Date.parse(item.kind === 'series' ? (item.series.nextOccurrence ?? '') : item.reminder.dueAt); }
  itemLabel(item: DailyReminderItem): string { return item.kind === 'series' ? item.series.content : item.reminder.content; }
  itemStatus(item: DailyReminderItem): string { return item.kind === 'series' ? `Next · ${item.series.readableRule}` : item.reminder.status === 'fired' ? 'Due now' : 'Scheduled'; }
  formatDate(value: string): string { return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(new Date(value)); }
  formatUpdatedAt(value: number): string { return new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(new Date(value)); }
  conversationStatus(conversation: Conversation): string { return conversation.currentRun?.status ? conversation.currentRun.status.replaceAll('_', ' ') : 'Ready to continue'; }
  approvalLabel(approval: ApprovalSummary): string { return approval.expiresAt ? `Approval awaiting review · expires ${this.formatDate(approval.expiresAt)}` : 'Approval awaiting review'; }
  notificationLabel(notification: Parameters<typeof notificationSource>[0]): string { return notificationSource(notification)?.kind === 'briefing' ? 'Briefing ready' : 'Reminder notification'; }
  sourceState(loading: boolean, errorStatus: number | null, hasData: boolean, notice: string | null = null, loaded = true, connection: DailyConnectionState = null): DailySourceState {
    return classifyDailySourceState({ loading, errorStatus, hasData, notice, loaded, connection });
  }
  sourceMessage(state: DailySourceState, source: string, errorStatus: number | null = null): string { return dailySourceMessage(state, source, errorStatus); }
  canRetrySource(state: DailySourceState): boolean { return state !== 'auth' && this.authenticationState() === 'authenticated'; }
  retryCalendar(): void { void this.calendar?.retry(); }
  calendarStatusMessage(): string {
    const notice = this.calendarNotice();
    if (notice) return notice;
    switch (this.calendarStatus()) {
      case 'loading': return 'Loading calendar events…';
      case 'connecting': return 'Connecting your calendar…';
      case 'reconnecting': return 'Reconnecting your calendar…';
      case 'reauthorization-required': return 'Calendar authorization needs your attention.';
      case 'revocation-uncertain': return 'Calendar access is being safely disconnected. Reconnect when Aura confirms it is ready.';
      case 'disabled': return 'Calendar access is disabled by your administrator.';
      case 'rate-limited': return 'Calendar reads are temporarily rate-limited.';
      case 'permission-denied': return 'Calendar permission was denied. Review the connection to continue.';
      case 'offline': return 'Calendar is unavailable while you are offline.';
      case 'stale': return 'Showing the last calendar snapshot. Refresh when you are ready.';
      case 'partial': return 'Some calendar events could not be loaded. The available events are shown.';
      case 'unavailable': return 'Calendar is temporarily unavailable.';
      case 'empty': return 'No calendar events are scheduled for this day.';
      case 'disconnected': return 'Connect a calendar to see your day alongside reminders.';
      default: return 'Calendar events are ready.';
    }
  }
  calendarNeedsRetry(): boolean { return ['unavailable', 'rate-limited', 'stale', 'offline', 'reconnecting', 'partial'].includes(this.calendarStatus()); }
  calendarEventTime(event: DailyCalendarEventProjection): string {
    if (event.kind === 'all_day') return event.spansMultipleDays ? `${this.formatCalendarDate(event.startDate)} – ${this.formatCalendarDate(finalIncludedCalendarDate(event.endDateExclusive ?? ''))}` : 'All day';
    const start = this.formatCalendarTime(event.startInstant);
    const end = this.formatCalendarTime(event.endInstant);
    return end ? `${start} – ${end}` : start;
  }
  calendarEventSource(event: DailyCalendarEventProjection): string {
    if (event.kind === 'all_day') return event.spansMultipleDays ? 'Calendar · all day · multi-day' : 'Calendar · all day';
    return event.eventTimeZone !== event.displayTimeZone ? `Calendar · ${event.eventTimeZone} → ${event.displayTimeZone}` : 'Calendar · timed';
  }
  calendarEventKey(event: DailyCalendarEventProjection): string { return calendarEventIdentity(event.calendarId, event.id); }
  calendarConflictLabels(event: DailyCalendarEventProjection): ReadonlyArray<string> {
    return calendarConflictsForEvent(event, this.calendarConflicts()).map((match) => {
      const other = this.calendarEvents().find((candidate) => candidate.id === match.other.eventId && candidate.calendarId === match.other.calendarId);
      return `Conflicts with ${other?.title ?? 'another calendar event'} (${this.relationshipLabel(match.relationship)})`;
    });
  }
  private relationshipLabel(relationship: DailyCalendarConflictProjection['relationship']): string { return relationship === 'same_interval' ? 'same time' : relationship.replace('_', ' '); }
  private formatCalendarTime(value: string | undefined): string { return value ? new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit', timeZone: this.calendarDisplayTimeZone() }).format(new Date(value)) : ''; }
  private formatCalendarDate(value: string | undefined): string { return value ? new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' }).format(new Date(`${value}T00:00:00Z`)) : 'Unknown date'; }
  private localDate(date: Date): string { return new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(date); }
  private browserTimeZone(): string { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; }
}
