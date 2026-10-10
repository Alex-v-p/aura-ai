import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import type { NotificationRecord } from './notification-models';
import { NotificationStore } from './notification-store';
import { ReminderStore } from '@aura/aura/automation/reminders';

@Component({
  selector: 'aura-notifications-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './notifications-page.component.html',
  styleUrl: './notifications-page.component.css',
})
export class NotificationsPageComponent {
  readonly store = inject(NotificationStore);
  readonly reminders = inject(ReminderStore);
  readonly selectedId = signal<string | null>(null);

  constructor() { void this.store.start(); }

  async inspect(notification: NotificationRecord): Promise<void> {
    this.selectedId.set(notification.id);
    await this.store.inspect(notification.id);
    await this.reminders.inspect(notification.reminderId);
  }

  closeDetail(): void { this.selectedId.set(null); this.store.selected.set(null); }
  async acknowledge(notification: NotificationRecord): Promise<void> { await this.store.acknowledge(notification); }

  async completeReminder(notification: NotificationRecord): Promise<void> {
    const reminderId = notification.reminderId;
    const reminder = await this.reminders.inspect(reminderId);
    if (!reminder || reminder.status === 'completed' || reminder.status === 'canceled') return;
    if (await this.reminders.complete(reminder)) await this.store.refresh();
  }

  content(notification: NotificationRecord): string {
    const reminder = this.reminders.selected();
    return reminder?.id === notification.reminderId ? reminder.content : 'A reminder is due.';
  }
  formatDate(value: string | null | undefined): string { return value ? new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : 'Unknown time'; }
  isAcknowledged(notification: NotificationRecord): boolean { return notification.status === 'acknowledged'; }
  canComplete(notification: NotificationRecord): boolean { return Boolean(notification.reminderId); }
}
