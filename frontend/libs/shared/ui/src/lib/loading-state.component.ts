import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

@Component({
  selector: 'aura-loading-state',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="loading-state" role="status" aria-live="polite">
      <span class="loading-mark" aria-hidden="true"></span>
      <span>{{ label }}</span>
    </div>
  `,
  styles: [`
    :host { display: block; }
    .loading-state { align-items: center; color: var(--text-muted); display: flex; gap: .65rem; justify-content: center; padding: 2rem 1rem; }
    .loading-mark { animation: loading-pulse 1.2s ease-in-out infinite; background: var(--accent); border-radius: 999px; height: .65rem; width: .65rem; }
    @keyframes loading-pulse { 0%, 100% { opacity: .35; transform: scale(.8); } 50% { opacity: 1; transform: scale(1); } }
    @media (prefers-reduced-motion: reduce) { .loading-mark { animation: none; opacity: .75; } }
  `],
})
export class LoadingStateComponent {
  @Input() label = 'Loading';
}
