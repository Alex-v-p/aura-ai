import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const page = readFileSync('libs/aura/automation/reminders/src/lib/reminders-page.component.html', 'utf8');
const api = readFileSync('libs/aura/automation/reminders/src/lib/reminders-api.ts', 'utf8');

describe('reminder workspace contracts', () => {
  it('exposes both owner reminder collections and accessible lifecycle actions', () => {
    expect(page).toContain('aria-labelledby="upcoming-heading"');
    expect(page).toContain('aria-labelledby="history-heading"');
    expect(page).toContain('Mark complete');
    expect(page).toContain('Cancel reminder');
    expect(page).toContain('datetime-local');
  });

  it('uses a session CSRF token and a fresh idempotency key for every mutation', () => {
    expect(api).toContain("this.client.execute('getSession'");
    expect(api).toContain("'X-CSRF-Token': this.csrf");
    expect(api).toContain("'Idempotency-Key': idempotencyKey()");
    expect(api).toContain('ReminderTransitionRequest');
  });
});
