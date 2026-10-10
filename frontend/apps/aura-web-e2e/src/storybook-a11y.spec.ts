import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const storybookBaseUrl = (process.env.AURA_WEB_E2E_STORYBOOK_URL ?? `http://127.0.0.1:${process.env.AURA_WEB_E2E_STORYBOOK_PORT ?? '6300'}`).replace(/\/$/, '');
const stories = ['empty', 'loading', 'working', 'interrupted', 'recoverable-error', 'completed', 'archived-recovery', 'run-inspector'] as const;
const themes = ['light', 'dark'] as const;
const memoryStories = ['records', 'first-run-setup', 'review-queue', 'empty-records', 'filtered-empty-records', 'configured-settings', 'reindex-settings', 'settings-error', 'mobile-dark-records', 'agent-policy-automatic', 'agent-policy-off'] as const;

for (const story of stories) {
  for (const theme of themes) {
    test(`Storybook ${story} state is accessible in ${theme} theme`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== 'desktop', 'Storybook axe coverage runs once in the desktop project.');
      await page.goto(`${storybookBaseUrl}/iframe.html?id=aura-conversation-panel--${story}&viewMode=story`);
      await expect(page.locator('aura-conversation-panel')).toBeVisible();
      await page.evaluate((value) => { document.documentElement.dataset['theme'] = value; }, theme);
      if (story === 'empty') {
        await expect(page.getByRole('heading', { name: 'Where would you like to begin?' })).toBeVisible();
        await expect(page.getByText(/Bring a question, an idea, or a half-formed thought\./)).toBeVisible();
        await expect(page.getByRole('alert').filter({ hasText: 'Agent setup is unavailable' })).toHaveCount(0);
      }
      if (story === 'run-inspector') {
        await expect(page.getByRole('dialog', { name: 'Recent runs' })).toBeVisible();
      }
      const results = await new AxeBuilder({ page }).include('aura-conversation-panel').analyze();
      expect(results.violations).toEqual([]);
    });
  }
}

for (const story of memoryStories) {
  for (const theme of themes) {
    test(`Memory Storybook ${story} is accessible in ${theme} theme`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== 'desktop', 'Storybook axe coverage runs once in the desktop project.');
      await page.goto(`${storybookBaseUrl}/iframe.html?id=aura-memory-product--${story}&viewMode=story`);
      await expect(page.locator('.memory-page').first()).toBeVisible();
      await page.evaluate((value) => { document.documentElement.dataset['theme'] = value; }, theme);
      const results = await new AxeBuilder({ page }).include('.memory-page').analyze();
      expect(results.violations).toEqual([]);
    });
  }
}
