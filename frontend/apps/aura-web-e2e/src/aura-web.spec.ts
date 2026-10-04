import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

test('opens with a welcome-first conversation surface', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'A calm space to think.' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: 'Message Aura' })).toBeVisible();
});

test('supports Enter and Shift+Enter composer behavior', async ({ page }) => {
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('A first line');
  await composer.press('Shift+Enter');
  await expect(composer).toHaveValue('A first line\n');
  await composer.fill('Send with Enter');
  await composer.press('Enter');
  await expect(page.getByRole('article', { name: 'Your message' })).toContainText('Send with Enter');
});

test('persists the selected theme preference', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Theme persistence is covered once in the desktop project.');
  await page.goto('/');
  await page.getByRole('radio', { name: 'Dark' }).check();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await page.reload();
  await expect(page.getByRole('radio', { name: 'Dark' })).toBeChecked();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
});

test('supports desktop collapse and tablet rail navigation', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop' && testInfo.project.name !== 'tablet', 'Navigation geometry is covered by desktop and tablet projects.');
  const navigation = page.locator('#primary-navigation');
  if (testInfo.project.name === 'desktop') {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/');
    await navigation.getByRole('button', { name: 'Collapse navigation' }).click();
    await expect(navigation).toHaveClass(/is-collapsed/);
    await expect(navigation.getByRole('button', { name: 'Expand navigation' })).toBeVisible();
    return;
  }
  await page.setViewportSize({ width: 1024, height: 768 });
  await page.goto('/');
  await expect(navigation).toHaveCSS('flex-basis', '78px');
  await expect(navigation.getByRole('radio', { name: 'Dark' })).toBeVisible();
  await expect(navigation.getByRole('button', { name: 'New conversation' })).toBeVisible();
  await expect(navigation.getByRole('button', { name: 'A thoughtful beginning' })).toBeVisible();
  await expect(navigation.getByRole('button', { name: 'New conversation' })).toHaveAttribute('title', 'New conversation');
  await expect(navigation.getByRole('button', { name: 'A thoughtful beginning' })).toHaveAttribute('title', 'A thoughtful beginning');
});

test('sends a local message and allows interruption', async ({ page }) => {
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('A small local thought');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByRole('article', { name: 'Your message' }).getByText('A small local thought')).toBeVisible();
  const workingStatus = page.getByRole('status');
  await expect(workingStatus.getByRole('button', { name: 'Stop' })).toBeVisible();
  await workingStatus.getByRole('button', { name: 'Stop' }).click();
  await expect(page.getByText(/Generation stopped/)).toBeVisible();
  await expect(page.getByText('Generation interrupted')).toBeVisible();
});

test('renders a completed local reply', async ({ page }) => {
  await page.goto('/');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('A completed local thought');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByRole('article', { name: 'Aura response' })).toContainText('A completed local thought');
});

test('recovers from a deterministic local error', async ({ page }) => {
  await page.goto('/?fixture=error');
  const composer = page.getByRole('textbox', { name: 'Message Aura' });
  await composer.fill('A recoverable local thought');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByText(/could not complete that local preview/)).toBeVisible();
  await page.getByRole('button', { name: 'Try again' }).click();
  await expect(page.getByRole('article', { name: 'Aura response' })).toContainText('A recoverable local thought');
});

test('mobile composer remains sticky and passes axe checks', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile sticky and axe coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const composer = page.locator('form.composer-wrap');
  await expect(composer).toHaveCSS('position', 'sticky');
  await expect(composer).toHaveCSS('bottom', '0px');
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test('dark theme conversation passes axe checks', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'desktop', 'Dark contrast coverage runs once in the desktop project.');
  await page.addInitScript(() => localStorage.setItem('aura-theme', 'dark'));
  await page.goto('/');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations).toEqual([]);
});

test('mobile navigation returns focus to its trigger', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile overlay focus coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  await trigger.click();
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible();
  await page.getByRole('button', { name: 'Close navigation' }).click();
  await expect(trigger).toBeFocused();
});

test('mobile navigation closes on Escape and restores focus', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile Escape coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  const navigation = page.locator('#primary-navigation');
  await trigger.click();
  await expect(navigation).toHaveClass(/is-mobile-open/);
  await page.keyboard.press('Escape');
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(trigger).toBeFocused();
});

test('closed mobile navigation is hidden and unreachable by Tab', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Closed drawer keyboard coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  const navigation = page.locator('#primary-navigation');
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(navigation).toHaveCSS('visibility', 'hidden');
  await expect(navigation).toHaveCSS('pointer-events', 'none');
  await trigger.focus();
  const focusableCount = await page.locator('a:visible, button:visible, textarea:visible, input:visible, select:visible, [tabindex]:visible').count();
  for (let index = 0; index < focusableCount + 1; index += 1) {
    await page.keyboard.press('Tab');
    await expect.poll(() => navigation.evaluate((element) => element.contains(document.activeElement))).toBe(false);
  }
});

test('mobile conversation selection closes navigation and restores focus', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile selection coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  const navigation = page.locator('#primary-navigation');
  await trigger.click();
  await navigation.getByRole('button', { name: /A thoughtful beginning/ }).click();
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(trigger).toBeFocused();
});

test('mobile new conversation closes navigation and restores focus', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Mobile new-conversation coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  const navigation = page.locator('#primary-navigation');
  await trigger.click();
  await navigation.getByRole('button', { name: 'New conversation' }).click();
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(trigger).toBeFocused();
});

test('leaving mobile clears the modal drawer without a reload', async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== 'mobile', 'Responsive drawer transition coverage uses the mobile project.');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const trigger = page.getByRole('button', { name: 'Open navigation' });
  const navigation = page.locator('#primary-navigation');
  await trigger.click();
  await expect(navigation).toHaveClass(/is-mobile-open/);

  await page.setViewportSize({ width: 1024, height: 768 });
  await expect(navigation).not.toHaveClass(/is-mobile-open/);
  await expect(page.getByRole('button', { name: 'Close navigation' })).toHaveCount(0);
  await page.locator('#main-content').focus();
  await expect(page.locator('#main-content')).toBeFocused();
});
