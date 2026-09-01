// e2e for P2-2: a Lakehouse answer that names an entity/cycle id renders a
// clickable link that opens it ON THE BOARD (entity → drawer over the board;
// cycle → the This Cycle lens pinned to that cycle). The real /api/lakehouse/*
// is served over MCP (often unavailable), so we route-mock it with a result that
// carries a real task id + a cycle id. Runs against the wiped-cycle DB copy.
//
// Run: `npx playwright test lakehouse`.
const { test, expect } = require('@playwright/test');

const RESULT = '#lakehouse-ask-result';

test('Lakehouse answer ids deep-link to the board (P2-2)', async ({ page }) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  const taskId = await page.evaluate(async () => (await fetch('/api/tasks?limit=0').then((r) => r.json())).tasks[0].id);

  // Mock the lakehouse endpoints (real one is over MCP).
  await page.route('**/api/lakehouse/overview', (route) =>
    route.fulfill({ contentType: 'application/json', body: JSON.stringify({ available: true, as_of: null, metrics: [] }) }));
  await page.route('**/api/lakehouse/ask*', (route) =>
    route.fulfill({
      contentType: 'application/json',
      body: JSON.stringify({
        available: true, resolved_to: { metric: 'demo' }, via: 'test',
        rows: [{ task: taskId, cycle: 'cyc_27dd8d89', accepted: 3 }],
      }),
    }));

  const ask = async () => {
    await page.evaluate(() => switchTab('lakehouse'));
    await expect(page.locator('#content-lakehouse')).toBeVisible();
    await page.fill('#lakehouse-ask-input', 'demo');
    await page.click('button[onclick="askLakehouse()"]');
  };

  await ask();

  // The id cells rendered as clickable deep-links (task → entity, cycle → cycle).
  const taskLink = page.locator(`${RESULT} button[onclick*="lakehouseOpenEntity('task','${taskId}'"]`);
  await expect(taskLink).toBeVisible();
  await expect(page.locator(`${RESULT} button[onclick*="lakehouseOpenCycle('cyc_27dd8d89'"]`)).toBeVisible();
  // A non-id cell stays plain text (no link).
  await expect(page.locator(`${RESULT}`)).toContainText('3');

  // Task link → board + task drawer.
  await taskLink.click();
  await expect(page.locator('#content-board')).toBeVisible();
  await expect(page.locator('#entity-drawer')).toBeVisible();
  await expect(page.locator('#ed-kind')).toHaveText('task');

  // Cycle link → board with the This Cycle lens pinned to that cycle.
  await page.keyboard.press('Escape');
  await ask();
  await page.locator(`${RESULT} button[onclick*="lakehouseOpenCycle"]`).click();
  await expect(page.locator('#content-board')).toBeVisible();
  const state = await page.evaluate(() => ({ lens: BOARD_LENS, override: BOARD_CYCLE_OVERRIDE }));
  expect(state.lens).toBe('cycle');
  expect(state.override).toBe('cyc_27dd8d89');
});
