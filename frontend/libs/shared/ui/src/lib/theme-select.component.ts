import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';

export type ThemePreference = 'system' | 'light' | 'dark';

@Component({
  selector: 'aura-theme-select',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <fieldset class="theme-select">
      <legend>Theme</legend>
      @for (option of options; track option.value) {
        <label class="theme-option" [class.is-selected]="value === option.value">
          <input type="radio" name="theme" [value]="option.value" [checked]="value === option.value" (change)="preferenceChange.emit(option.value)" />
          <span aria-hidden="true">{{ option.icon }}</span><span>{{ option.label }}</span>
        </label>
      }
    </fieldset>
  `,
  styles: [`
    :host { display: block; }
    .theme-select { border: 0; margin: 0; padding: 0; display: grid; gap: .25rem; }
    legend { color: var(--text-muted); font-size: .7rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; margin-bottom: .35rem; }
    .theme-option { align-items: center; border-radius: .5rem; color: var(--text-muted); cursor: pointer; display: flex; gap: .55rem; padding: .45rem .5rem; font-size: .8rem; transition: background .15s, color .15s; }
    .theme-option:hover, .theme-option.is-selected { background: var(--surface-subtle); color: var(--text); }
    input { accent-color: var(--accent); margin: 0; }
    @media (max-width: 1279px) and (min-width: 721px) {
      :host-context(.sidebar) .theme-select { justify-items: center; }
      :host-context(.sidebar) legend { height: 1px; margin: -1px; overflow: hidden; position: absolute; width: 1px; clip: rect(0,0,0,0); }
      :host-context(.sidebar) .theme-option { justify-content: center; padding: .45rem; }
      :host-context(.sidebar) .theme-option span:not([aria-hidden]) { height: 1px; margin: -1px; overflow: hidden; position: absolute; width: 1px; clip: rect(0,0,0,0); }
    }
  `],
})
export class ThemeSelectComponent {
  @Input({ required: true }) value: ThemePreference = 'system';
  @Output() readonly preferenceChange = new EventEmitter<ThemePreference>();
  readonly options: ReadonlyArray<{ value: ThemePreference; label: string; icon: string }> = [
    { value: 'system', label: 'System', icon: '◐' },
    { value: 'light', label: 'Light', icon: '☼' },
    { value: 'dark', label: 'Dark', icon: '◑' },
  ];
}
