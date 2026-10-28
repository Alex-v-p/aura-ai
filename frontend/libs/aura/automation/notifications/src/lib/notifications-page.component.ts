import { ChangeDetectionStrategy, Component, HostListener, Injector, afterNextRender, effect, inject, signal } from '@angular/core';
import { A11yModule } from '@angular/cdk/a11y';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { isWorkflowNotification, notificationBriefingResult, notificationReminderId, notificationSource, notificationWorkflow, type NotificationRecord } from './notification-models';
import { focusNotificationReturnTarget } from './notification-focus';
import { NotificationStore } from './notification-store';
import { ReminderStore } from '@aura/aura/automation/reminders';

type ReturnFocusState = { readonly returnFocusRequested?: boolean; readonly returnFocusId?: string | null };
function boundedFocusId(value: unknown): string | null { return typeof value === 'string' && value.startsWith('notification-') && value.length <= 160 && /^[A-Za-z0-9_-]+$/.test(value) ? value : null; }
@Component({ selector: 'aura-notifications-page', standalone: true, imports: [A11yModule, RouterLink], changeDetection: ChangeDetectionStrategy.OnPush, templateUrl: './notifications-page.component.html', styleUrl: './notifications-page.component.css' })
export class NotificationsPageComponent {
  readonly store = inject(NotificationStore);
  readonly reminders = inject(ReminderStore);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly injector = inject(Injector);
  private readonly returnFocusState = this.readReturnFocusState();
  readonly selectedId = signal<string | null>(null);

  constructor() {
    effect(() => { if (this.store.resetEpoch() > 0) { this.selectedId.set(null); this.reminders.clearDetail(); } });
    void this.store.start();
    this.route.paramMap.subscribe((params) => { const id = params.get('id'); this.selectedId.set(id); if (id) void this.openRoute(id); else { this.store.clearDetail(); this.reminders.clearDetail(); if (this.returnFocusState.returnFocusRequested) this.scheduleReturnFocus(); } });
  }
  inspect(notification: NotificationRecord, event?: Event): void { const returnFocusId = boundedFocusId((event?.currentTarget as HTMLElement | null)?.id); void this.router.navigate(['/notifications', notification.id], { state: { returnFocusRequested: true, returnFocusId } satisfies ReturnFocusState }); }
  closeDetail(): void { this.store.clearDetail(); this.reminders.clearDetail(); this.selectedId.set(null); void this.router.navigate(['/notifications'], { state: { ...this.returnFocusState, returnFocusRequested: true } satisfies ReturnFocusState }); }
  retryDetail(): void { const id = this.selectedId(); if (id) void this.store.inspect(id).then((notification) => { const reminderId = notification && notificationReminderId(notification); if (reminderId) void this.reminders.inspect(reminderId); }); }
  async acknowledge(notification: NotificationRecord): Promise<void> { const detailOpen = this.selectedId() === notification.id; if (await this.store.acknowledge(notification) && !detailOpen) this.scheduleHeadingFocus(); }
  async completeReminder(notification: NotificationRecord): Promise<void> { const reminderId = notificationReminderId(notification); if (!reminderId) return; const reminder = this.reminders.selected()?.id === reminderId ? this.reminders.selected() : await this.reminders.inspect(reminderId); if (!reminder || reminder.status === 'completed' || reminder.status === 'canceled') return; if (await this.reminders.complete(reminder)) await this.store.refresh(); }
  openReminder(notification: NotificationRecord): void { const reminderId = notificationReminderId(notification); if (reminderId) void this.router.navigate(['/reminders', reminderId]); }
  content(notification: NotificationRecord): string { const source = notificationSource(notification); if (source?.kind === 'briefing') return 'Your daily briefing is ready.'; if (source?.kind === 'workflow') return 'A workflow update is available.'; const reminder = this.reminders.selected(); return source?.kind === 'reminder' && reminder?.id === source.reminderId ? reminder.content : 'A reminder is due.'; }
  isWorkflow(notification: NotificationRecord): boolean { return isWorkflowNotification(notification); }
  workflowSource(notification: NotificationRecord): ReturnType<typeof notificationWorkflow> { return notificationWorkflow(notification); }
  workflowRoute(notification: NotificationRecord): ReadonlyArray<string> { const source = notificationWorkflow(notification); return source ? ['/workflows', source.workflowId, 'instances', source.workflowInstanceId] : ['/workflows']; }
  briefingResult(notification: NotificationRecord): ReturnType<typeof notificationBriefingResult> { return notificationBriefingResult(notification); }
  briefingRoute(notification: NotificationRecord): ReadonlyArray<string> { const source = notificationBriefingResult(notification); return source ? ['/briefings', source.briefingScheduleId, 'occurrences', source.briefingOccurrenceId] : ['/briefings']; }
  formatDate(value: string | null | undefined): string { return value ? new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' }).format(new Date(value)) : 'Unknown time'; }
  isAcknowledged(notification: NotificationRecord): boolean { return notification.status === 'acknowledged'; }
  @HostListener('document:keydown.escape') onEscape(): void { if (this.selectedId()) this.closeDetail(); }
  private async openRoute(id: string): Promise<void> { const notification = await this.store.inspect(id); const reminderId = notification && notificationReminderId(notification); if (reminderId) await this.reminders.inspect(reminderId); }
  private scheduleHeadingFocus(): void { afterNextRender(() => focusNotificationReturnTarget(null), { injector: this.injector }); }
  private scheduleReturnFocus(): void { afterNextRender(() => focusNotificationReturnTarget(this.returnFocusState.returnFocusId), { injector: this.injector }); }
  private readReturnFocusState(): ReturnFocusState { const state = this.router.getCurrentNavigation()?.extras.state ?? (typeof history !== 'undefined' ? history.state : null); return { returnFocusRequested: state?.['returnFocusRequested'] === true, returnFocusId: boundedFocusId(state?.['returnFocusId']) }; }
}
