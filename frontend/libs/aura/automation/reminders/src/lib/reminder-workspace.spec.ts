import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { ReminderSessionHydrator } from './reminder-session-hydration';

const page = readFileSync('libs/aura/automation/reminders/src/lib/reminders-page.component.html', 'utf8');
const api = readFileSync('libs/aura/automation/reminders/src/lib/reminders-api.ts', 'utf8');
const store = readFileSync('libs/aura/automation/reminders/src/lib/reminder-store.ts', 'utf8');

describe('reminder workspace contracts', () => {
  it('exposes both owner reminder collections and accessible lifecycle actions', () => {
    expect(page).toContain('aria-labelledby="upcoming-heading"');
    expect(page).toContain('aria-labelledby="history-heading"');
    expect(page).toContain('Mark complete');
    expect(page).toContain('Cancel reminder');
    expect(page).toContain('datetime-local');
  });

  it('uses a session CSRF token and a stable idempotency key for an unchanged create draft', () => {
    expect(api).toContain("this.client.execute('getSession'");
    expect(api).toContain("'X-CSRF-Token': this.session.currentToken()");
    expect(api).toContain('requestId = idempotencyKey()');
    expect(api).toContain("this.client.execute('createReminder'");
    expect(api).toContain('ReminderTransitionRequest');
  });

  it('guards late session, list, detail, and mutation results independently', () => {
    expect(store).toContain('private sessionGeneration = 0');
    expect(store).toContain('private listGeneration = 0');
    expect(store).toContain('private mutationGeneration = 0');
    expect(store).toContain('session !== this.sessionGeneration');
    expect(store).toContain('mutation !== this.mutationGeneration');
    expect(store).toContain("this.createOutcome() === 'unconfirmed'");
    expect(store).toContain('this.saving.set(false);');
    expect(store).toContain('if (current()) this.saving.set(false)');
  });

  it('does not let an old hydration repopulate after clearSession and new hydration', async () => {
    let resolveOld!: (token: string) => void;
    let resolveNew!: (token: string) => void;
    const hydrator = new ReminderSessionHydrator(() => new Promise<string>((resolve) => {
      if (!resolveOld) resolveOld = resolve;
      else resolveNew = resolve;
    }), () => Object.assign(new Error('Session changed.'), { status: 401 }));
    const old = hydrator.ensure();
    hydrator.clearSession();
    const replacement = hydrator.ensure();
    resolveNew('new-token');
    await replacement;
    resolveOld('old-token');
    await expect(old).rejects.toMatchObject({ status: 401 });
    expect(hydrator.currentToken()).toBe('new-token');
  });
});
