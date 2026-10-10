import { applicationConfig } from '@storybook/angular';
import type { Meta, StoryObj } from '@storybook/angular';
import { computed, signal } from '@angular/core';
import type { Notification } from '@aura/aura-api-client';
import { NotificationsPageComponent, NotificationStore } from '@aura/aura/automation/notifications';
import { ReminderStore } from '@aura/aura/automation/reminders';

const delivered: Notification = { id: 'notification-story', reminderId: 'reminder-story', status: 'delivered', version: 1, createdAt: '2026-10-08T09:00:00.000Z', updatedAt: '2026-10-08T09:00:00.000Z', deliveredAt: '2026-10-08T09:00:00.000Z', acknowledgedAt: null };
const acknowledged: Notification = { ...delivered, id: 'notification-acknowledged', status: 'acknowledged', version: 2, acknowledgedAt: '2026-10-08T09:05:00.000Z' };

function fixtureStore(items: ReadonlyArray<Notification> = [delivered, acknowledged]): NotificationStore {
  const collection = signal({ items, nextCursor: null, unacknowledgedCount: items.filter((item) => item.status === 'delivered').length });
  return {
    collection, selected: signal(null), loading: signal(false), saving: signal(false), notice: signal(null), errorStatus: signal(null), connectionState: signal<'offline' | 'connecting' | 'live'>('live'), filter: signal<'all' | 'delivered' | 'acknowledged'>('all'), unreadCount: computed(() => collection().unacknowledgedCount),
    start: async () => undefined, stop: () => undefined, refresh: async () => undefined, loadMore: async () => undefined, inspect: async () => null, acknowledge: async () => true, setFilter: async () => undefined, clearNotice: () => undefined,
  } as unknown as NotificationStore;
}

function fixtureReminders(): ReminderStore {
  return { inspect: async () => null, complete: async () => true } as unknown as ReminderStore;
}

const meta: Meta<NotificationsPageComponent> = { title: 'Aura/Notifications centre', component: NotificationsPageComponent, parameters: { layout: 'fullscreen' }, decorators: [applicationConfig({ providers: [{ provide: NotificationStore, useValue: fixtureStore() }, { provide: ReminderStore, useValue: fixtureReminders() }] })] };
export default meta;
type Story = StoryObj<NotificationsPageComponent>;
export const Inbox: Story = { name: 'Unread and acknowledged notifications' };
export const Empty: Story = { name: 'Empty inbox', decorators: [applicationConfig({ providers: [{ provide: NotificationStore, useValue: fixtureStore([]) }, { provide: ReminderStore, useValue: fixtureReminders() }] })] };
export const Mobile: Story = { name: 'Mobile responsive inbox', parameters: { viewport: { defaultViewport: 'mobile1' } } };
