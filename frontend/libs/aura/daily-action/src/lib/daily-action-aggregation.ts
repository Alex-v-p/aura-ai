import type { Reminder, ReminderSeries } from '@aura/aura-api-client';

export type DailyReminderItem = { readonly kind: 'reminder'; readonly reminder: Reminder } | { readonly kind: 'series'; readonly series: ReminderSeries };
export type DailySourceState = 'loading' | 'ready' | 'error' | 'auth' | 'offline' | 'stale';
export type DailyAuthenticationState = 'loading' | 'authenticated' | 'unauthenticated' | 'error';
export type DailyAuthenticationNotice = 'loading' | 'sign-in' | 'unavailable' | 'none';
export const MAX_NEEDS_ATTENTION = 6;
export const isTerminalReminder = (reminder: Reminder): boolean => reminder.status === 'completed' || reminder.status === 'canceled';

type DailyConnectionState = 'offline' | 'connecting' | 'reconnecting' | 'live' | 'unavailable' | null;

export function dailyAuthenticationNotice(state: DailyAuthenticationState): DailyAuthenticationNotice {
  if (state === 'loading') return 'loading';
  if (state === 'unauthenticated') return 'sign-in';
  if (state === 'error') return 'unavailable';
  return 'none';
}

export function classifyConversationSourceState(state: DailyAuthenticationState, hasLibraryError: boolean): DailySourceState {
  if (state === 'loading') return 'loading';
  if (state === 'unauthenticated') return 'auth';
  if (state === 'error' || hasLibraryError) return 'error';
  return 'ready';
}

export function classifyDailySourceState(options: {
  readonly loading: boolean;
  readonly errorStatus: number | null;
  readonly notice: string | null;
  readonly hasData: boolean;
  readonly loaded: boolean;
  readonly connection?: DailyConnectionState;
}): DailySourceState {
  if (options.errorStatus === 401 || options.errorStatus === 403) return 'auth';
  if (options.errorStatus) return options.hasData ? 'stale' : 'error';
  if (options.notice) return options.hasData ? 'stale' : 'error';
  if (options.loading || !options.loaded) return 'loading';
  if (options.connection === 'offline' || options.connection === 'unavailable') return 'offline';
  if (options.connection === 'reconnecting') return 'stale';
  return 'ready';
}

export function dailySourceMessage(state: DailySourceState, source: string, errorStatus: number | null = null): string {
  if (state === 'auth' && errorStatus === 403) return `You do not have permission to view ${source}.`;
  if (state === 'auth') return `Sign in again to view ${source}.`;
  switch (state) {
    case 'loading': return `Loading ${source}…`;
    case 'offline': return `${source} is unavailable offline. Reconnect to refresh it.`;
    case 'stale': return `${source} may be out of date while Aura reconnects.`;
    case 'error': return `${source} could not be loaded.`;
    default: return '';
  }
}

export function attentionOverflowMessage(shown: number, hasMore: boolean, limit = MAX_NEEDS_ATTENTION): string | null {
  if (!hasMore) return null;
  if (shown >= limit) return `Showing up to ${limit} items needing attention. Open Approvals or Notifications to see more.`;
  return 'More attention items may be available. Open Approvals or Notifications to view them.';
}

export function attentionHasMore(shown: number, approvalNextCursor: string | null, notificationNextCursor: string | null, limit = MAX_NEEDS_ATTENTION): boolean {
  return shown >= limit || approvalNextCursor !== null || notificationNextCursor !== null;
}

export function limitDailyItems<T>(items: ReadonlyArray<T>, limit: number): ReadonlyArray<T> { return items.slice(0, limit); }

export function buildReminderItems(upcoming: ReadonlyArray<Reminder>, series: ReadonlyArray<ReminderSeries>, boundary: number, later: boolean, seriesReminderIds: ReadonlySet<string>): ReadonlyArray<DailyReminderItem> {
  const reminders = upcoming
    .filter((reminder) => !isTerminalReminder(reminder) && (later ? Date.parse(reminder.dueAt) > boundary : Date.parse(reminder.dueAt) <= boundary) && !seriesReminderIds.has(reminder.id))
    .map((reminder) => ({ kind: 'reminder' as const, reminder }));
  const recurring = series
    .filter((item) => item.status === 'active' && item.nextOccurrence && (later ? Date.parse(item.nextOccurrence) > boundary : Date.parse(item.nextOccurrence) <= boundary))
    .map((series) => ({ kind: 'series' as const, series }));
  return [...reminders, ...recurring].sort((left, right) => Date.parse(left.kind === 'series' ? (left.series.nextOccurrence ?? '') : left.reminder.dueAt) - Date.parse(right.kind === 'series' ? (right.series.nextOccurrence ?? '') : right.reminder.dueAt));
}
