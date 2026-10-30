import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const styles = readFileSync('libs/aura/activity/tasks/src/lib/tasks-page.component.css', 'utf8');

describe('TasksPageComponent responsive acceptance regressions', () => {
  it('bounds filters, cards, details, and confirmation surfaces without clipping', () => {
    expect(styles).toContain('.tasks-page > *');
    expect(styles).toContain('.task-card > *');
    expect(styles).toContain('grid-template-columns:minmax(0,1fr)');
    expect(styles).toContain('overflow-wrap:anywhere');
    expect(styles).not.toContain('overflow-x:hidden');
  });
});
