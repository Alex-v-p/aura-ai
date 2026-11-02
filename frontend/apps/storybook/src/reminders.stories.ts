import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { signal } from '@angular/core';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { provideRouter, withDisabledInitialNavigation } from '@angular/router';
import { of } from 'rxjs';
import { RemindersPageComponent, ReminderStore } from '@aura/aura/automation/reminders';

const now = '2026-10-08T09:00:00.000Z';
const series = { id: 'series-story', content: 'Water the plants', recurrence: { frequency: 'weekly' as const, interval: 1, weekdays: [1, 4], monthDay: null, localStart: '2026-10-09T18:30:00', timeZone: 'Europe/Brussels', count: null, endDate: null }, readableRule: 'Every week on Monday and Thursday', timeZone: 'Europe/Brussels', localStart: '2026-10-09T18:30:00', nextOccurrence: '2026-10-09T16:30:00.000Z', nextReminderId: 'reminder-generated', status: 'active' as const, version: 3, missedCount: 0, occurrenceCount: 4, count: null, endDate: null, createdAt: now, updatedAt: now, pausedAt: null, completedAt: null, canceledAt: null };
const occurrence = { id: 'occurrence-story', seriesId: series.id, reminderId: series.nextReminderId!, sequence: 1, outcome: 'completed' as const, localDueAt: series.localStart, dueAt: series.nextOccurrence!, occurredAt: series.nextOccurrence!, createdAt: now, updatedAt: now, skippedAt: null, firedAt: series.nextOccurrence!, completedAt: series.nextOccurrence! };
const seriesRoute = (): ActivatedRoute => ({ paramMap: of(convertToParamMap({ seriesId: series.id })) }) as unknown as ActivatedRoute;

function fixtureStore(status: 'active' | 'paused' | 'completed' | 'canceled' = 'active', withHistory = false): Partial<ReminderStore> {
  const current = { ...series, status };
  return { upcoming: signal({ items: [], nextCursor: null }), history: signal({ items: [], nextCursor: null }), series: signal({ items: [current], nextCursor: null }), selected: signal(null), selectedSeries: signal(current), occurrences: signal({ items: withHistory ? [occurrence] : [], nextCursor: null }), loading: signal(false), loadingMore: signal(false), detailLoading: signal(false), detailError: signal(null), seriesDetailLoading: signal(false), seriesDetailError: signal(null), saving: signal(false), notice: signal(null), errorStatus: signal(null), createOutcome: signal('idle'), seriesMutationOutcome: signal('idle'), canReconcileRecurringCreate: signal(false), resetEpoch: signal(0), load: async () => undefined, loadMore: async () => undefined, loadMoreSeries: async () => undefined, inspect: async () => null, inspectSeries: async () => current, clearDetail: () => undefined, create: async () => true, createRecurring: async () => true, reconcileRecurringCreate: async () => false, markCreateDraftChanged: () => undefined, clearCreateDraft: () => undefined, complete: async () => true, cancel: async () => true, pauseSeries: async () => true, resumeSeries: async () => true, skipNextSeriesOccurrence: async () => true, cancelSeries: async () => true, updateSeries: async () => true, clearNotice: () => undefined, registerSessionResetHandler: () => undefined, resetSessionState: () => undefined } as unknown as Partial<ReminderStore>;
}

const meta: Meta<RemindersPageComponent> = { title: 'Aura/Reminders recurring experience', component: RemindersPageComponent, parameters: { layout: 'fullscreen' }, decorators: [applicationConfig({ providers: [provideRouter([], withDisabledInitialNavigation()), { provide: ReminderStore, useValue: fixtureStore() }] })] };
export default meta;
type Story = StoryObj<RemindersPageComponent>;
export const RecurringComposer: Story = { name: 'Recurring composer with validation' };
export const SeriesDetail: Story = { name: 'Recurring series detail', decorators: [applicationConfig({ providers: [{ provide: ActivatedRoute, useValue: seriesRoute() }, { provide: ReminderStore, useValue: fixtureStore() }] })] };
export const SeriesHistory: Story = { name: 'Recurring series history', decorators: [applicationConfig({ providers: [{ provide: ActivatedRoute, useValue: seriesRoute() }, { provide: ReminderStore, useValue: fixtureStore('active', true) }] })] };
export const Paused: Story = { name: 'Paused series', decorators: [applicationConfig({ providers: [{ provide: ActivatedRoute, useValue: seriesRoute() }, { provide: ReminderStore, useValue: fixtureStore('paused') }] })] };
export const Completed: Story = { name: 'Completed series', decorators: [applicationConfig({ providers: [{ provide: ActivatedRoute, useValue: seriesRoute() }, { provide: ReminderStore, useValue: fixtureStore('completed') }] })] };
export const Canceled: Story = { name: 'Canceled series', decorators: [applicationConfig({ providers: [{ provide: ActivatedRoute, useValue: seriesRoute() }, { provide: ReminderStore, useValue: fixtureStore('canceled') }] })] };
export const Dark: Story = { name: 'Recurring detail theme matrix' };
export const ErrorRecovery: Story = { name: 'Unknown outcome recovery', decorators: [applicationConfig({ providers: [{ provide: ReminderStore, useValue: { ...fixtureStore(), notice: signal('The command outcome is unknown. Refresh this series before trying again.'), errorStatus: signal(null) } }] })] };
export const Conflict: Story = { name: 'Version conflict', decorators: [applicationConfig({ providers: [{ provide: ReminderStore, useValue: { ...fixtureStore(), notice: signal('This recurring reminder changed elsewhere. Refresh before trying again.'), errorStatus: signal(409) } }] })] };
export const Offline: Story = { name: 'Offline draft preserved', decorators: [applicationConfig({ providers: [{ provide: ReminderStore, useValue: { ...fixtureStore(), notice: signal('You appear to be offline. Your draft is still here; reconnect before submitting again.'), errorStatus: signal(0) } }] })] };
export const UnknownOutcome: Story = { name: 'Unknown outcome requires reconciliation', decorators: [applicationConfig({ providers: [{ provide: ReminderStore, useValue: { ...fixtureStore(), notice: signal('The command outcome is unknown. Refresh this series to reconcile before trying again.'), errorStatus: signal(null), seriesMutationOutcome: signal('unconfirmed') } }] })] };
