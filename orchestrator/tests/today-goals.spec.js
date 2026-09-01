// Contract for the compact “Metas de hoy” mirror on Today.
// Goal text is canonical reflection data; only its date-scoped completion state
// is mutable here.
const { test, expect } = require('@playwright/test');

const openToday = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('today'));
  await expect(page.locator('[data-testid="today-goals"]')).toBeVisible();
};

test('lists canonical goals and persists one completion through reload', async ({ page }) => {
  const state = {
    date: '2026-08-03',
    morning: {
      intentions: ['Cerrar propuesta Acme', 'Caminar 30 minutos', 'Leer capítulo 4'],
      completed: [false, true, false],
      created_at: '2026-08-03T07:15:00-06:00',
    },
    evening: null,
    day_review: null,
    created_at: '2026-08-03T07:15:00-06:00',
  };
  await page.route('**/api/reflection', route => route.fulfill({ json: state }));
  await page.route('**/api/reflection/morning/progress', async route => {
    const body = route.request().postDataJSON();
    state.morning.completed[body.index] = body.completed;
    await route.fulfill({ json: state });
  });

  await openToday(page);
  const rows = page.locator('[data-testid="today-goal-item"]');
  await expect(rows).toHaveCount(3);
  await expect(rows.nth(0)).toContainText('Cerrar propuesta Acme');
  await expect(rows.nth(1)).toContainText('Caminar 30 minutos');
  await expect(rows.nth(2)).toContainText('Leer capítulo 4');
  await expect(page.locator('[data-testid="today-goals-progress"]')).toHaveText('1/3');
  await expect(rows.nth(1)).toHaveAttribute('aria-pressed', 'true');

  await rows.nth(0).click();
  await expect(rows.nth(0)).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('[data-testid="today-goals-progress"]')).toHaveText('2/3');

  await page.reload();
  await expect(page.locator('[data-testid="today-goals"]')).toBeVisible();
  await expect(page.locator('[data-testid="today-goal-item"]').nth(0))
    .toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('[data-testid="today-goals-progress"]')).toHaveText('2/3');

  await page.locator('[data-testid="today-goals-open-reflection"]').click();
  await expect(page.locator('#content-reflection')).toBeVisible();
  await expect(page).toHaveURL(/tab=reflection/);
});

test('empty state routes to Personal reflection and goals are the first Today section', async ({ page }) => {
  await page.route('**/api/reflection', route => route.fulfill({ json: {
    date: '2026-08-03', morning: null, evening: null, day_review: null, created_at: null,
  }}));
  await openToday(page);

  await expect(page.locator('[data-testid="today-goals-empty"]'))
    .toContainText('Define tus 1–3 metas');
  const order = await page.evaluate(() => {
    const children = [...document.getElementById('today-col').children];
    return {
      goals: children.findIndex(el => el.id === 'today-goals'),
      horizon: children.findIndex(el => el.id === 'today-horizon'),
      plan: children.findIndex(el => el.id === 'today-plan-wrap'),
    };
  });
  expect(order.goals, 'page header is child 0; goals must be the first content section').toBe(1);
  expect(order.goals).toBeLessThan(order.horizon);
  expect(order.goals).toBeLessThan(order.plan);

  await page.locator('[data-testid="today-goals-empty-reflection"]').click();
  await expect(page.locator('#content-reflection')).toBeVisible();
  await expect(page).toHaveURL(/tab=reflection/);
});
