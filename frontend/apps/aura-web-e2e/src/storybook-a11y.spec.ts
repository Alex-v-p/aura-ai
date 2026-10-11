import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const storybookBaseUrl = (process.env.AURA_WEB_E2E_STORYBOOK_URL ?? `http://127.0.0.1:${process.env.AURA_WEB_E2E_STORYBOOK_PORT ?? '6300'}`).replace(/\/$/, '');
const stories = ['empty', 'loading', 'working', 'tool-activity', 'interrupted', 'recoverable-error', 'completed', 'archived-recovery', 'run-inspector'] as const;
const themes = ['light', 'dark'] as const;
const memoryStories = ['records', 'first-run-setup', 'review-queue', 'empty-records', 'filtered-empty-records', 'configured-settings', 'reindex-settings', 'settings-error', 'mobile-dark-records', 'agent-policy-automatic', 'agent-policy-off'] as const;
const managementStories = ['agent-library', 'agent-builder', 'persona-library', 'persona-builder', 'persona-detail'] as const;
const applyStoryTheme = (theme: 'light' | 'dark'): void => {
  document.documentElement.dataset['theme'] = theme;
  document.documentElement.style.colorScheme = theme;
  document.body.style.background = 'var(--canvas)';
  document.body.style.color = 'var(--text)';
};
const applyAndSettleStoryTheme = async (page: import('@playwright/test').Page, theme: 'light' | 'dark'): Promise<void> => {
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.evaluate(applyStoryTheme, theme);
  await page.evaluate(() => document.fonts?.ready);
  await page.waitForFunction(() => {
    const root = document.documentElement;
    const styles = getComputedStyle(root);
    return styles.getPropertyValue('--canvas').trim() !== '' && styles.getPropertyValue('--text').trim() !== '';
  });
  await page.waitForTimeout(50);
};

for (const story of stories) {
  for (const theme of themes) {
    test(`Storybook ${story} state is accessible in ${theme} theme`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== 'desktop', 'Storybook axe coverage runs once in the desktop project.');
      await page.goto(`${storybookBaseUrl}/iframe.html?id=aura-conversation-panel--${story}&viewMode=story`);
      await expect(page.locator('aura-conversation-panel')).toBeVisible();
      await applyAndSettleStoryTheme(page, theme);
      if (story === 'empty') {
        await expect(page.getByRole('heading', { name: 'Where would you like to begin?' })).toBeVisible();
        await expect(page.getByText(/Bring a question, an idea, or a half-formed thought\./)).toBeVisible();
        await expect(page.getByRole('alert').filter({ hasText: 'Agent setup is unavailable' })).toHaveCount(0);
      }
      if (story === 'run-inspector') {
        await expect(page.getByRole('dialog', { name: 'Recent runs' })).toBeVisible();
      }
      if (story === 'tool-activity') {
        await expect(page.getByRole('region', { name: 'Tool activity' })).toBeVisible();
        await expect(page.getByText('memory.search v1')).toBeVisible();
        await expect(page.getByText('2 authorized results')).toBeVisible();
      }
      const results = await new AxeBuilder({ page }).include('aura-conversation-panel').analyze();
      expect(results.violations).toEqual([]);
    });
  }
}

for (const story of managementStories) {
  for (const theme of themes) {
    test(`Management Storybook ${story} is accessible in ${theme} theme`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== 'desktop', 'Storybook axe coverage runs once in the desktop project.');
      await page.goto(`${storybookBaseUrl}/iframe.html?id=aura-management-surfaces-agents-and-personas--${story}&viewMode=story`);
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
    test(`Memory Storybook ${story} is accessible in ${theme} theme`, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== 'desktop', 'Storybook axe coverage runs once in the desktop project.');
      await page.goto(`${storybookBaseUrl}/iframe.html?id=aura-memory-product--${story}&viewMode=story`);
      await expect(page.locator('.memory-page').first()).toBeVisible();
      await applyAndSettleStoryTheme(page, theme);
      const results = await new AxeBuilder({ page }).include('.memory-page').analyze();
      expect(results.violations).toEqual([]);
    });
  }
}
