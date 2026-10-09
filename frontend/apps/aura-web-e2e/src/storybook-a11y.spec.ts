import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const stories = ['empty', 'loading', 'working', 'interrupted', 'recoverable-error', 'completed'] as const;
const themes = ['light', 'dark'] as const;
const memoryStories = ['records', 'first-run-setup', 'review-queue', 'empty-records', 'filtered-empty-records', 'configured-settings', 'reindex-settings', 'settings-error', 'mobile-dark-records', 'agent-policy-automatic', 'agent-policy-off'] as const;

for (const story of stories) {
  for (const theme of themes) {
    test(`Storybook ${story} state is accessible in ${theme} theme`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== 'desktop', 'Storybook axe coverage runs once in the desktop project.');
      await page.goto(`http://127.0.0.1:6006/iframe.html?id=aura-conversation-panel--${story}&viewMode=story`);
      await expect(page.locator('aura-conversation-panel')).toBeVisible();
      await page.evaluate((value) => { document.documentElement.dataset['theme'] = value; }, theme);
      const results = await new AxeBuilder({ page }).include('aura-conversation-panel').analyze();
      expect(results.violations).toEqual([]);
    });
  }
}

for (const story of memoryStories) {
  for (const theme of themes) {
    test(`Memory Storybook ${story} is accessible in ${theme} theme`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== 'desktop', 'Storybook axe coverage runs once in the desktop project.');
      await page.goto(`http://127.0.0.1:6006/iframe.html?id=aura-memory-product--${story}&viewMode=story`);
      await expect(page.locator('.memory-page').first()).toBeVisible();
      await page.evaluate((value) => { document.documentElement.dataset['theme'] = value; }, theme);
      const results = await new AxeBuilder({ page }).include('.memory-page').analyze();
      expect(results.violations).toEqual([]);
    });
  }
}
