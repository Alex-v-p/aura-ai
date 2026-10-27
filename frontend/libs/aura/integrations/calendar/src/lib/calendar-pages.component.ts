import { ChangeDetectionStrategy, Component, Injector, OnDestroy, afterNextRender, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { CalendarStore } from './calendar-store';
import type { CalendarConnectionStatus, SelectableCalendar } from './calendar-models';

@Component({
  selector: 'aura-integrations-page',
  standalone: true,
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './integrations-page.component.html',
  styleUrl: './calendar-pages.component.css',
})
export class IntegrationsPageComponent {
  readonly store = inject(CalendarStore);
  constructor() { void this.store.load(); }
  statusLabel(status: CalendarConnectionStatus): string { return status.replaceAll('_', ' '); }
  connectionRecoveryGuidance(action: string): string { return ({ connect: 'Connect Calendar to restore read access.', complete_connection: 'Complete the pending connection step.', retry: 'Retry the connection check when the provider is available.', reauthorize: 'Reauthorize Calendar to restore read access.', disconnect: 'Disconnect Calendar and try again.', wait: 'Wait briefly and retry.', contact_administrator: 'Contact your administrator for calendar access.' } as Record<string, string>)[action] ?? 'Follow the available connection action.'; }
}

@Component({
  selector: 'aura-calendar-management-page',
  standalone: true,
  imports: [RouterLink],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './calendar-management-page.component.html',
  styleUrl: './calendar-management-page.component.css',
})
export class CalendarManagementPageComponent implements OnDestroy {
  readonly store = inject(CalendarStore);
  private readonly injector = inject(Injector);
  readonly confirmingDisconnect = signal(false);
  constructor() { void this.initialize(); }
  ngOnDestroy(): void { this.store.clearPrivateContent(); }
  statusLabel(status: CalendarConnectionStatus | null): string { return (status ?? 'disconnected').replaceAll('_', ' '); }
  connectionRecoveryGuidance(action: string): string {
    return ({
      connect: 'Connect Calendar again to restore read access.',
      complete_connection: 'Complete the pending connection step to restore read access.',
      retry: 'Retry the connection check when the provider is available.',
      reauthorize: 'Reauthorize Calendar to restore read access.',
      disconnect: 'Disconnect Calendar and try again if access cannot be restored.',
      wait: 'Wait briefly and retry while the provider recovers.',
      contact_administrator: 'Contact your administrator for calendar access.',
    } as Record<string, string>)[action] ?? 'Follow the available connection action to recover calendar access.';
  }
  busy(): boolean { return this.store.requestState() === 'loading' || this.store.requestState() === 'saving'; }
  private afterRender(callback: () => void): void { afterNextRender(callback, { injector: this.injector }); }
  private settleFocus(preferredId: string | null = null): void {
    if (typeof document === 'undefined') return;
    if (preferredId && !this.store.error()) {
      const preferred = document.getElementById(preferredId);
      if (preferred) { preferred.focus(); return; }
    }
    const action = document.querySelector<HTMLElement>('.recovery button');
    if (action) { action.focus(); return; }
    const error = this.store.error();
    const needsRecoveryFocus = error?.retryable === true && error.recovery !== 'wait' && error.recovery !== 'connect';
    if (needsRecoveryFocus) {
      this.afterRender(() => { const recoveryAction = document.querySelector<HTMLElement>('.recovery button'); (recoveryAction ?? document.getElementById('calendar-status-heading'))?.focus(); });
      return;
    }
    document.getElementById('calendar-status-heading')?.focus();
  }
  private async initialize(): Promise<void> { await this.store.load(); if (this.store.connection()?.status === 'connecting' && this.store.canComplete()) { await this.store.completeConnection(); this.settleFocus(); } }
  isSelected(calendar: SelectableCalendar): boolean { return this.store.selectedCalendarIds().includes(calendar.id); }
  toggleCalendar(calendar: SelectableCalendar): void { const current = this.store.selectedCalendarIds(); this.store.setCalendarSelection(current.includes(calendar.id) ? current.filter((id) => id !== calendar.id) : [...current, calendar.id]); }
  async saveSelection(): Promise<void> { await this.store.replaceSelection(); this.settleFocus(); }
  async connect(): Promise<void> { if (await this.store.connect()) { if (!this.store.openAuthorization()) this.settleFocus(); } else this.settleFocus(); }
  async reauthorize(): Promise<void> { if (await this.store.reauthorize()) { if (!this.store.openAuthorization()) this.settleFocus(); } else this.settleFocus(); }
  requestDisconnectConfirmation(): void { this.confirmingDisconnect.set(true); this.afterRender(() => document.getElementById('confirm-disconnect')?.focus()); }
  cancelDisconnect(): void { this.confirmingDisconnect.set(false); this.afterRender(() => document.getElementById('disconnect-calendar')?.focus()); }
  async disconnect(): Promise<void> { this.confirmingDisconnect.set(false); await this.store.disconnect(); this.settleFocus(); }
  async complete(): Promise<void> { await this.store.completeConnection(); this.settleFocus(); }
  retry(): void { void this.store.load().finally(() => this.settleFocus()); }
  retryAgenda(): void { void this.store.loadAgenda().finally(() => this.settleFocus(this.store.error() ? null : 'refresh-agenda')); }
}
