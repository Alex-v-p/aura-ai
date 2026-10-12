import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

export type StatePanelTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger';
export type StatePanelMode = 'static' | 'status' | 'alert';

/** Product-neutral empty, error, permission, and recovery presentation. */
@Component({
  selector: 'aura-state-panel',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section class="state-panel" [class]="'state-panel state-' + tone" [attr.role]="mode === 'static' ? null : mode" [attr.aria-live]="mode === 'static' ? null : (mode === 'alert' ? 'assertive' : 'polite')" [attr.aria-atomic]="mode === 'static' ? null : 'true'" [attr.aria-labelledby]="headingId">
      <div class="state-icon" aria-hidden="true">{{ icon }}</div>
      <div class="state-copy">
        <h2 [id]="headingId">{{ title }}</h2>
        @if (message) { <p>{{ message }}</p> }
        <div class="state-actions"><ng-content /></div>
      </div>
    </section>
  `,
  styles: [`
    :host { display: block; }
    .state-panel { align-items: flex-start; background: var(--surface); border: 1px solid var(--border); border-radius: 1rem; display: flex; gap: 1rem; margin: 1rem auto; max-width: 42rem; padding: 1.25rem; }
    .state-icon { align-items: center; background: var(--surface-subtle); border-radius: 999px; color: var(--text-muted); display: flex; flex: 0 0 2.25rem; font-weight: 750; height: 2.25rem; justify-content: center; }
    .state-copy { min-width: 0; }
    h2 { color: var(--text); font-size: 1rem; margin: 0; }
    p { color: var(--text-muted); line-height: 1.5; margin: .4rem 0 0; }
    .state-actions { display: flex; flex-wrap: wrap; gap: .5rem; margin-top: .85rem; }
    .state-info { background: var(--info-surface); border-color: var(--info-text); }
    .state-success { background: var(--success-surface); border-color: var(--success-text); }
    .state-warning { background: var(--warning-surface); border-color: var(--warning-text); }
    .state-danger { background: var(--danger-surface); border-color: var(--danger-text); }
  `],
})
export class StatePanelComponent {
  @Input() title = '';
  @Input() message = '';
  @Input() icon = 'i';
  @Input() tone: StatePanelTone = 'neutral';
  @Input() mode: StatePanelMode = 'static';
  @Input() headingId = `state-panel-${Math.random().toString(36).slice(2, 9)}`;
}
