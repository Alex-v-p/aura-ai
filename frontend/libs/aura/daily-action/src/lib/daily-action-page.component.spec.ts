import { describe, expect, it } from 'vitest';
import { attentionHasMore, attentionOverflowMessage, buildReminderItems, classifyConversationSourceState, classifyDailySourceState, dailyAuthenticationNotice, isTerminalReminder, limitDailyItems } from './daily-action-aggregation';

describe('DailyActionPageComponent aggregation', () => {
  it('keeps navigation records bounded and excludes terminal or duplicate series reminders', () => {
    const today = new Date();
    const due = { id: 'due', content: 'Due item', dueAt: new Date(today.getTime() - 10_000).toISOString(), status: 'fired', version: 1, createdAt: today.toISOString(), updatedAt: today.toISOString(), firedAt: today.toISOString(), completedAt: null, canceledAt: null } as never;
    const terminal = { ...due, id: 'done', status: 'completed' } as never;
    const series = { id: 'series', content: 'Series item', status: 'active', nextReminderId: 'due', nextOccurrence: new Date(today.getTime() + 86_400_000).toISOString(), readableRule: 'Every day' } as never;
    const boundary = new Date(today.getFullYear(), today.getMonth(), today.getDate(), 23, 59, 59, 999).getTime();
    expect(buildReminderItems([due, terminal], [series], boundary, false, new Set(['due']))).toHaveLength(0);
    expect(isTerminalReminder(terminal)).toBe(true);
  });

  it('shows the next bounded reminder preview without duplicating a series occurrence', () => {
    const now = Date.now();
    const makeReminder = (id: string, offset: number) => ({ id, content: id, dueAt: new Date(now + offset).toISOString(), status: 'scheduled', version: 1, createdAt: new Date(now).toISOString(), updatedAt: new Date(now).toISOString(), firedAt: null, completedAt: null, canceledAt: null } as never);
    const items = buildReminderItems([makeReminder('one', 86_400_000), makeReminder('two', 172_800_000)], [], now, true, new Set());
    expect(items.map((item) => item.kind === 'reminder' ? item.reminder.id : item.series.id)).toEqual(['one', 'two']);
  });

  it('keeps the Today reminder projection bounded after date aggregation', () => {
    const reminders = Array.from({ length: 8 }, (_, index) => `reminder-${index}`);
    expect(limitDailyItems(reminders, 6)).toEqual(reminders.slice(0, 6));
  });

  it('classifies initial source failures before loading and exposes status-less notices', () => {
    expect(classifyDailySourceState({ loading: true, errorStatus: 503, notice: 'Unavailable', hasData: false, loaded: false })).toBe('error');
    expect(classifyDailySourceState({ loading: false, errorStatus: null, notice: 'Network unavailable', hasData: false, loaded: true })).toBe('error');
    expect(classifyDailySourceState({ loading: true, errorStatus: 403, notice: 'Forbidden', hasData: false, loaded: false })).toBe('auth');
  });

  it('keeps an active initial refresh in loading despite an offline connection default', () => {
    expect(classifyDailySourceState({ loading: true, errorStatus: null, notice: null, hasData: false, loaded: false, connection: 'offline' })).toBe('loading');
  });

  it('marks cursor-backed attention projections as incomplete without showing an exact total', () => {
    expect(attentionHasMore(1, 'approval-next-page', null)).toBe(true);
    expect(attentionOverflowMessage(1, true)).toContain('may be available');
    expect(attentionOverflowMessage(6, true)).toContain('up to 6');
    expect(attentionOverflowMessage(1, false)).toBeNull();
  });

  it('suppresses source retries after the shared session becomes unauthenticated', () => {
    expect(dailyAuthenticationNotice('unauthenticated')).toBe('sign-in');
    expect(dailyAuthenticationNotice('error')).toBe('unavailable');
    expect(dailyAuthenticationNotice('loading')).toBe('loading');
    expect(dailyAuthenticationNotice('authenticated')).toBe('none');
  });

  it('maps retained-session permission failures to an error without sign-in guidance', () => {
    expect(classifyConversationSourceState('error', false)).toBe('error');
    expect(classifyConversationSourceState('unauthenticated', false)).toBe('auth');
    expect(classifyConversationSourceState('loading', false)).toBe('loading');
  });
});
