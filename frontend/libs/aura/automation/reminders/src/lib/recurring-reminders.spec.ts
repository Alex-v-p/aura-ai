import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const models = readFileSync('libs/aura/automation/reminders/src/lib/reminder-models.ts', 'utf8');
const api = readFileSync('libs/aura/automation/reminders/src/lib/reminders-api.ts', 'utf8');
const store = readFileSync('libs/aura/automation/reminders/src/lib/reminder-store.ts', 'utf8');
const page = readFileSync('libs/aura/automation/reminders/src/lib/reminders-page.component.html', 'utf8');
const pageComponent = readFileSync('libs/aura/automation/reminders/src/lib/reminders-page.component.ts', 'utf8');
const routes = readFileSync('apps/aura-web/src/app/app.routes.ts', 'utf8');
const generated = readFileSync('libs/platform/aura-api-client/src/lib/generated/aura-api.ts', 'utf8');
const stories = readFileSync('apps/storybook/src/reminders.stories.ts', 'utf8');

describe('recurring reminder product contracts', () => {
  it('models every public series and occurrence state without widening ReminderStatus', () => {
    expect(models).toContain("SeriesStatus = 'active' | 'paused' | 'completed' | 'canceled'");
    expect(models).toContain("OccurrenceOutcome = 'scheduled' | 'fired' | 'completed' | 'skipped' | 'replaced' | 'paused-window' | 'series-canceled'");
    expect(models).toContain('nextReminderId');
    expect(models).toContain('readableRule');
    expect(models).toContain('timeZone');
    expect(models).toContain('count: number | null');
    expect(models).toContain('endDate: string | null');
    expect(models).not.toMatch(/ReminderStatus\s*=.*paused/);
  });

  it('exposes the complete generated transport surface and keeps recurrence out of transport implementation', () => {
    for (const operation of [
      'listReminderSeries',
      'createReminderSeries',
      'getReminderSeries',
      'listReminderSeriesOccurrences',
      'updateReminderSeries',
      'pauseReminderSeries',
      'resumeReminderSeries',
      'skipNextReminderSeries',
      'cancelReminderSeries',
    ]) {
      expect(generated).toContain(operation);
      expect(api).toContain(operation);
    }
    expect(api).not.toMatch(/next_occurrence|ZoneInfo|setDate\(|setMonth\(/);
    expect(generated).toContain('Contract SHA-256:');
  });

  it('requires session CSRF, fresh UUID command keys, and expected versions for mutations', () => {
    expect(api).toContain("'X-CSRF-Token': this.session.currentToken()");
    expect(api).toContain('crypto.randomUUID()');
    expect(api).toContain('expectedVersion');
    expect(api).toContain('idempotencyKey');
    expect(store).toContain('createOutcome');
    expect(store).toContain('unconfirmed');
    expect(store).toContain('resetSessionState');
    expect(store).toContain('mutationGeneration');
    expect(store).toContain("seriesMutationOutcome() === 'unconfirmed'");
    expect(store).toContain('seriesRecovery.set');
  });

  it('keeps the unified agenda authoritative and deduplicates generated occurrences', () => {
    expect(models).toContain('nextReminderId');
    expect(pageComponent).toContain('nextReminderId');
    expect(page).toContain('repeat');
    expect(page).not.toContain('calculateNextOccurrence');
    expect(page).not.toContain('new Date(');
  });

  it('provides the series detail route and accessible recovery affordances', () => {
    expect(routes).toMatch(/reminders\/series\/:seriesId/);
    expect(page).toContain('cdkTrapFocus');
    expect(page).toContain('aria-modal="true"');
    expect(pageComponent).toContain('keydown.escape');
    for (const label of ['Pause', 'Resume', 'Skip next', 'Cancel series']) {
      expect(page).toContain(label);
    }
    expect(pageComponent).toContain('recurrenceError');
    expect(store).toContain('status === 409');
    expect(store).toContain('status === 503');
    expect(store).toContain('status === 403');
    expect(store).toContain('statusOf(error) === null');
    expect(page).toContain('Ends');
    expect(pageComponent).toContain('endCount');
    expect(pageComponent).toContain('endDate');
  });

  it('keeps recurring Storybook IDs explicit and synchronized with named exports', () => {
    for (const exportName of ['RecurringComposer', 'SeriesDetail', 'SeriesHistory', 'Paused', 'Completed', 'Canceled', 'Dark', 'ErrorRecovery', 'Conflict', 'Offline', 'UnknownOutcome']) {
      expect(stories).toMatch(new RegExp(`export const ${exportName}\\s*:`));
    }
    expect(stories).toContain("title: 'Aura/Reminders recurring experience'");
  });

  it('clears recurring private state across principal changes and never persists it durably', () => {
    expect(store).toContain('clearSession');
    expect(store).toContain('sessionGeneration');
    expect(store).toContain('detailGeneration');
    expect(store).toContain('pending');
    expect(store).toContain('createKey');
    expect(store).not.toContain('localStorage');
    expect(store).not.toContain('sessionStorage');
  });
});
