import { A11yModule } from '@angular/cdk/a11y';
import { ChangeDetectionStrategy, Component, EventEmitter, HostListener, Input, Output } from '@angular/core';

/** Product-neutral focus-managed detail drawer. */
@Component({
  selector: 'aura-drawer',
  standalone: true,
  imports: [A11yModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (open) {
      <button class="drawer-scrim" type="button" aria-label="Close detail" (click)="closed.emit()"></button>
      <aside class="drawer" role="dialog" aria-modal="true" [attr.aria-labelledby]="headingId" cdkTrapFocus cdkTrapFocusAutoCapture>
        <header class="drawer-header"><h2 [id]="headingId">{{ title }}</h2><button class="close" type="button" aria-label="Close detail" (click)="closed.emit()">×</button></header>
        <div class="drawer-content"><ng-content /></div>
      </aside>
    }
  `,
  styles: [`
    :host { display: contents; }
    .drawer-scrim { background: rgb(18 16 18 / .35); border: 0; inset: 0; position: fixed; z-index: 40; }
    .drawer { background: var(--surface); border-left: 1px solid var(--border); box-shadow: -1rem 0 3rem rgb(0 0 0 / .14); display: flex; flex-direction: column; height: 100dvh; max-width: min(32rem, 92vw); position: fixed; right: 0; top: 0; width: 32rem; z-index: 41; }
    .drawer-header { align-items: center; border-bottom: 1px solid var(--border); display: flex; gap: 1rem; justify-content: space-between; padding: 1rem 1.25rem; }
    h2 { font-size: 1.1rem; margin: 0; }
    .close { align-items: center; background: transparent; border: 0; border-radius: .5rem; color: var(--text-muted); cursor: pointer; display: inline-flex; font-size: 1.35rem; height: 2.75rem; justify-content: center; width: 2.75rem; }
    .close:hover { background: var(--surface-subtle); color: var(--text); }
    .drawer-content { min-height: 0; overflow: auto; padding: 1.25rem; }
    @media (max-width: 720px) { .drawer { border-left: 0; max-width: none; width: 100%; } }
  `],
})
export class DrawerComponent {
  @Input() open = false;
  @Input() title = 'Details';
  @Input() headingId = `drawer-${Math.random().toString(36).slice(2, 9)}`;
  @Output() readonly closed = new EventEmitter<void>();

  @HostListener('document:keydown.escape')
  handleEscape(): void { if (this.open) this.closed.emit(); }
}
