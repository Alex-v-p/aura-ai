import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const template = readFileSync('libs/aura/activity/tasks/src/lib/tasks-page.component.html', 'utf8');
const styles = readFileSync('libs/aura/activity/tasks/src/lib/tasks-page.component.css', 'utf8');

describe('TasksPageComponent responsive acceptance regressions', () => {
  it('bounds filters, cards, details, and confirmation surfaces without clipping', () => {
    expect(styles).toContain('.tasks-page > *');
    expect(styles).toContain('.task-card > *');
    expect(styles).toContain('grid-template-columns:minmax(0,1fr)');
    expect(styles).toContain('overflow-wrap:anywhere');
    expect(styles).not.toContain('overflow-x:hidden');
  });

  it('keeps the shell as the only main landmark', () => {
    expect(template).not.toMatch(/<main\b/);
    expect(template).toContain('<div class="tasks-page" aria-labelledby="tasks-heading">');
    expect(template).toContain('<h1 id="tasks-heading">Task Center</h1>');
  });
});
