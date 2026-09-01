/*
 * 💰 Cobro — the money band's Today contract (m17, cobro first-class).
 *
 * The block is READ-ONLY: it presents /api/crm/cash-flow and deep-links into
 * the deal drawer where the 💵/✅ verbs live. Every response is intercepted:
 * this spec owns its state and never writes to the operator's database.
 *
 * Run: `npx playwright test today-cobro --workers=1`
 */
const { test, expect } = require('@playwright/test');

const ROW = (id, title, value, extra = {}) => ({
  deal_id: id, title, account_name: title.split(' ')[0], value,
  currency: 'MXN', expected_payment_date: '2026-08-06', invoiced: true,
  paid: false, delivered: false, days_late: 0, ...extra,
});

const EMPTY_CF = {
  status: 'ok', date: '2026-08-04',
  week: { start: '2026-08-03', end: '2026-08-09', total: 0, rows: [] },
  month: { label: 'AGO', collected: 0, expected: 0, target: null },
  overdue: [], no_expected: [],
  leaks: { uninvoiced_count: 0, uninvoiced_value: 0, no_expected_count: 0,
           no_project_count: 0, first_uninvoiced_deal_id: null,
           first_no_expected_deal_id: null, first_no_project_deal_id: null },
  slippage: null,
  narrative: { severity: 'healthy', text: 'Al día · AGO: $0 cobrado' },
};

const BUSY_CF = {
  ...EMPTY_CF,
  week: { start: '2026-08-03', end: '2026-08-09', total: 24500,
          rows: [ROW('d_cf_week', 'Acme Sprint 2', 6000, { delivered: true }),
                 ROW('d_cf_week2', 'Norte Golden Record', 18500)] },
  month: { label: 'AGO', collected: 6000, expected: 120000, target: 300000 },
  overdue: [ROW('d_cf_late', 'Vega Consultoria', 18500,
                { expected_payment_date: '2026-07-23', days_late: 12 })],
  leaks: { ...EMPTY_CF.leaks, uninvoiced_count: 4, uninvoiced_value: 200500,
           no_project_count: 4, first_uninvoiced_deal_id: 'd_cf_leak' },
  narrative: { severity: 'overdue', text: '🔴 Vega venció hace 12d' },
};

async function openToday(page, cf = EMPTY_CF) {
  await page.route('**/api/crm/cash-flow*', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(cf),
  }));
  await page.route('**/api/context/deal/*', route => {
    const id = route.request().url().split('/').pop();
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      entity: { type: 'deal', id, title: id, stage: 'won', value: 1000 },
      ancestors: [], children: [], actions: [],
    }) });
  });
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('today'));
  await expect(page.locator('[data-testid="today-cobro"]')).toBeVisible();
  await expect(page.locator('#today-cobro-mount [data-state]')).toHaveAttribute(
    'data-state', /ready|empty/);
}

test('the money band: cobro sits immediately above the deal pipeline', async ({ page }) => {
  await openToday(page);
  const order = await page.evaluate(() => {
    const kids = [...document.getElementById('today-col').children];
    const at = selector => kids.findIndex(el => el.matches(selector));
    return {
      goals: at('#today-goals'),
      cobro: at('#today-cobro'),
      pipeline: at('[data-testid="today-deal-pipeline"]'),
    };
  });
  // Adjacent halves of one band, and never above the goals block (that one is
  // deliberately the first content section).
  expect(order.goals).toBeGreaterThanOrEqual(0);
  expect(order.goals).toBeLessThan(order.cobro);
  expect(order.cobro).toBe(order.pipeline - 1);
});

test('empty-healthy: two quiet lines, never a tall empty box, never hidden', async ({ page }) => {
  await openToday(page, EMPTY_CF);
  const block = page.locator('#today-cobro-mount');
  await expect(block).toContainText('Nada por cobrar');
  await expect(block).toContainText('AGO');
  // Quiet strip stays quiet: no 💰 chip without red overdue.
  await expect(page.locator('#today-highlights button', { hasText: '💰' })).toHaveCount(0);
});

test('a week row deep-links into the deal drawer — the verbs live there', async ({ page }) => {
  await openToday(page, BUSY_CF);
  const block = page.locator('#today-cobro-mount');
  await expect(block).toContainText('Acme');
  await block.getByText('Acme', { exact: false }).first().click();
  // openEntity('deal', …) drives the context drawer.
  await expect(page.locator('#entity-drawer, [data-testid="entity-drawer"], #context-drawer')
    .first()).toBeVisible({ timeout: 10000 });
});

test('red money raises the chip; the strip shows the overdue row', async ({ page }) => {
  await openToday(page, BUSY_CF);
  await expect(page.locator('#today-cobro-mount')).toContainText('Vega');
  const chip = page.locator('#today-highlights button', { hasText: '💰' });
  await expect(chip).toHaveCount(1);
  await expect(chip).toContainText('1 vencido');
});
