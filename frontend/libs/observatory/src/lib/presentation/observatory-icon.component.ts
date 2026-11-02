import { ChangeDetectionStrategy, Component, Input } from '@angular/core';

/** Closed, dependency-free icon vocabulary owned by Observatory. */
export type ObservatoryIconName =
  | 'overview' | 'runs' | 'trace' | 'components' | 'analytics' | 'compare'
  | 'evaluation' | 'experiments' | 'operations' | 'retention' | 'menu' | 'next'
  | 'success' | 'warning' | 'danger' | 'document' | 'image' | 'json';

const ICON_PATHS: Record<ObservatoryIconName, string> = {
  overview: 'M4 12a8 8 0 1 0 16 0 8 8 0 1 0-16 0Zm8-5v5l3 2',
  runs: 'M5 19V9m7 10V5m7 14v-7',
  trace: 'M6 4v16m0-12h6a3 3 0 0 1 0 6H6m6-3h4a3 3 0 0 1 0 6h-4',
  components: 'M12 3 4 7.5 12 12l8-4.5L12 3Zm-8 9 8 4.5 8-4.5M4 16.5 12 21l8-4.5',
  analytics: 'M4 19V5m0 14h16M8 16v-4m4 4V8m4 8v-6',
  compare: 'M7 7h10m0 0-3-3m3 3-3 3M17 17H7m0 0 3-3m-3 3 3 3',
  evaluation: 'm5 12 4 4L19 6',
  experiments: 'M9 3h6m-5 0v5l-4.5 8.5A2 2 0 0 0 7.3 20h9.4a2 2 0 0 0 1.8-3.5L14 8V3M7 15h10',
  operations: 'M12 8v4l2.5 1.5M20 12a8 8 0 1 1-2.34-5.66M20 4v5h-5',
  retention: 'M6 7h12m-9 0v10m6-10v10M8 7l1-3h6l1 3m-9 0 .7 13h8.6L17 7',
  menu: 'M4 7h16M4 12h16M4 17h16',
  next: 'm9 5 7 7-7 7',
  success: 'm5 12 4 4L19 6',
  warning: 'M12 4 3 20h18L12 4Zm0 6v4m0 3h.01',
  danger: 'M6 6l12 12M18 6 6 18',
  document: 'M7 3h7l3 3v15H7V3Zm7 0v4h4',
  image: 'M4 5h16v14H4V5Zm2 11 3-3 2 2 3-4 4 5M9 9h.01',
  json: 'M9 6 6 12l3 6m6-12 3 6-3 6',
};

@Component({
  selector: 'observatory-icon',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `<svg class="observatory-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" [attr.aria-hidden]="label ? null : 'true'" [attr.role]="label ? 'img' : null" [attr.aria-label]="label || null"><path [attr.d]="path"></path></svg>`,
  styles: [`:host { display:inline-flex; flex:0 0 auto; height:1.15em; width:1.15em; } .observatory-icon { display:block; height:100%; width:100%; }`],
})
export class ObservatoryIconComponent {
  @Input() name: ObservatoryIconName = 'overview';
  @Input() label = '';
  get path(): string { return ICON_PATHS[this.name] ?? ICON_PATHS.overview; }
}
