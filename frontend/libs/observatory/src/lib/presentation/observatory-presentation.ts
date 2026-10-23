import { ChangeDetectionStrategy, Component, input } from '@angular/core';

export type ObservatoryStatusTone = 'neutral' | 'success' | 'warning' | 'danger' | 'info';

@Component({
  selector: 'observatory-status-badge',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<span class="observatory-status-badge" [class]="'tone-' + tone()" role="status"><span class="status-mark" aria-hidden="true"></span>{{ label() }}</span>`,
  styles: [`
    :host { display:inline-flex; max-width:100%; min-width:0; }
    .observatory-status-badge { align-items:center; border:1px solid currentColor; border-radius:999px; display:inline-flex; font-size:.74rem; font-weight:750; gap:.4rem; letter-spacing:.01em; line-height:1.2; max-width:100%; overflow-wrap:anywhere; padding:.32rem .58rem; }
    .status-mark { background:currentColor; border-radius:50%; block-size:.45rem; flex:0 0 .45rem; inline-size:.45rem; }
    .tone-neutral { color:var(--obs-ink-soft); }
    .tone-success { background:var(--obs-success-surface); color:var(--obs-success-text); }
    .tone-warning { background:var(--obs-warning-surface); color:var(--obs-warning-text); }
    .tone-danger { background:var(--obs-danger-surface); color:var(--obs-danger-text); }
    .tone-info { background:var(--obs-info-surface); color:var(--obs-info-text); }
  `],
})
export class ObservatoryStatusBadgeComponent {
  readonly label = input.required<string>();
  readonly tone = input<ObservatoryStatusTone>('neutral');
}

@Component({
  selector: 'observatory-section-heading',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<header class="section-heading"><div><p class="eyebrow">{{ eyebrow() }}</p><h2>{{ title() }}</h2>@if (description()) { <p class="description">{{ description() }}</p> }</div><div class="actions"><ng-content /></div></header>`,
  styles: [`
    :host { display:block; min-width:0; }
    .section-heading { align-items:flex-start; display:flex; flex-wrap:wrap; gap:1rem; justify-content:space-between; }
    .eyebrow { color:var(--obs-accent-strong); font-size:.7rem; font-weight:800; letter-spacing:.13em; margin:0 0 .35rem; text-transform:uppercase; }
    h2 { color:var(--obs-ink); font-size:clamp(1.35rem,2vw,1.75rem); letter-spacing:-.025em; line-height:1.15; margin:0; overflow-wrap:anywhere; }
    .description { color:var(--obs-ink-soft); font-size:.9rem; line-height:1.5; margin:.45rem 0 0; max-width:46rem; }
    .actions { align-items:center; display:flex; flex-wrap:wrap; gap:.5rem; }
  `],
})
export class ObservatorySectionHeadingComponent {
  readonly eyebrow = input('Observatory');
  readonly title = input.required<string>();
  readonly description = input('');
}

@Component({
  selector: 'observatory-surface',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<section class="surface"><ng-content /></section>`,
  styles: [`
    :host { display:block; min-width:0; }
    .surface { background:var(--obs-surface); border:1px solid var(--obs-line); border-radius:var(--obs-radius-md); box-shadow:var(--obs-shadow-sm); min-width:0; padding:var(--obs-space-5); }
    @media (max-width:40rem) { .surface { padding:var(--obs-space-4); } }
  `],
})
export class ObservatorySurfaceComponent {}
