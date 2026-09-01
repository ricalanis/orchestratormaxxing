// e2e for the Cycle-tab empty state (Playwright). The server (see
// playwright.config.js) serves a wiped DB copy — a first-run blank slate — so
// these run SERIAL and build on each other: the first-run test creates the first
// cycle, and the later tests assert the empty state is gone once it exists.
//
// Run: `npx playwright test` (webServer auto-starts one), or point at a running
// server with `PW_BASE_URL=http://127.0.0.1:8931 npx playwright test`.
const { test, expect } = require('@playwright/test');

// Distinct, unambiguous signals:
//   empty state → header contains "No cycles yet"
//   real board  → #cycle-kanban contains a real column label ("Backlog"); the
//                 loading skeleton's columns have NO labels, and the empty state
//                 clears the board. (NB: "velocity"/"burndown" appear in the
//                 empty-state COPY too, so they can't distinguish board vs empty.)
//   skeleton    → pulsing placeholders in the header (#cycle-header .animate-pulse)
const HEADER = '#cycle-header';
const BOARD = '#cycle-kanban';
const openCycleTab = async (page) => {
  await page.goto('/');
  // window.onload = init, and init ends with switchTab('today'). Wait for that to
  // settle before switching tabs, else init can re-hide the Cycle tab under us.
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('cycle'));
  await expect(page.locator('#content-cycle')).toBeVisible();
};

test.describe.configure({ mode: 'serial' });

test.describe('Cycle board empty state', () => {
  test('first-run: empty state + CTA creates the first cycle → board', async ({ page }) => {
    await openCycleTab(page);

    await expect(page.locator(HEADER)).toContainText('No cycles yet');
    const cta = page.getByRole('button', { name: /create your first cycle/i });
    await expect(cta).toBeVisible();

    await cta.click();
    await expect(page.locator(BOARD)).toContainText('Backlog');          // real board rendered
    await expect(page.locator(HEADER)).not.toContainText('No cycles yet');
  });

  test('loading skeleton shows, then resolves to the board', async ({ page }) => {
    // HOLD the board fetch (deterministic, not a timing race): the skeleton stays
    // up until we release it, then the board renders.
    let release;
    const gate = new Promise((r) => { release = r; });
    await page.route('**/api/cycle/active/board*', async (route) => {
      await gate;
      await route.continue();
    });
    await openCycleTab(page);   // asserts the Cycle tab is visible

    // Skeleton up (pulsing placeholders present), board not yet rendered. Use
    // presence (count), not toBeVisible — the animate-pulse animation makes the
    // visibility check flaky even though the placeholder is on-screen.
    await expect(page.locator(`${HEADER} .animate-pulse`)).not.toHaveCount(0);
    await expect(page.locator(BOARD)).not.toContainText('Backlog');

    release();

    // Board arrives, skeleton gone.
    await expect(page.locator(BOARD)).toContainText('Backlog');
    await expect(page.locator(`${HEADER} .animate-pulse`)).toHaveCount(0);
  });

  test('empty state does NOT render once a cycle exists', async ({ page }) => {
    await openCycleTab(page);

    await expect(page.locator(BOARD)).toContainText('Backlog');           // a real cycle
    await expect(page.locator(HEADER)).not.toContainText('No cycles yet');
    await expect(page.getByRole('button', { name: /create your first cycle/i })).toHaveCount(0);
  });
});
