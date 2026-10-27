import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const storybookBaseUrl = (
  process.env.AURA_WEB_E2E_STORYBOOK_URL ??
  `http://127.0.0.1:${process.env.AURA_WEB_E2E_STORYBOOK_PORT ?? '6300'}`
).replace(/\/$/, '');
const stories = [
  'empty',
  'loading',
  'working',
  'tool-activity',
  'interrupted',
  'recoverable-error',
  'completed',
  'archived-recovery',
  'run-inspector',
  'attachment-uploading',
  'attachment-processing',
  'attachment-failed',
  'attachment-ready',
  'attachment-tombstone',
  'attachment-model-vision',
] as const;
const themes = ['light', 'dark'] as const;
const memoryStories = [
  'records',
  'selected-record-detail',
  'create-memory',
  'first-run-setup',
  'review-queue',
  'empty-records',
  'filtered-empty-records',
  'configured-settings',
  'reindex-settings',
  'settings-error',
  'mobile-dark-records',
  'mobile-detail',
  'agent-policy-automatic',
  'agent-policy-off',
] as const;
const managementStories = [
  'agent-library',
  'agent-builder',
  'persona-library',
  'persona-builder',
  'persona-detail',
] as const;
const documentStories = [
  'empty',
  'ready',
  'failed',
  'dark-detail',
  'loading',
  'duplicate-upload',
  'unavailable',
  'ready-detail-preview',
  'deletion-confirmation',
] as const;
const reminderStories = [
  'workspace',
  'history',
  'composer',
  'detail',
  'loading',
  'empty',
  'recovery',
  'mobile',
] as const;
// These IDs mirror the named exports in apps/storybook/src/reminders.stories.ts.
// Keep this list explicit: a typo here silently turns accessibility coverage
// into a 404 page instead of exercising the recurring-reminder states.
const recurringReminderStories = [
  'recurring-composer',
  'series-detail',
  'series-history',
  'paused',
  'completed',
  'canceled',
  'dark',
  'error-recovery',
  'conflict',
  'offline',
  'unknown-outcome',
] as const;
const notificationStories = [
  'inbox',
  'history',
  'detail',
  'loading',
  'empty',
  'recovery',
  'mobile',
] as const;
const approvalStories = [
  'pending',
  'history',
  'detail',
  'terminal-detail',
  'loading',
  'empty',
  'authentication-error',
  'permission-error',
  'conflict',
  'unknown-outcome',
  'unavailable-stream',
  'recovery',
  'mobile',
] as const;
const todayStories = [
  'action-center',
  'calm-empty',
  'loading',
  'partial-failure',
  'permission-denied',
  'offline',
  'reconnecting',
  'unauthenticated',
  'calendar-connected',
  'calendar-partial',
  'calendar-stale',
  'calendar-rate-limited',
  'calendar-permission-denied',
  'calendar-offline',
] as const;
const calendarStories = [
  'connected',
  'loading',
  'disconnected',
  'reauthorization-required',
  'revocation-uncertain',
  'partial-read',
  'stale-snapshot',
  'rate-limited',
  'empty-selection',
  'long-titles',
] as const;
const applyStoryTheme = (theme: 'light' | 'dark'): void => {
  document.documentElement.dataset['theme'] = theme;
  document.documentElement.style.colorScheme = theme;
  document.body.style.background = 'var(--canvas)';
  document.body.style.color = 'var(--text)';
};
const applyAndSettleStoryTheme = async (
  page: import('@playwright/test').Page,
  theme: 'light' | 'dark',
): Promise<void> => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.evaluate(applyStoryTheme, theme);
  await page.evaluate(() => document.fonts?.ready);
  await page.waitForFunction(() => {
    const root = document.documentElement;
    const styles = getComputedStyle(root);
    return (
      styles.getPropertyValue('--canvas').trim() !== '' &&
      styles.getPropertyValue('--text').trim() !== ''
    );
  });
  const motion = await page.locator('body').evaluate((element) => ({
    reduced: matchMedia('(prefers-reduced-motion: reduce)').matches,
    scrollBehavior: getComputedStyle(element).scrollBehavior,
    transitionDuration: getComputedStyle(element).transitionDuration,
  }));
  expect(motion.reduced).toBe(true);
  expect(motion.scrollBehavior).toBe('auto');
  expect(Number.parseFloat(motion.transitionDuration.replace('s', ''))).toBeLessThan(0.001);
  await page.waitForTimeout(50);
};
const watchRuntimeErrors = (page: import('@playwright/test').Page): (() => void) => {
  const errors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(error.message));
  return () => expect(errors).toEqual([]);
};

for (const story of todayStories) {
  for (const theme of themes) {
    test(`Today Storybook ${story} is accessible in ${theme} theme`, async ({ page }, testInfo) => {
      test.skip(
        testInfo.project.name !== 'desktop',
        'Today Storybook axe coverage runs once in the desktop project.',
      );
      const assertNoRuntimeErrors = watchRuntimeErrors(page);
      await page.goto(
        `${storybookBaseUrl}/iframe.html?id=aura-today-action-center--${story}&viewMode=story`,
      );
      // A full matrix can cold-start Storybook's preview bundle. Keep this
      // readiness wait bounded, while leaving the state assertions, Axe scan,
      // and runtime-error checks unchanged.
      await expect(page.locator('.daily-action').first()).toBeVisible({ timeout: 30_000 });
      await applyAndSettleStoryTheme(page, theme);
      if (story === 'action-center') {
        await expect(
          page.getByRole('heading', { name: 'Needs attention', exact: true }),
        ).toBeVisible();
        await expect(page.getByText('Bring the library books back', { exact: true })).toBeVisible();
      } else if (story === 'calm-empty') {
        await expect(page.getByText('All clear for now.', { exact: true })).toBeVisible();
      } else if (story === 'loading') {
        await expect(
          page
            .getByRole('status')
            .filter({ hasText: /Loading/ })
            .first(),
        ).toBeVisible();
        await expect(page.getByText('All clear for now.', { exact: true })).toHaveCount(0);
      } else if (story === 'partial-failure') {
        await expect(
          page.locator('.section-status').filter({ hasText: /notifications/i }),
        ).toBeVisible();
      } else if (story === 'permission-denied') {
        await expect(
          page.getByText(/Today access is unavailable|permission/i).first(),
        ).toBeVisible();
        await expect(page.getByText('All clear for now.', { exact: true })).toHaveCount(0);
      } else if (story === 'offline') {
        await expect(page.getByText(/offline|unavailable/i).first()).toBeVisible();
      } else if (story === 'reconnecting') {
        await expect(page.getByText(/out of date|reconnect/i).first()).toBeVisible();
      } else if (story === 'calendar-connected') {
        await expect(page.getByRole('heading', { name: 'Calendar agenda' })).toBeVisible();
        await expect(page.getByText('Planning day', { exact: true })).toBeVisible();
        await expect(page.getByText(/America\/New_York.*Europe\/Brussels/)).toBeVisible();
      } else if (story.startsWith('calendar-')) {
        await expect(page.getByRole('heading', { name: 'Calendar agenda' })).toBeVisible();
        await expect(page.getByText(/calendar|offline|permission|rate-limited|snapshot/i).first()).toBeVisible();
      } else {
        await expect(page.getByText(/Sign in to see your private Today view/i)).toBeVisible();
        await expect(page.getByText('All clear for now.', { exact: true })).toHaveCount(0);
      }
      const results = await new AxeBuilder({ page }).include('.daily-action').analyze();
      expect(results.violations).toEqual([]);
      assertNoRuntimeErrors();
    });
  }
}

for (const story of calendarStories) {
  for (const theme of themes) {
    test(`Calendar Storybook ${story} is accessible in ${theme} theme`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== 'desktop', 'Calendar Storybook axe coverage runs once in the desktop project.');
      const assertNoRuntimeErrors = watchRuntimeErrors(page);
      await page.goto(`${storybookBaseUrl}/iframe.html?id=aura-calendar-planning--${story}&viewMode=story`);
      await expect(page.locator('.calendar-page').first()).toBeVisible();
      await applyAndSettleStoryTheme(page, theme);
      if (story === 'connected') {
        await expect(page.getByRole('heading', { name: 'Readable calendars' })).toBeVisible();
        await expect(page.getByText('Planning day', { exact: true })).toBeVisible();
        await expect(page.getByText('Focus block', { exact: true })).toBeVisible();
      }
      if (story === 'partial-read') {
        await expect(page.locator('.calendar-page .notice')).toContainText(/Some calendars could not be read/i);
      }
      if (story === 'stale-snapshot') await expect(page.getByRole('alert')).toBeVisible();
      if (story === 'empty-selection') {
        await expect(page.locator('.calendar-page .notice')).toContainText(/Empty selection is allowed/i);
      }
      if (story === 'long-titles') {
        await expect(page.getByText(/deliberately long multi-day planning title/i)).toBeVisible();
        await page.evaluate(() => { document.documentElement.style.zoom = '2'; });
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
      }
      const results = await new AxeBuilder({ page }).include('.calendar-page').analyze();
      expect(results.violations).toEqual([]);
      assertNoRuntimeErrors();
    });
  }
}

for (const story of stories) {
  for (const theme of themes) {
    test(`Storybook ${story} state is accessible in ${theme} theme`, async ({ page }, testInfo) => {
      test.skip(
        testInfo.project.name !== 'desktop',
        'Storybook axe coverage runs once in the desktop project.',
      );
      await page.goto(
        `${storybookBaseUrl}/iframe.html?id=aura-conversation-panel--${story}&viewMode=story`,
      );
      await expect(page.locator('aura-conversation-panel')).toBeVisible();
      await applyAndSettleStoryTheme(page, theme);
      if (story === 'empty') {
        await expect(
          page.getByRole('heading', { name: 'Where would you like to begin?' }),
        ).toBeVisible();
        await expect(
          page.getByText(/Bring a question, an idea, or a half-formed thought\./),
        ).toBeVisible();
        await expect(
          page.getByRole('alert').filter({ hasText: 'Agent setup is unavailable' }),
        ).toHaveCount(0);
      }
      if (story === 'run-inspector') {
        await expect(page.getByRole('dialog', { name: 'Recent runs' })).toBeVisible();
      }
      if (story === 'tool-activity') {
        await expect(page.getByRole('region', { name: 'Tool activity' })).toBeVisible();
        await expect(page.getByText('memory.search v1')).toBeVisible();
        await expect(page.getByText('2 authorized results')).toBeVisible();
      }
      if (story === 'attachment-ready') {
        await expect(page.getByText('inspection.png', { exact: true })).toBeVisible();
        await expect(page.getByText('grounding.pdf', { exact: true })).toBeVisible();
        const preview = page.getByRole('button', { name: 'Preview inspection.png' });
        await expect(preview).toBeVisible();
        await preview.click();
        await expect(page.getByRole('dialog', { name: 'inspection.png' })).toBeVisible();
        await page.getByRole('button', { name: 'Close image preview' }).click();
        await expect(preview).toBeFocused();
      }
      if (story === 'attachment-model-vision') {
        await expect(page.getByRole('alert')).toContainText(/verified vision model/i);
      }
      if (story === 'attachment-tombstone') {
        await expect(
          page.getByText('Attachment unavailable is unavailable', { exact: true }),
        ).toHaveCount(2);
        await expect(page.getByText('Unavailable', { exact: true })).toHaveCount(2);
        await expect(page.getByText('application/pdf', { exact: true })).toHaveCount(0);
      }
      const results = await new AxeBuilder({ page }).include('aura-conversation-panel').analyze();
      expect(results.violations).toEqual([]);
    });
  }
}

for (const story of managementStories) {
  for (const theme of themes) {
    test(`Management Storybook ${story} is accessible in ${theme} theme`, async ({
      page,
    }, testInfo) => {
      test.skip(
        testInfo.project.name !== 'desktop',
        'Storybook axe coverage runs once in the desktop project.',
      );
      await page.goto(
        `${storybookBaseUrl}/iframe.html?id=aura-management-surfaces-agents-and-personas--${story}&viewMode=story`,
      );
      await expect(page.locator('[data-feature-root]').first()).toBeVisible();
      await applyAndSettleStoryTheme(page, theme);
      const results = await new AxeBuilder({ page }).include('[data-feature-root]').analyze();
      expect(results.violations).toEqual([]);
    });
  }
}

for (const story of memoryStories) {
  const storyThemes = story === 'mobile-dark-records' ? (['dark'] as const) : themes;
  for (const theme of storyThemes) {
    test(`Memory Storybook ${story} is accessible in ${theme} theme`, async ({
      page,
    }, testInfo) => {
      test.skip(
        testInfo.project.name !== 'desktop',
        'Storybook axe coverage runs once in the desktop project.',
      );
      await page.goto(
        `${storybookBaseUrl}/iframe.html?id=aura-memory-product--${story}&viewMode=story`,
      );
      await expect(page.locator('.memory-page').first()).toBeVisible();
      await applyAndSettleStoryTheme(page, theme);
      const results = await new AxeBuilder({ page }).include('.memory-page').analyze();
      expect(results.violations).toEqual([]);
    });
  }
}

for (const story of documentStories) {
  for (const theme of themes) {
    test(`Document library Storybook ${story} is accessible in ${theme} theme`, async ({
      page,
    }, testInfo) => {
      test.skip(
        testInfo.project.name !== 'desktop',
        'Document Storybook axe coverage runs once in the desktop project.',
      );
      const assertNoRuntimeErrors = watchRuntimeErrors(page);
      await page.goto(
        `${storybookBaseUrl}/iframe.html?id=aura-knowledge-documents--${story}&viewMode=story`,
      );
      await expect(page.locator('.documents-page').first()).toBeVisible();
      await applyAndSettleStoryTheme(page, theme);
      if (story === 'ready' || story === 'failed') {
        await expect(
          page.getByText(story === 'ready' ? 'A quiet guide.md' : 'Needs retry.docx', {
            exact: true,
          }),
        ).toBeVisible();
      }
      const results = await new AxeBuilder({ page }).include('.documents-page').analyze();
      expect(results.violations).toEqual([]);
      assertNoRuntimeErrors();
    });
  }
}

for (const story of reminderStories) {
  for (const theme of themes) {
    test(`Reminder Storybook ${story} is accessible in ${theme} theme`, async ({
      page,
    }, testInfo) => {
      test.skip(
        testInfo.project.name !== 'desktop',
        'Storybook axe coverage runs once in the desktop project.',
      );
      const assertNoRuntimeErrors = watchRuntimeErrors(page);
      await page.goto(
        `${storybookBaseUrl}/iframe.html?id=aura-reminders-product--${story}&viewMode=story`,
      );
      await expect(page.locator('.reminders-page').first()).toBeVisible();
      await applyAndSettleStoryTheme(page, theme);
      if (story === 'workspace') {
        await expect(page.getByRole('heading', { name: 'Upcoming', exact: true })).toBeVisible();
        await expect(page.getByRole('button', { name: /History/ })).toBeVisible();
      }
      if (story === 'history') {
        await page.getByRole('button', { name: /History/ }).click();
        await expect(page.getByRole('heading', { name: 'History', exact: true })).toBeVisible();
        await expect(page.getByText('Call the dentist', { exact: true })).toBeVisible();
      }
      if (story === 'composer') {
        await page.getByRole('button', { name: 'New reminder', exact: true }).click();
        await expect(
          page.getByRole('heading', { name: 'What should Aura remind you about?' }),
        ).toBeVisible();
      }
      if (story === 'detail') {
        const detail = page.getByRole('dialog', { name: 'Bring the library books back' });
        await expect(detail).toBeVisible();
        await expect(
          detail.getByRole('button', { name: 'Mark complete', exact: true }),
        ).toBeVisible();
        await expect(
          detail.getByRole('button', { name: 'Cancel reminder', exact: true }),
        ).toBeVisible();
        await expect(detail.getByText('Timezone', { exact: true })).toBeVisible();
      }
      if (story === 'loading')
        await expect(
          page.getByRole('status').filter({ hasText: 'Loading reminders' }),
        ).toBeVisible();
      if (story === 'empty')
        await expect(
          page.getByRole('heading', { name: 'No upcoming reminders', exact: true }),
        ).toBeVisible();
      if (story === 'recovery')
        await expect(
          page.getByRole('alert').filter({ hasText: 'temporarily unavailable' }),
        ).toBeVisible();
      const results = await new AxeBuilder({ page }).include('.reminders-page').analyze();
      expect(results.violations).toEqual([]);
      assertNoRuntimeErrors();
    });
  }
}

for (const story of recurringReminderStories) {
  for (const theme of themes) {
    test(`Recurring reminder Storybook ${story} is accessible in ${theme} theme`, async ({
      page,
    }, testInfo) => {
      test.skip(
        testInfo.project.name !== 'desktop',
        'Storybook axe coverage runs once in the desktop project.',
      );
      const assertNoRuntimeErrors = watchRuntimeErrors(page);
      await page.goto(
        `${storybookBaseUrl}/iframe.html?id=aura-reminders-recurring-experience--${story}&viewMode=story`,
      );
      await expect(page.locator('.reminders-page').first()).toBeVisible();
      await applyAndSettleStoryTheme(page, theme);
      if (
        story === 'recurring-composer' ||
        story === 'series-detail' ||
        story === 'series-history'
      ) {
        await expect(page.getByRole('heading', { name: /reminder|repeat/i }).first()).toBeVisible();
      }
      if (story === 'recurring-composer') {
        await page.getByRole('button', { name: 'New reminder', exact: true }).click();
        await expect(page.getByLabel('Repeat schedule', { exact: true })).toBeVisible();
      }
      if (story === 'error-recovery' || story === 'unknown-outcome')
        await expect(
          page.getByRole('alert').filter({ hasText: /unknown|reconcile/i }),
        ).toBeVisible();
      if (story === 'conflict')
        await expect(
          page.getByRole('alert').filter({ hasText: /conflict|changed/i }),
        ).toBeVisible();
      if (story === 'offline')
        await expect(
          page.getByRole('alert').filter({ hasText: /offline|unavailable/i }),
        ).toBeVisible();
      const results = await new AxeBuilder({ page }).include('.reminders-page').analyze();
      expect(results.violations).toEqual([]);
      assertNoRuntimeErrors();
    });
  }
}

for (const story of notificationStories) {
  for (const theme of themes) {
    test(`Notification Storybook ${story} is accessible in ${theme} theme`, async ({
      page,
    }, testInfo) => {
      test.skip(
        testInfo.project.name !== 'desktop',
        'Storybook axe coverage runs once in the desktop project.',
      );
      await page.goto(
        `${storybookBaseUrl}/iframe.html?id=aura-notifications-centre--${story}&viewMode=story`,
      );
      await expect(page.locator('.notifications-page').first()).toBeVisible();
      await applyAndSettleStoryTheme(page, theme);
      if (story === 'inbox') {
        await expect(
          page.getByRole('heading', { name: 'Notifications', exact: true }),
        ).toBeVisible();
        await expect(page.getByRole('button', { name: /Unread/ })).toBeVisible();
      }
      if (story === 'history') {
        await expect(page.getByRole('button', { name: /History/ })).toHaveAttribute(
          'aria-current',
          'page',
        );
        await expect(page.getByText('Acknowledged', { exact: true })).toBeVisible();
      }
      if (story === 'detail') {
        const detail = page.getByRole('dialog', { name: 'Bring the library books back' });
        await expect(detail).toBeVisible();
        await expect(
          detail.getByRole('button', { name: 'Mark reminder complete', exact: true }),
        ).toBeVisible();
        await expect(detail.getByText(/Due/)).toBeVisible();
        await expect(detail.getByText('Unknown time', { exact: true })).toHaveCount(0);
      }
      if (story === 'loading')
        await expect(
          page.getByRole('status').filter({ hasText: 'Loading notifications' }),
        ).toBeVisible();
      if (story === 'empty')
        await expect(
          page.getByRole('heading', { name: 'All caught up', exact: true }),
        ).toBeVisible();
      if (story === 'recovery')
        await expect(
          page.getByRole('alert').filter({ hasText: 'temporarily unavailable' }),
        ).toBeVisible();
      const results = await new AxeBuilder({ page }).include('.notifications-page').analyze();
      expect(results.violations).toEqual([]);
    });
  }
}

for (const story of approvalStories) {
  for (const theme of themes) {
    test(`Approval Storybook ${story} is accessible in ${theme} theme`, async ({
      page,
    }, testInfo) => {
      test.skip(
        testInfo.project.name !== 'desktop',
        'Approval Storybook axe coverage runs once in the desktop project.',
      );
      await page.goto(
        `${storybookBaseUrl}/iframe.html?id=aura-governance-approval-inbox--${story}&viewMode=story`,
      );
      await expect(page.locator('.approvals-page').first()).toBeVisible();
      await applyAndSettleStoryTheme(page, theme);
      if (story === 'pending') {
        await expect(page.getByRole('heading', { name: 'Approvals', exact: true })).toBeVisible();
        await expect(page.getByRole('button', { name: /Pending/ })).toBeVisible();
        await expect(page.getByRole('button', { name: /Approve|Reject/ })).toHaveCount(0);
      }
      if (story === 'history') {
        await expect(page.getByRole('button', { name: /History/ })).toHaveAttribute(
          'aria-current',
          'page',
        );
        await expect(page.getByText('Approved', { exact: true })).toBeVisible();
        await expect(page.getByText('Rejected', { exact: true })).toBeVisible();
        await expect(page.getByRole('button', { name: /Approve|Reject/ })).toHaveCount(0);
      }
      if (story === 'detail' || story === 'conflict' || story === 'unknown-outcome') {
        const detail = page.getByRole('dialog', { name: /Create a reminder/ });
        await expect(detail).toBeVisible();
        await expect(detail.getByText('What will happen', { exact: true })).toBeVisible();
        await expect(detail.getByText('Origin', { exact: true })).toBeVisible();
        await expect(detail.getByText('Expires', { exact: true })).toBeVisible();
        await expect(detail.getByText('Technical details', { exact: true })).toBeVisible();
        await expect(detail.getByText('reminder.create v1', { exact: true })).not.toBeVisible();
        await detail.getByText('Technical details', { exact: true }).click();
        await expect(detail.getByText('reminder.create v1', { exact: true })).toBeVisible();
        await expect(detail.getByText('approval-pending', { exact: true })).toBeVisible();
        if (story === 'unknown-outcome') {
          const unknownNotice = detail.getByRole('alert').filter({ hasText: 'outcome is unknown' });
          await expect(unknownNotice).toBeVisible();
          await expect(
            unknownNotice.getByRole('button', { name: 'Retry', exact: true }),
          ).toBeVisible();
          await expect(detail.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
          await expect(detail.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
        } else {
          await expect(detail.getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
          await expect(detail.getByRole('button', { name: 'Reject', exact: true })).toBeVisible();
        }
      }
      if (story === 'terminal-detail') {
        const detail = page.getByRole('dialog', { name: /Create a reminder/ });
        await expect(detail).toBeVisible();
        await expect(detail.getByRole('status').filter({ hasText: 'durable' })).toBeVisible();
        await expect(detail.getByRole('button', { name: 'Approve', exact: true })).toHaveCount(0);
        await expect(detail.getByRole('button', { name: 'Reject', exact: true })).toHaveCount(0);
      }
      if (story === 'loading')
        await expect(
          page.getByRole('status').filter({ hasText: 'Loading pending approvals' }),
        ).toBeVisible();
      if (story === 'empty')
        await expect(
          page.getByRole('heading', { name: 'Nothing needs your decision', exact: true }),
        ).toBeVisible();
      if (story === 'authentication-error') {
        const sessionAlert = page.getByRole('alert').filter({ hasText: 'session has expired' });
        await expect(sessionAlert).toBeVisible();
        await expect(sessionAlert.getByRole('button', { name: 'Retry', exact: true })).toHaveCount(
          0,
        );
      }
      if (story === 'permission-error') {
        const permissionAlert = page.getByRole('alert').filter({ hasText: 'permission' });
        await expect(permissionAlert).toBeVisible();
        await expect(
          permissionAlert.getByRole('button', { name: 'Retry', exact: true }),
        ).toHaveCount(0);
      }
      if (story === 'unavailable-stream')
        await expect(
          page.getByRole('button', { name: 'Retry live updates', exact: true }),
        ).toBeVisible();
      if (story === 'recovery') {
        const recoveryNotice = page
          .getByRole('alert')
          .filter({ hasText: 'temporarily unavailable' });
        await expect(recoveryNotice).toBeVisible();
        await expect(
          recoveryNotice.getByRole('button', { name: 'Retry', exact: true }),
        ).toBeVisible();
      }
      const results = await new AxeBuilder({ page }).include('.approvals-page').analyze();
      expect(results.violations).toEqual([]);
    });
  }
}
