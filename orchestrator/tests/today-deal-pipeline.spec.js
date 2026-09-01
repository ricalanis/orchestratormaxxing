/*
 * Home's commercial-awareness contract. The pipeline is an always-visible,
 * read-only bridge between operational work and the deferred/analytical zones.
 * Every CRM response is intercepted: this spec owns its state and never writes
 * to the operator's database.
 *
 * Run: `npx playwright test today-deal-pipeline --workers=1`
 */
const { test, expect } = require('@playwright/test');

const D = (id, stage, title, value) => ({
  id, stage, title, value, currency: 'MXN', account_name: title.split(' ')[0],
});

const PIPELINE = {
  stages: ['lead', 'engaged', 'qualified', 'demo', 'proposal', 'stalled', 'won', 'delivered', 'lost'],
  by_stage: {
    lead: [D('d_lead_1', 'lead', 'Acme discovery', 10000), D('d_lead_2', 'lead', 'Beta audit', 20000), D('d_lead_3', 'lead', 'Gamma platform', 30000)],
    engaged: [],
    qualified: [D('d_qualified', 'qualified', 'Delta data sprint', 40000)],
    demo: [],
    proposal: [D('d_proposal_1', 'proposal', 'Epsilon transformation', 50000), D('d_proposal_2', 'proposal', 'Zeta roadmap', 60000)],
    stalled: [D('d_stalled', 'stalled', 'Icebox opportunity', 70000)],
    won: [D('d_won', 'won', 'Won delivery active', 80000)],
    delivered: [D('d_delivered', 'won', 'Delivered history', 90000)],
    lost: [D('d_lost', 'lost', 'Lost history', 100000)],
  },
  counts: { lead: 3, engaged: 0, qualified: 1, demo: 0, proposal: 2, stalled: 1, won: 1, delivered: 1, lost: 1 },
  open_value: 210000,
  won_value: 170000,
  stalled_value: 70000,
};

async function openToday(page, pipeline = PIPELINE) {
  await page.addInitScript(() => {
    localStorage.removeItem('hermes_today_group_growth');
    localStorage.removeItem('hermes_today_group_reference');
  });
  await page.route('**/api/crm/pipeline', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(pipeline),
  }));
  await page.route('**/api/context/deal/*', route => {
    const id = route.request().url().split('/').pop();
    const deal = Object.values(pipeline.by_stage).flat().find(d => d.id === id) || D(id, 'lead', id, 0);
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      entity: { type: 'deal', ...deal }, ancestors: [], children: [], actions: [],
    }) });
  });
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('today'));
  await expect(page.locator('[data-testid="today-deal-pipeline"]')).toBeVisible();
  await expect(page.locator('[data-testid="today-deal-pipeline"] [data-pipeline-state="ready"]')).toBeVisible();
}

test('pipeline is visible after operations and before deferred work and analytics', async ({ page }) => {
  await openToday(page);
  const order = await page.evaluate(() => {
    const kids = [...document.getElementById('today-col').children];
    const at = selector => kids.findIndex(el => el.matches(selector));
    return {
      review: at('#today-review-zone'), pipeline: at('[data-testid="today-deal-pipeline"]'),
      later: at('#today-later-drawer'), growth: at('section[data-today-group="growth"]'),
      growthCollapsed: document.getElementById('today-sec-growth-body').classList.contains('hidden'),
    };
  });
  expect(order.review).toBeLessThan(order.pipeline);
  expect(order.pipeline).toBeLessThan(order.later);
  expect(order.later).toBeLessThan(order.growth);
  expect(order.growthCollapsed, 'analytics remain collapsed; the actual pipeline does not').toBe(true);
});

test('shows the fixed active lifecycle, capped deal rows, and honest value buckets', async ({ page }) => {
  await openToday(page);
  const pulse = page.locator('[data-testid="today-deal-pipeline"]');
  await expect(pulse).toContainText('$210,000');
  await expect(pulse).toContainText('$80,000');
  await expect(pulse).not.toContainText('<span');
  for (const stage of ['lead', 'engaged', 'qualified', 'demo', 'proposal', 'won']) {
    await expect(pulse.locator(`[data-pipeline-stage="${stage}"]`)).toHaveCount(1);
  }
  await expect(pulse.locator('[data-pipeline-stage="lead"] [data-deal-id]')).toHaveCount(2);
  await expect(pulse.locator('[data-pipeline-stage="lead"]')).toContainText('+1');
  await expect(pulse).toContainText('Won / Active');
  await expect(pulse).toContainText('1 stalled');
  await expect(pulse).not.toContainText('Delivered history');
  await expect(pulse).not.toContainText('Lost history');
  await expect(pulse.locator('select, [draggable="true"], .crm-deliver-btn')).toHaveCount(0);
});

test('deal buttons open the canonical drawer and full-pipeline opens CRM', async ({ page }) => {
  await openToday(page);
  const deal = page.locator('[data-testid="today-deal-pipeline"] [data-deal-id="d_lead_1"]');
  await deal.click();
  await expect(page.locator('#entity-drawer')).toBeVisible();
  await expect(page.locator('#ed-kind')).toHaveText('deal');
  await expect(page).toHaveURL(/entity=deal%3Ad_lead_1|entity=deal:d_lead_1/);

  await page.evaluate(() => closeEntity({ push: false }));
  await page.locator('[data-testid="today-deal-pipeline"] [data-action="full-pipeline"]').click();
  await expect(page.locator('#content-crm')).toBeVisible();
});

test('loading, error, retry, and empty are distinct without breaking Today', async ({ page }) => {
  let fail = true;
  await page.route('**/api/crm/pipeline', route => fail
    ? route.fulfill({ status: 503, body: 'unavailable' })
    : route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(PIPELINE) }));
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('today'));
  const pulse = page.locator('[data-testid="today-deal-pipeline"]');
  await expect(pulse.locator('[data-pipeline-state="error"]')).toBeVisible();
  await expect(page.locator('#today-plan-wrap')).toBeVisible();
  fail = false;
  await pulse.locator('[data-action="retry"]').click();
  await expect(pulse.locator('[data-pipeline-state="ready"]')).toBeVisible();

  await page.route('**/api/crm/pipeline', route => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify({
      ...PIPELINE, by_stage: Object.fromEntries(PIPELINE.stages.map(s => [s, []])),
      counts: Object.fromEntries(PIPELINE.stages.map(s => [s, 0])), open_value: 0, won_value: 0, stalled_value: 0,
    }),
  }));
  await page.evaluate(() => loadTodayDealPipeline());
  await expect(pulse.locator('[data-pipeline-state="empty"]')).toBeVisible();
  await expect(pulse).toContainText('No active opportunities');
});

test.use({ viewport: { width: 390, height: 844 } });
test('mobile confines pipeline width and keeps Won reachable', async ({ page }) => {
  await openToday(page);
  await expect(page.locator('[data-pipeline-stage="won"]')).toBeVisible();
  const width = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    page: document.documentElement.scrollWidth,
    pulse: document.querySelector('[data-testid="today-deal-pipeline"]').getBoundingClientRect().width,
    railClient: document.querySelector('.today-deal-pipeline-rail').clientWidth,
    railScroll: document.querySelector('.today-deal-pipeline-rail').scrollWidth,
  }));
  expect(width.page, 'the Home page itself must not scroll horizontally').toBeLessThanOrEqual(width.viewport);
  expect(width.pulse).toBeLessThanOrEqual(width.viewport);
  expect(width.railScroll, 'all six stages stay in the initial Home view').toBeLessThanOrEqual(width.railClient);
});
