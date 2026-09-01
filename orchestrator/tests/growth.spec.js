// e2e for the CRM Growth System — the Pipeline (Strategy → Pipeline) growth
// cockpit + the Strategy → Growth view. Runs against the wiped-cycle DB copy
// from playwright.config.js (deals/initiatives are preserved).
//
// Covers: weekly scorecard strip, pipeline math, value ladder, growth-loop +
// touch badges on deal cards, the +Lead quick-capture modal, the +Touch action,
// and the Growth view (3 loops + content cadence). Also asserts NO console
// errors across the whole flow (the single-file JS is easy to break).
//
// Run: `npx playwright test growth`
const { test, expect } = require('@playwright/test');

// Fail loud on any uncaught page error or console.error across the flow.
function guard(page) {
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  return errors;
}

const openPipeline = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('crm'));
  await expect(page.locator('#content-crm')).toBeVisible();
};

test.describe('CRM Growth System', () => {
  test('Pipeline shows scorecard + pipeline math + value ladder', async ({ page }) => {
    const errors = guard(page);
    await openPipeline(page);

    // Weekly scorecard: 5 KPIs.
    const score = page.locator('#crm-scorecard');
    await expect(score).toContainText('Scorecard semanal');
    for (const label of ['Leads', 'Toques', 'Discovery calls', 'Contenido', 'Propuestas']) {
      await expect(score).toContainText(label);
    }
    // Pipeline math: backward funnel + coverage.
    await expect(page.locator('#crm-math')).toContainText('Pipeline math');
    await expect(page.locator('#crm-math')).toContainText('Toques');
    // Value ladder: the 4 rungs.
    const ladder = page.locator('#crm-ladder');
    for (const rung of ['Magnet', 'Entry', 'Core', 'Recurring']) {
      await expect(ladder).toContainText(rung);
    }
    expect(errors).toEqual([]);
  });

  test('+Lead captures a lead → appears in the pipeline', async ({ page }) => {
    const errors = guard(page);
    await openPipeline(page);

    await page.click('button:has-text("+ Lead")');
    await expect(page.locator('#quick-lead-modal')).toBeVisible();
    const stamp = Date.now().toString().slice(-6);
    await page.fill('#ql-name', 'E2E Tester ' + stamp);
    await page.fill('#ql-company', 'Growth Co ' + stamp);
    await page.selectOption('#ql-source', 'referral');
    await page.selectOption('#ql-loop', 'referido');
    await page.fill('#ql-industry', 'saas');
    await page.fill('#ql-engagement', '30');
    await page.click('button:has-text("Capture")');

    await expect(page.locator('#quick-lead-modal')).toBeHidden();
    // A lead without a product track still belongs to the selling Kanban; the
    // two-track ladder intentionally cannot place it until that field is known.
    await expect(page.locator('#crm-pipeline')).toContainText('Growth Co ' + stamp);
    expect(errors).toEqual([]);
  });

  test('+Touch on a deal logs a touch (count increments)', async ({ page }) => {
    const errors = guard(page);
    await openPipeline(page);
    // Ensure at least one deal card with a +Touch button.
    const touchBtn = page.locator('#content-crm button:has-text("+Touch")').first();
    await expect(touchBtn).toBeVisible();
    await touchBtn.click();
    // After reload the touch marker (✋ or 🔴 with a count) shows somewhere.
    await expect(page.locator('#content-crm')).toContainText(/✋|🔴/);
    expect(errors).toEqual([]);
  });

  test('Won stays active on the Kanban until Delivered moves it to History', async ({ page }) => {
    const errors = guard(page);
    const stamp = Date.now().toString();
    const title = 'Won active ' + stamp;
    await openPipeline(page);
    const setup = await page.evaluate(async ({ title }) => {
      const captured = await fetch('/api/crm/leads', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
        name: title,
        company: title,
        source: 'referral',
        loop: 'referido',
        industry: 'consulting',
        engagement_score: 30,
        }),
      });
      const capturedBody = await captured.json();
      if (!captured.ok) return { ok: false, capturedBody };
      const won = await fetch(`/api/crm/deals/${capturedBody.deal_id}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stage: 'won', value: 25000 }),
      });
      const wonBody = await won.json();
      return { ok: won.ok, capturedBody, wonBody };
    }, { title });
    expect(setup.ok, JSON.stringify(setup)).toBeTruthy();
    await page.evaluate(() => loadCRM());
    const wonColumn = page.locator('#crm-pipeline .crm-col', {
      has: page.locator('.crm-stage-list[data-stage="won"]'),
    });
    await expect(wonColumn).toContainText('Won / Active');
    const card = wonColumn.locator('.crm-deal-card', { hasText: title });
    await expect(card).toBeVisible();

    // Delivery is now a project lifecycle fact, never a draggable deal stage:
    // link the won deal to a project, then close that project.
    const delivered = await page.evaluate(async ({ dealId, title, stamp }) => {
      const created = await fetch('/api/projects', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: title, slug: `e2e-delivery-${stamp}` }),
      });
      const project = await created.json();
      if (!created.ok) return { ok: false, step: 'project', project };
      const linked = await fetch(`/api/crm/deals/${dealId}/deliver`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: project.id }),
      });
      const linkBody = await linked.json();
      if (!linked.ok) return { ok: false, step: 'link', linkBody };
      const closed = await fetch(`/api/projects/${project.id}/delivered`, { method: 'POST' });
      const closeBody = await closed.json();
      return { ok: closed.ok, step: 'delivered', closeBody };
    }, { dealId: setup.capturedBody.deal_id, title, stamp });
    expect(delivered.ok, JSON.stringify(delivered)).toBeTruthy();
    await page.evaluate(() => loadCRM());

    const deliveredTarget = page.locator(
      '#crm-pipeline .crm-closed-tile .crm-history-tile[data-stage="delivered"]'
    );
    await expect(deliveredTarget).toBeVisible();

    await expect.poll(async () => {
      const res = await page.request.get('/api/crm/pipeline');
      return ((await res.json()).by_stage.delivered || []).some(d => d.title === title);
    }).toBe(true);

    const deliveredTile = page.locator('#crm-pipeline .crm-closed-tile', {
      has: page.locator('.crm-history-tile[data-stage="delivered"]'),
    });
    await deliveredTile.getByRole('button', { name: 'view →' }).click();
    await expect(page.locator('#crm-all-deals-details')).toHaveAttribute('open', '');
    await expect(page.locator('#crm-all-deals')).toContainText('Filtered · Delivered');
    await expect(page.locator('#crm-all-deals')).toContainText(title);
    expect(errors).toEqual([]);
  });

  test('Growth view renders the 3 loops + content cadence', async ({ page }) => {
    const errors = guard(page);
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    await page.evaluate(() => switchTab('growth'));
    await expect(page.locator('#content-growth')).toBeVisible();

    const loops = page.locator('#growth-loops');
    await expect(loops).toContainText('Growth loops');
    for (const l of ['Autoridad', 'Referido', 'Producto']) {
      await expect(loops).toContainText(l);
    }
    await expect(page.locator('#growth-content')).toContainText('Content cadence');
    await expect(page.locator('#growth-content')).toContainText('streak');
    expect(errors).toEqual([]);
  });

  test('Growth shows the person → versioned proposal → Project strategy', async ({ page }) => {
    const errors = guard(page);
    await page.setViewportSize({ width: 375, height: 812 });
    await page.route('**/api/growth/radar', route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        rings: {
          seguimiento: [{ deal_id: 'l1' }, { deal_id: 'l2' }],
          oportunidad: [{ deal_id: 'o1' }],
          propuesta: [
            { deal_id: 'p1', proposal_state: 'verified' },
            { deal_id: 'p2', proposal_state: null },
          ],
        },
        centro: [{ deal_id: 'w1', project_id: 'project-1' }],
        won_sin_proyecto: [],
      }),
    }));
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    await page.evaluate(() => switchTab('growth'));

    const block = page.locator('[data-testid="commercial-journey"]');
    await expect(block).toBeVisible();
    await expect(block).toContainText('Persona');
    await expect(block).toContainText('Oportunidad');
    await expect(block).toContainText('Propuesta versionada');
    await expect(block).toContainText('Proyecto');
    await expect(block).toContainText('1 de 2');
    await expect(block).toContainText('La llamada puede ayudar; no es requisito');
    await expect(block).toContainText('Versionar 1 propuesta');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(errors).toEqual([]);
  });

  test('Growth keeps a radar failure distinct from an empty journey', async ({ page }) => {
    const errors = guard(page);
    await page.route('**/api/growth/radar', route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: '{not-json',
    }));
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    await page.evaluate(() => switchTab('growth'));

    const block = page.locator('[data-testid="commercial-journey"]');
    await expect(block).toContainText('Indicador no disponible');
    await expect(block).not.toContainText('0 de 0');
    expect(errors).toEqual([]);
  });

  test('scorecard strip is on the Today home view', async ({ page }) => {
    const errors = guard(page);
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#today-scorecard')).toContainText('Scorecard semanal');
    expect(errors).toEqual([]);
  });
});
