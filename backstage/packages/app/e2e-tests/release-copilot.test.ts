import { test, expect } from '@playwright/test';

// Release Copilot plugin smoke test — requires the release-copilot service
// running on :8000 (proxied via /api/proxy/release-copilot).
test.describe('Release Copilot page', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto('/release-copilot');
    const enterButton = page.getByRole('button', { name: 'Enter' });
    if (await enterButton.isVisible().catch(() => false)) {
      await enterButton.click();
    }
    await expect(
      page.getByRole('main').getByRole('heading', { name: 'Release Copilot' }),
    ).toBeVisible();
  });

  test('renders all five tabs', async ({ page }) => {
    for (const name of [
      'Chat',
      'Deploy',
      'Dataflow',
      'Releases',
      'Queue',
      'Insights',
    ]) {
      await expect(page.getByRole('tab', { name })).toBeVisible();
    }
  });

  test('deploy tab pre-fills live deployment JSON', async ({ page }) => {
    await page.getByRole('tab', { name: 'Deploy' }).click();
    await expect(
      page.getByRole('button', { name: 'UAT', exact: true }),
    ).toBeVisible();
    // the JSON editor textarea: pre-filled from /api/deploy-template
    await expect(page.locator('textarea').first()).toHaveValue(/include/, {
      timeout: 30_000,
    });
  });

  test('dataflow tab loads defaults and submits to chat', async ({ page }) => {
    await page.getByRole('tab', { name: 'Dataflow' }).click();
    // deploy repo (last textbox) pre-filled from /api/df-template
    await expect(page.getByRole('textbox').last()).toHaveValue(/\//, {
      timeout: 30_000,
    });
    await page.getByRole('textbox').nth(0).fill('my-df-image');
    await page.getByRole('textbox').nth(1).fill('1.2.3');
    await page.getByRole('button', { name: 'Deploy to DF UAT' }).click();
    await expect(page.getByText(/Sent to the agent/i)).toBeVisible({
      timeout: 15_000,
    });
  });

  test('insights tab shows real release history from BigQuery log', async ({
    page,
  }) => {
    await page.getByRole('tab', { name: 'Insights' }).click();
    await expect(page.getByText('Release history')).toBeVisible();
    // known released chart in the event log
    await expect(page.getByText('orders-svc').first()).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByText('#999').first()).toBeVisible();
  });

  test('deploy tab submit renders agent preview with CONFIRM token', async ({
    page,
  }) => {
    test.setTimeout(180_000);
    await page.getByRole('tab', { name: 'Deploy' }).click();
    await expect(page.locator('textarea').first()).toHaveValue(/include/, {
      timeout: 30_000,
    });
    await page.getByRole('button', { name: 'Deploy to UAT' }).click();
    await page.getByRole('tab', { name: 'Chat' }).click();
    // Read-side assertion only: the preview + CONFIRM token appear; we never
    // send the token, so no workflow is dispatched.
    await expect(
      page.getByText(/CONFIRM-[a-z0-9]+/i).last(),
    ).toBeVisible({ timeout: 150_000 });
  });

  test('queue tab lists items and add dialog has jira field', async ({
    page,
  }) => {
    await page.getByRole('tab', { name: 'Queue' }).click();
    await page.getByRole('button', { name: 'Add items' }).click();
    await expect(page.getByText('Jira ticket').first()).toBeVisible();
    await expect(page.getByText('Build run URL').first()).toBeVisible();
    await page.getByRole('button', { name: 'Cancel' }).click();
  });

  test('chat round-trip answers a release status query', async ({ page }) => {
    test.setTimeout(120_000);
    await page
      .getByPlaceholder(/Message the release agent/i)
      .fill('what is the current release status?');
    await page.getByRole('button', { name: 'Send' }).click();
    // streamed agent text should mention an environment or known charts
    // Real chart names from deployment state — cannot match the input hint.
    await expect(
      page.getByText(/orders-svc|test-prod-svc|another-svc/i).first(),
    ).toBeVisible({ timeout: 90_000 });
  });
});
