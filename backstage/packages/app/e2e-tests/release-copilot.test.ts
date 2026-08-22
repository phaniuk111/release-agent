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
      page.getByRole('heading', { name: 'Release Copilot' }),
    ).toBeVisible();
  });

  test('renders all five tabs', async ({ page }) => {
    for (const name of ['Chat', 'Deploy', 'Dataflow', 'Releases', 'Queue']) {
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

  test('queue tab lists or shows empty queue', async ({ page }) => {
    await page.getByRole('tab', { name: 'Queue' }).click();
    await expect(page.getByRole('button', { name: 'Add items' })).toBeVisible();
  });

  test('chat round-trip answers a release status query', async ({ page }) => {
    test.setTimeout(120_000);
    await page.getByPlaceholder(/Message the release agent/i).fill(
      'what is the current release status?',
    );
    await page.getByRole('button', { name: 'Send' }).click();
    // streamed agent text should mention an environment or known charts
    // Real chart names from deployment state — cannot match the input hint.
    await expect(
      page.getByText(/orders-svc|test-prod-svc|another-svc/i).first(),
    ).toBeVisible({ timeout: 90_000 });
  });
});
