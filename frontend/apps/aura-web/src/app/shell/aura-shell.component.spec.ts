import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const componentSource = readFileSync('apps/aura-web/src/app/shell/aura-shell.component.ts', 'utf8');
const templateSource = readFileSync('apps/aura-web/src/app/shell/aura-shell.component.html', 'utf8');

describe('AuraShellComponent conversation library controls', () => {
  it('keeps the connected overlay focus lifecycle explicit', () => {
    expect(componentSource).toContain('this.filterPanelOpen.set(true)');
    expect(componentSource).toContain('queueMicrotask(() => this.focusFilterPanel())');
    expect(componentSource).toContain('handleFilterOverlayAttach(): void');
    expect(componentSource).toContain('this.filterTrigger?.nativeElement.focus()');
    expect(componentSource).toContain("if (this.filterPanelOpen()) {\n      event.preventDefault();\n      this.closeFilterPanel();");
    expect(templateSource).toContain('(overlayOutsideClick)="closeFilterPanel()"');
  });

  it('resets all live library filters without closing the popup', () => {
    expect(componentSource).toContain("const next: ShellConversationFilters = { archiveState: 'active' }");
    expect(componentSource).toContain('this.filtersChange.emit(next)');
    expect(templateSource).toContain('(click)="clearFilters()"');
    expect(templateSource).toContain('Clear filters');
  });

  it('keeps the title search label accessible while the icon is decorative', () => {
    expect(templateSource).toContain('<span class="sr-only">Search conversation titles</span>');
    expect(templateSource).toContain('<span class="search-icon" aria-hidden="true">');
    expect(templateSource).toContain('<input type="search"');
  });
});
