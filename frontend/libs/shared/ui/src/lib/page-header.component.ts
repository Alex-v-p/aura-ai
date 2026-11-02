import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

/** Product-neutral page framing for management surfaces. */
@Component({
  selector: 'aura-page-header',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <header class="page-header">
      <div class="page-copy">
        @if (eyebrow) { <p class="eyebrow">{{ eyebrow }}</p> }
        <h1>{{ title }}</h1>
        @if (description) { <p class="description">{{ description }}</p> }
      </div>
      <div class="page-actions"><ng-content /></div>
    </header>
  `,
  styles: [`
    :host { display: block; }
    .page-header { align-items: flex-start; display: flex; gap: 1.5rem; justify-content: space-between; margin: 0 auto 1.5rem; max-width: 72rem; padding: clamp(1.5rem, 4vw, 3rem) clamp(1rem, 4vw, 3rem) 0; }
    .page-copy { min-width: 0; }
    .eyebrow { color: var(--text-muted); font-size: .75rem; font-weight: 750; letter-spacing: .1em; margin: 0 0 .45rem; text-transform: uppercase; }
    h1 { color: var(--text); font-size: clamp(1.7rem, 3vw, 2.35rem); letter-spacing: -.035em; line-height: 1.12; margin: 0; }
    .description { color: var(--text-muted); line-height: 1.55; margin: .65rem 0 0; max-width: 48rem; }
    .page-actions { align-items: center; display: flex; flex: 0 0 auto; gap: .5rem; }
    @media (max-width: 640px) { .page-header { display: grid; gap: 1rem; } .page-actions { justify-content: flex-start; } }
  `],
})
export class PageHeaderComponent {
  @Input() eyebrow = '';
  @Input() title = '';
  @Input() description = '';
}
