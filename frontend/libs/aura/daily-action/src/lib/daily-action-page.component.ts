import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ReminderStore } from '@aura/aura/automation/reminders';
import { NotificationStore } from '@aura/aura/automation/notifications';
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
    return this.sourceState(
      this.notifications.loading(),
      this.notifications.errorStatus(),
      (this.notifications.unreadCollection()?.items.length ?? 0) > 0,
      this.notifications.notice(),
      this.notifications.unreadLoaded(),
      this.notifications.connectionState(),
    );
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

  constructor() {
    // Reminder reads are scoped to this feature. Shell-owned notification and
    // approval streams are intentionally not started a second time here.
    void this.reminders.load();
  }

  retryReminders(): void { void this.reminders.load(); }
  retryNotifications(): void { this.notifications.retryConnection(); }
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
  sourceState(loading: boolean, errorStatus: number | null, hasData: boolean, notice: string | null = null, loaded = true, connection: DailyConnectionState = null): DailySourceState {
    return classifyDailySourceState({ loading, errorStatus, hasData, notice, loaded, connection });
  }
  sourceMessage(state: DailySourceState, source: string, errorStatus: number | null = null): string { return dailySourceMessage(state, source, errorStatus); }
  canRetrySource(state: DailySourceState): boolean { return state !== 'auth' && this.authenticationState() === 'authenticated'; }
}
