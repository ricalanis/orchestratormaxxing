// e2e for P0-9: CRM deal rows open the shared entity drawer (P0-4), and the
// Deal → Initiative → Task chain is clickable all the way down. Also covers the
// per-row initiative chip (opens the initiative directly, not the deal) and the
// ?entity= deep-link + browser Back. Runs against the wiped-cycle DB copy from
// playwright.config.js (deals/initiatives/tasks are preserved — only cycles are
// wiped — so the seeded Eduardo deal chain is present).
//
// Run: `npx playwright test drawer-crm` (webServer auto-starts one), or
//      `PW_BASE_URL=http://127.0.0.1:8931 npx playwright test drawer-crm`.
const { test, expect } = require('@playwright/test');

const DRAWER = '#entity-drawer';
const KIND = '#ed-kind';
// Drawer child/ancestor refs are <button onclick="openEntity('<type>','<id>')">.
const childBtn = (type) => `${DRAWER} #ed-body button[onclick*="openEntity('${type}'"]`;

const openCrm = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');   // let window.onload → switchTab('today') settle
  await page.evaluate(() => switchTab('crm'));
  await expect(page.locator('#content-crm')).toBeVisible();
  await expect(page.locator('#crm-pipeline [onclick^="crmDrill"]').first()).toBeVisible();
};

// The deal card that carries an initiative (its violet 🎯 chip → title^="Open initiative:").
const dealWithInitiative = (page) =>
  page.locator('#crm-pipeline [onclick^="crmDrill"]', {
    has: page.locator('[title^="Open initiative:"]'),
  }).first();

test.describe('CRM → entity drawer (P0-9)', () => {
  test('deal row opens the drawer; Deal → Initiative → Task all clickable', async ({ page }) => {
    await openCrm(page);

    // Click the deal card TITLE (bubbles to the card's crmDrill, not the chip).
    await dealWithInitiative(page).locator('.font-medium').first().click();

    // Opens on the DEAL.
    await expect(page.locator(DRAWER)).toBeVisible();
    await expect(page.locator(KIND)).toHaveText('deal');

    // Deal drawer → clickable INITIATIVE child → drawer switches to the initiative.
    await expect(page.locator(childBtn('initiative')).first()).toBeVisible();
    await page.locator(childBtn('initiative')).first().click();
    await expect(page.locator(KIND)).toHaveText('initiative');

    // Initiative drawer → clickable TASK child → drawer switches to the task.
    await expect(page.locator(childBtn('task')).first()).toBeVisible();
    await page.locator(childBtn('task')).first().click();
    await expect(page.locator(KIND)).toHaveText('task');

    // Esc closes the drawer and clears the ?entity= param.
    await page.keyboard.press('Escape');
    await expect(page.locator(DRAWER)).toBeHidden();
    expect(new URL(page.url()).searchParams.get('entity')).toBeNull();
  });

  test('the initiative chip on a deal row opens the INITIATIVE drawer directly', async ({ page }) => {
    await openCrm(page);
    // stopPropagation on the chip means it opens the initiative, NOT the deal.
    await page.locator('#crm-pipeline [title^="Open initiative:"]').first().click();
    await expect(page.locator(DRAWER)).toBeVisible();
    await expect(page.locator(KIND)).toHaveText('initiative');
  });

  test('deep-link ?entity=deal:… opens the drawer; Back closes it', async ({ page }) => {
    await openCrm(page);
    const onclick = await page.locator('#crm-pipeline [onclick^="crmDrill"]').first().getAttribute('onclick');
    const dealId = onclick.match(/crmDrill\('([^']+)'\)/)[1];

    await page.goto(`/?entity=deal:${dealId}`);
    await page.waitForLoadState('networkidle');
    await expect(page.locator(DRAWER)).toBeVisible();
    await expect(page.locator(KIND)).toHaveText('deal');

    await page.goBack();
    await expect(page.locator(DRAWER)).toBeHidden();
  });
});

// ---------------------------------------------------------------------------
// Step 6 — "Deliver this" + the drawer spine.
//
// The deal was the most commercially important entity in the product with a
// DEAD action footer: context.py declared ["advance","event"] and
// edActionsHtml() fell through to ''. The first test here is the RED one — it
// fails against the pre-fix template, which is the proof it tests the fix and
// not the fixture.
//
// These are read-only on purpose: the Deliver modal is opened and cancelled,
// never confirmed. The e2e server runs against a temp copy, but a spec that
// mutates its own fixture is a spec whose second run tests something else.
//
// These deep-link rather than clicking through the CRM board on purpose: the
// board's fixture is live data whose stages decay (auto_stale_decay moves an
// idle deal to `stalled`, and the icebox column is collapsed), so a drawer
// contract routed through it would fail for reasons that have nothing to do
// with the drawer.
const dealIdAtStage = async (request, stage) => {
  const res = await request.get(`/api/crm/deals${stage ? `?stage=${stage}` : ''}`);
  const deals = (await res.json()).deals || [];
  return deals.length ? deals[0].id : null;
};

const openDeal = async (page, dealId) => {
  await page.goto(`/?entity=deal:${dealId}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator(DRAWER)).toBeVisible();
  await expect(page.locator(KIND)).toHaveText('deal');
};

test.describe('Deal actions + drawer spine (step 6)', () => {
  test('the deal drawer action footer is not dead', async ({ page, request }) => {
    const dealId = await dealIdAtStage(request, null);
    test.skip(!dealId, 'no deal in the e2e DB copy');
    await openDeal(page, dealId);

    // The whole point: a deal has actions. Before this step the footer was
    // empty for every deal in the product — the most commercially important
    // entity had a dead action footer.
    const actions = page.locator(`${DRAWER} #ed-actions button`);
    await expect(actions.first()).toBeVisible();
    expect(await actions.count()).toBeGreaterThan(0);

    // And the stage stepper IS the "advance stage" verb: the deal's own stage is
    // always rendered as a step, so moving it is one click and no typing.
    const stage = (await page.locator(`${DRAWER} #ed-sub`).innerText()).split('·')[0].trim();
    await expect(page.locator(`${DRAWER} #ed-actions [data-stage="${stage}"]`)).toHaveCount(1);
  });

  test('a won deal offers "Deliver this", and the picker opens with a default', async ({ page, request }) => {
    const dealId = await dealIdAtStage(request, 'won');
    test.skip(!dealId, 'no won deal in the e2e DB copy');
    await openDeal(page, dealId);

    const deliver = page.locator(`${DRAWER} #ed-deliver-btn`);
    await expect(deliver).toBeVisible();
    await deliver.click();

    // Zero required decisions: the modal opens already answered.
    const modal = page.locator('#deliver-modal');
    await expect(modal).toBeVisible();
    const picked = await page.locator('#deliver-project').inputValue();
    expect(picked).not.toBe('');
    if (picked === '__new__') {
      // No existing project for this client → prefilled "create «client»".
      expect((await page.locator('#deliver-new-name').inputValue()).trim()).not.toBe('');
    }
    await expect(page.locator('#deliver-confirm-btn')).toBeEnabled();

    // Cancel — this spec never fires the conversion verb.
    await page.locator('#deliver-cancel-btn').click();
    await expect(modal).toBeHidden();
  });

  test('the account breadcrumb no longer dead-ends', async ({ page, request }) => {
    const dealId = await dealIdAtStage(request, 'won');
    test.skip(!dealId, 'no won deal in the e2e DB copy');
    await openDeal(page, dealId);

    // context.py used to mark the account ancestor clickable:false, so the top of
    // the chain rendered as a <span> and taught the operator not to click breadcrumbs.
    const account = page.locator('#ed-crumb button').first();
    await expect(account).toBeVisible();
    await account.click();
    await expect(page.locator(KIND)).toHaveText('account');
    // The client view is the lateral one: its deals hang off it.
    await expect(page.locator(childBtn('deal')).first()).toBeVisible();
  });
});
