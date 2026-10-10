import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import type { Reminder } from '@aura/aura-api-client';
import { ReminderStore } from './reminder-store';

function localDateTimeValue(date = new Date()): string {
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function toExplicitUtc(value: string): string | null {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

@Component({
  selector: 'aura-reminders-page',
  standalone: true,
  imports: [FormsModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './reminders-page.component.html',
  styleUrl: './reminders-page.component.css',
})
export class RemindersPageComponent {
  readonly store = inject(ReminderStore);
  readonly createOpen = signal(false);
  readonly selectedId = signal<string | null>(null);
  content = '';
  dueAt = localDateTimeValue(new Date(Date.now() + 60 * 60 * 1000));
  readonly minDueAt = localDateTimeValue();

  constructor() { void this.store.load(); }

  beginCreate(): void { this.createOpen.set(true); this.content = ''; this.dueAt = localDateTimeValue(new Date(Date.now() + 60 * 60 * 1000)); }
  cancelCreate(): void { this.createOpen.set(false); }

  async saveCreate(): Promise<void> {
    const content = this.content.trim();
    const dueAt = toExplicitUtc(this.dueAt);
    if (!content || content.length > 2000 || !dueAt || new Date(dueAt).getTime() <= Date.now()) {
      this.store.notice.set('Enter a message and a future date and time.');
      return;
    }
    if (await this.store.create(content, dueAt)) { this.createOpen.set(false); this.content = ''; }
  }

  async inspect(reminder: Reminder): Promise<void> { this.selectedId.set(reminder.id); await this.store.inspect(reminder.id); }
  closeDetail(): void { this.selectedId.set(null); this.store.selected.set(null); }
  async complete(reminder: Reminder): Promise<void> { await this.store.complete(reminder); }
  async cancel(reminder: Reminder): Promise<void> { await this.store.cancel(reminder); }
  formatDate(value: string): string { return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)); }
  statusLabel(status: Reminder['status']): string { return status === 'canceled' ? 'Canceled' : status[0].toUpperCase() + status.slice(1); }
  isTerminal(reminder: Reminder): boolean { return reminder.status === 'completed' || reminder.status === 'canceled'; }
}
