// Contract for the compact personal checklist on Today.
// It is a mirror of /api/health/today, not a second checklist model:
// checking here must update the same routine that Personal → Daily renders.
const { test, expect } = require('@playwright/test');

const openToday = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('today'));
  await expect(page.locator('[data-testid="today-personal-checklist"]')).toBeVisible();
  await expect.poll(() => page.locator('[data-testid="today-personal-item"]').count(),
    { timeout: 15000 }).toBeGreaterThan(0);
};

const routineFrom = (payload, id) => payload.blocks
  .flatMap(block => block.items)
  .find(item => item.id === id);

test('Today shows and toggles the canonical personal checklist', async ({ page, request }) => {
  await openToday(page);

  const before = await (await request.get('/api/health/today')).json();
  const routine = before.blocks.flatMap(block => block.items)[0];
  const row = page.locator(`[data-testid="today-personal-item"][data-routine-id="${routine.id}"]`);

  await expect(page.locator('[data-testid="today-personal-progress"]'))
    .toHaveText(`${before.done}/${before.total}`);
  await expect(page.locator('[data-testid="today-personal-item"]')).toHaveCount(before.total);
  await expect(row).toHaveAttribute('aria-pressed', String(Boolean(routine.done)));

  try {
    await row.click();
    await expect(row).toHaveAttribute('aria-pressed', String(!routine.done));
    await expect.poll(async () => {
      const current = await (await request.get('/api/health/today')).json();
      return routineFrom(current, routine.id).done;
    }).toBe(!routine.done);
  } finally {
    const current = await (await request.get('/api/health/today')).json();
    if (routineFrom(current, routine.id).done !== routine.done) {
      await request.post(`/api/health/routines/${routine.id}/${routine.done ? 'check' : 'uncheck'}`);
    }
  }

  const restored = await (await request.get('/api/health/today')).json();
  expect(routineFrom(restored, routine.id).done).toBe(routine.done);
});

test('the checklist sits after the work stream and opens Personal', async ({ page }) => {
  await openToday(page);
  const order = await page.evaluate(() => {
    const children = [...document.getElementById('today-col').children];
    return {
      later: children.findIndex(el => el.id === 'today-later-drawer'),
      personal: children.findIndex(el => el.id === 'today-personal-checklist'),
      growth: children.findIndex(el => el.dataset.todayGroup === 'growth'),
    };
  });
  expect(order.personal).toBeGreaterThan(order.later);
  expect(order.personal).toBeLessThan(order.growth);

  await page.locator('[data-testid="today-personal-open"]').click();
  await expect(page.locator('#content-daily')).toBeVisible();
  await expect(page).toHaveURL(/tab=daily/);
});
