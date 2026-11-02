import AxeBuilder from '@axe-core/playwright';
import { expect, test } from '@playwright/test';

const stories = [
  ['healthy', /Fresh aggregate/],
  ['partial-and-stale', /Stale source|Partial snapshot/],
  ['loading', /Loading workflow monitoring/],
  ['empty', /No workflow telemetry/],
  ['unavailable', /Workflow monitoring unavailable|Unavailable/],
] as const;

for (const [storyId, stateText] of stories) {
  test(`workflow Storybook ${storyId} renders and is accessible`, async ({ page }) => {
    await page.goto(`/iframe.html?id=observatory-workflow-monitoring--${storyId}&viewMode=story`);
    await expect(page.getByRole('heading', { name: 'Workflow monitoring' })).toBeVisible();
    await expect(page.getByText(stateText)).toBeVisible();
    const results = await new AxeBuilder({ page }).disableRules(['landmark-one-main', 'page-has-heading-one']).analyze();
    expect(results.violations).toEqual([]);
  });
}

test('workflow Storybook remains usable with reduced motion and forced colors', async ({ page }) => {
  await page.emulateMedia({ reducedMotion: 'reduce', forcedColors: 'active' });
  await page.goto('/iframe.html?id=observatory-workflow-monitoring--healthy&viewMode=story');
  await expect(page.getByRole('heading', { name: 'Workflow monitoring' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Refresh workflow monitoring snapshot' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
  const results = await new AxeBuilder({ page }).disableRules(['landmark-one-main', 'page-has-heading-one']).analyze();
  expect(results.violations).toEqual([]);
});

test('workflow Storybook renders a readable dark theme', async ({ page }) => {
  await page.goto('/iframe.html?id=observatory-workflow-monitoring--dark&viewMode=story');
  await expect(page.getByRole('heading', { name: 'Workflow monitoring' })).toBeVisible();
  const colors = await page.locator('.panel').first().evaluate((node) => ({ background: getComputedStyle(node).backgroundColor, color: getComputedStyle(node).color }));
  expect(colors.background).toBe('rgb(25, 22, 25)');
  expect(colors.color).toBe('rgb(247, 240, 243)');
  const results = await new AxeBuilder({ page }).disableRules(['landmark-one-main', 'page-has-heading-one']).analyze();
  expect(results.violations).toEqual([]);
});
