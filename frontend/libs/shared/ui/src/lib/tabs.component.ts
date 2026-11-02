import { ChangeDetectionStrategy, Component, EventEmitter, Input, Output } from '@angular/core';

export interface AuraTab {
  readonly id: string;
  readonly label: string;
  readonly count?: number;
  readonly disabled?: boolean;
}

/** Product-neutral tabs with an explicit current selection. */
@Component({
  selector: 'aura-tabs',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <nav class="tabs" role="tablist" [attr.aria-label]="label" (keydown)="handleKeydown($event)">
      @for (tab of tabs; track tab.id) {
        <button class="tab" type="button" role="tab" [id]="tabId(tab.id)" [attr.aria-selected]="tab.id === activeId" [attr.aria-controls]="panelId + '-' + tab.id" [attr.tabindex]="tab.id === activeId ? 0 : -1" [disabled]="tab.disabled" [class.is-active]="tab.id === activeId" (click)="select(tab.id)">
          <span>{{ tab.label }}</span>@if (tab.count !== undefined) { <span class="tab-count">{{ tab.count }}</span> }
        </button>
      }
    </nav>
  `,
  styles: [`
    :host { display: block; }
    .tabs { align-items: center; border-bottom: 1px solid var(--border); display: flex; gap: .25rem; overflow-x: auto; }
    .tab { background: transparent; border: 0; border-bottom: 2px solid transparent; color: var(--text-muted); cursor: pointer; display: inline-flex; font: inherit; font-size: .875rem; gap: .4rem; min-height: 2.75rem; padding: .55rem .8rem; white-space: nowrap; }
    .tab:hover, .tab.is-active { color: var(--text); }
    .tab.is-active { border-bottom-color: var(--accent); font-weight: 650; }
    .tab:disabled { cursor: not-allowed; opacity: .55; }
    .tab-count { align-items: center; background: var(--surface-subtle); border-radius: 999px; display: inline-flex; font-size: .7rem; height: 1.25rem; justify-content: center; min-width: 1.25rem; padding: 0 .25rem; }
  `],
})
export class TabsComponent {
  @Input() tabs: ReadonlyArray<AuraTab> = [];
  @Input() activeId = '';
  @Input() label = 'Sections';
  @Input() panelId = 'tab-panel';
  @Output() readonly activeChange = new EventEmitter<string>();

  tabId(id: string): string { return `${this.panelId}-tab-${id}`; }

  select(id: string): void { if (this.tabs.some((tab) => tab.id === id && !tab.disabled)) this.activeChange.emit(id); }

  handleKeydown(event: KeyboardEvent): void {
    const enabled = this.tabs.filter((tab) => !tab.disabled);
    if (!enabled.length) return;
    const currentIndex = Math.max(0, enabled.findIndex((tab) => tab.id === this.activeId));
    let nextIndex = currentIndex;
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (currentIndex + 1) % enabled.length;
    else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (currentIndex - 1 + enabled.length) % enabled.length;
    else if (event.key === 'Home') nextIndex = 0;
    else if (event.key === 'End') nextIndex = enabled.length - 1;
    else return;
    event.preventDefault();
    const next = enabled[nextIndex];
    this.select(next.id);
    queueMicrotask(() => document.getElementById(this.tabId(next.id))?.focus());
  }
}
