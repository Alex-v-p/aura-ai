import { describe, expect, it } from 'vitest';
import { TabsComponent } from './tabs.component';

describe('TabsComponent keyboard model', () => {
  it('roves across enabled tabs and emits the newly selected tab', () => {
    const tabs = new TabsComponent();
    tabs.tabs = [
      { id: 'overview', label: 'Overview' },
      { id: 'advanced', label: 'Advanced', disabled: true },
      { id: 'versions', label: 'Versions' },
    ];
    tabs.activeId = 'overview';
    let selected = '';
    tabs.activeChange.subscribe((id) => { selected = id; });

    const event = new KeyboardEvent('keydown', { key: 'ArrowRight', cancelable: true });
    tabs.handleKeydown(event);

    expect(event.defaultPrevented).toBe(true);
    expect(selected).toBe('versions');
  });

  it('supports Home and End navigation without selecting disabled tabs', () => {
    const tabs = new TabsComponent();
    tabs.tabs = [
      { id: 'overview', label: 'Overview' },
      { id: 'behavior', label: 'Behavior' },
      { id: 'versions', label: 'Versions', disabled: true },
    ];
    tabs.activeId = 'behavior';
    const selected: string[] = [];
    tabs.activeChange.subscribe((id) => selected.push(id));

    tabs.handleKeydown(new KeyboardEvent('keydown', { key: 'Home', cancelable: true }));
    tabs.activeId = 'overview';
    tabs.handleKeydown(new KeyboardEvent('keydown', { key: 'End', cancelable: true }));

    expect(selected).toEqual(['overview', 'behavior']);
  });
});
