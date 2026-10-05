import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

export type StatusTone = 'info' | 'success' | 'warning' | 'danger';

@Component({
  selector: 'aura-status-message',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<div class="status" [class]="'status status-' + tone" role="status"><span aria-hidden="true">{{ icon }}</span><span><ng-content /></span></div>`,
  styles: [`
    .status { align-items: flex-start; border: 1px solid var(--control-border); border-radius: .75rem; display: flex; gap: .65rem; line-height: 1.45; padding: .75rem .9rem; font-size: .875rem; }
    .status-info { background: var(--info-surface); color: var(--info-text); }
    .status-success { background: var(--success-surface); color: var(--success-text); }
    .status-warning { background: var(--warning-surface); color: var(--warning-text); }
    .status-danger { background: var(--danger-surface); color: var(--danger-text); }
  `],
})
export class StatusMessageComponent {
  @Input() tone: StatusTone = 'info';
  @Input() icon = 'i';
}
