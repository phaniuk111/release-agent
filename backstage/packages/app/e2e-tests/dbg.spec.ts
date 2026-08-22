import { test } from '@playwright/test';
test('dbg', async ({ page }) => {
  page.on('console', m => console.log('CONSOLE', m.type(), m.text()));
  page.on('response', r => {
    if (r.url().includes('deploy-template')) console.log('RESP', r.status(), r.url());
  });
  await page.goto('/release-copilot');
  const b = page.getByRole('button', { name: 'Enter' });
  if (await b.isVisible().catch(() => false)) await b.click();
  await page.getByRole('tab', { name: 'Deploy' }).click();
  await page.waitForTimeout(6000);
  const ta = await page.locator('textarea').first().inputValue();
  console.log('TEXTAREA_LEN', ta.length, JSON.stringify(ta.slice(0, 80)));
  console.log('BODY_SNIP', (await page.textContent('body'))?.match(/Deploy charts[\s\S]{0,300}/)?.[0]);
});
