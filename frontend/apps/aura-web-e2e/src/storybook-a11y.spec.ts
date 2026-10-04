import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const stories = ['empty', 'loading', 'working', 'interrupted', 'recoverable-error', 'completed'] as const;
const themes = ['light', 'dark'] as const;

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
