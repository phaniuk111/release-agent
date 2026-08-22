import { test, expect } from '@playwright/test';

test('DF UAT deploy end-to-end: form -> preview -> CONFIRM -> dispatch', async ({ page }) => {
  test.setTimeout(300_000);
  await page.goto('/release-copilot');
  const b = page.getByRole('button', { name: 'Enter' });
  if (await b.isVisible().catch(() => false)) await b.click();

  await page.getByRole('tab', { name: 'Dataflow' }).click();
  await page.getByRole('textbox').nth(0).fill('e2e-poc-test-image');
  await page.getByRole('textbox').nth(1).fill('0.0.1-e2e');
  await page.getByRole('button', { name: 'Deploy to DF UAT' }).click();
  await page.getByRole('tab', { name: 'Chat' }).click();

  const tokenEl = page.locator('text=/CONFIRM-[a-z0-9]+/i').last();
  await expect(tokenEl).toBeVisible({ timeout: 180_000 });
  const bodyText = (await page.textContent('body')) ?? '';
  const token = bodyText.match(/CONFIRM-[a-z0-9-]+/i)?.[0];
  console.log('GOT_TOKEN:', token);
  expect(token).toBeTruthy();

  // Send the CONFIRM token back — this is the step that dispatches the workflow.
  await page.getByPlaceholder(/Message the release agent/i).fill(token!);
  await page.getByRole('button', { name: 'Send' }).click();
  await page.waitForTimeout(60_000);
  const after = (await page.textContent('body')) ?? '';
  console.log('AFTER_SNIP:', after.match(/[\s\S]{0,150}(dispatch|deploy|workflow|error|fail)[\s\S]{0,250}/i)?.[0]);
});
