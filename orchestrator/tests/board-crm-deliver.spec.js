// e2e for fase-1 step 3 (frontend half) — "deals die at won, delivery is a
// separate human act".
//
// The server half already landed: `delivered` is a retired stage (crm.py rejects
// it with `stage_retired`, m05's trigger ABORTs it at the storage engine), and
// the board's ✅ Delivered bucket is DERIVED (`stage='won'` + its project
// delivered). This spec pins the frontend consequences of that:
//
//   1. Dropping a deal into **Won** asks the delivery question FIRST. Today the
//      drop PATCHes straight through with no modal — that is the RED this spec
//      was written against, and the reason the first test asserts the stage is
//      still unchanged while the modal is open (a modal that opened *after* the
//      write would be theatre).
//   2. The three outcomes are real and distinguishable: Confirm links a project,
//      "Todavía no" leaves a deliberate won orphan, Cancel writes nothing.
//   3. 🚚 is reachable without a drag (card button + `?action=deliver` deep link).
//   4. `delivered` is gone from every DEAL stage picker — and still present on
//      the Speaking `tk-status` select, which is a different entity whose
//      vocabulary this change must not touch.
//
// Runs against the wiped-cycle DB COPY from playwright.config.js. It fabricates
// its own account + deals (the live pipeline decays: an idle deal auto-moves to
// `stalled`, so a spec routed through whatever happens to be on the board fails
// for reasons unrelated to delivery) and puts every one of them to `lost` in
// afterAll so the `?stage=won` fixture the drawer-crm spec reads stays clean.
//
// Run: `npx playwright test board-crm-deliver`, or
//      `PW_BASE_URL=http://127.0.0.1:8931 npx playwright test board-crm-deliver`.
const { test, expect } = require('@playwright/test');

const PIPE = '#crm-pipeline';
const wonList = (page) => page.locator(`${PIPE} .crm-stage-list[data-stage="won"]`);
const card = (page, id) => page.locator(`${PIPE} .crm-deal-card[data-deal-id="${id}"]`);
const MODAL = '#deliver-modal';

// SortableJS runs with forceFallback (simulated drag), so native dragTo never
// fires it — drive a real mouse. Press on the card TITLE, not its centre: the
// Sortable `filter` excludes buttons/selects, and the card's centre row is all
// buttons.
async function dragTo(page, handle, targetList) {
  const s = await handle.boundingBox();
  const d = await targetList.boundingBox();
  await page.mouse.move(s.x + s.width / 2, s.y + s.height / 2);
  await page.mouse.down();
  await page.mouse.move(s.x + s.width / 2, s.y + s.height / 2 + 12, { steps: 6 });
  await page.mouse.move(d.x + d.width / 2, d.y + 14, { steps: 20 });
  await page.mouse.move(d.x + d.width / 2, d.y + 16, { steps: 3 });
  await page.waitForTimeout(150);
  await page.mouse.up();
}

// Server truth for a deal, read through the same context endpoint the drawer
// uses (there is no single-deal REST GET).
async function dealState(request, id) {
  const res = await request.get(`/api/context/deal/${encodeURIComponent(id)}`);
  const e = (await res.json()).entity || {};
  return { stage: e.stage, project_id: e.project_id || null };
}

async function openCrm(page) {
  await page.setViewportSize({ width: 1900, height: 1000 });
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('crm'));
  await expect(page.locator('#content-crm')).toBeVisible();
  await expect(wonList(page)).toBeVisible();
}

// A column caps at CRM_CARD_CAP cards; expand it if our fixture fell past the cap.
async function revealCard(page, dealId, stage) {
  const c = card(page, dealId);
  if (await c.count() === 0) await page.evaluate((s) => crmToggleStage(s), stage);
  await expect(c).toBeVisible();
  await c.scrollIntoViewIfNeeded();
  return c;
}

test.describe('Won-drop → Entregar (fase 1, step 3)', () => {
  let accountId;
  const deals = {};          // label → deal id

  test.beforeAll(async ({ request }) => {
    const acc = await (await request.post('/api/crm/accounts', {
      data: { name: `E2E Entregar ${Date.now()}`, notes: 'playwright fixture' },
    })).json();
    accountId = acc.account_id || acc.id;
    expect(accountId, 'fixture account was created').toBeTruthy();

    for (const [label, stage] of [
      ['opensModal', 'proposal'], ['cancel', 'proposal'],
      ['later', 'proposal'], ['confirm', 'proposal'], ['cardBtn', 'won'],
      ['deepLink', 'won'],
    ]) {
      const d = await (await request.post('/api/crm/deals', {
        data: { account_id: accountId, title: `E2E deliver · ${label}`, stage, value: 1000 },
      })).json();
      deals[label] = d.deal_id;
      expect(deals[label], `fixture deal ${label}`).toBeTruthy();
    }
  });

  test.afterAll(async ({ request }) => {
    // Park every fixture deal in `lost` so `/api/crm/deals?stage=won` (which
    // drawer-crm reads first) is not shadowed by this spec's leftovers, and
    // archive the project the Confirm test minted.
    for (const id of Object.values(deals)) {
      if (id) await request.patch(`/api/crm/deals/${id}`, { data: { stage: 'lost', lost_reason: 'other' } });
    }
    const projects = (await (await request.get('/api/projects')).json()).projects || [];
    for (const p of projects) {
      if ((p.name || '').startsWith('E2E Entregar ')) {
        await request.post(`/api/projects/${p.id}/archive`).catch(() => {});
      }
    }
  });

  // ---- RED-PROOF ---------------------------------------------------------
  // Against the pre-change template this fails twice over: no modal appears at
  // all, and the PATCH has already landed by the time we look.
  test('dropping a deal into Won asks the delivery question BEFORE writing', async ({ page, request }) => {
    const id = deals.opensModal;
    await openCrm(page);
    const c = await revealCard(page, id, 'proposal');
    await dragTo(page, c.locator('.font-medium').first(), wonList(page));

    await expect(page.locator(MODAL)).toBeVisible();
    // The question comes first: nothing is written while it is open.
    expect((await dealState(request, id)).stage).toBe('proposal');
    // And it opens already answered — zero required decisions.
    expect(await page.locator('#deliver-project').inputValue()).not.toBe('');

    await page.locator('#deliver-cancel-btn').click();
  });

  test('Cancel reverts — the deal never leaves its column', async ({ page, request }) => {
    const id = deals.cancel;
    await openCrm(page);
    const c = await revealCard(page, id, 'proposal');
    await dragTo(page, c.locator('.font-medium').first(), wonList(page));

    await expect(page.locator(MODAL)).toBeVisible();
    await page.locator('#deliver-cancel-btn').click();
    await expect(page.locator(MODAL)).toBeHidden();

    expect(await dealState(request, id)).toEqual({ stage: 'proposal', project_id: null });
    // loadCRM() re-rendered from server truth: the card is back where it was.
    await expect(page.locator(`${PIPE} .crm-stage-list[data-stage="proposal"] .crm-deal-card[data-deal-id="${id}"]`))
      .toHaveCount(1);
  });

  test('"Todavía no" wins the deal and leaves a deliberate orphan', async ({ page, request }) => {
    const id = deals.later;
    await openCrm(page);
    const c = await revealCard(page, id, 'proposal');
    await dragTo(page, c.locator('.font-medium').first(), wonList(page));

    await expect(page.locator(MODAL)).toBeVisible();
    await page.locator('#deliver-later-btn').click();
    await expect(page.locator(MODAL)).toBeHidden();

    await expect.poll(() => dealState(request, id).then(s => s.stage)).toBe('won');
    expect((await dealState(request, id)).project_id).toBeNull();
  });

  test('Confirm wins the deal AND links the delivering project', async ({ page, request }) => {
    const id = deals.confirm;
    await openCrm(page);
    const c = await revealCard(page, id, 'proposal');
    await dragTo(page, c.locator('.font-medium').first(), wonList(page));

    await expect(page.locator(MODAL)).toBeVisible();
    await page.locator('#deliver-confirm-btn').click();
    await expect(page.locator(MODAL)).toBeHidden();

    await expect.poll(() => dealState(request, id).then(s => s.stage)).toBe('won');
    const after = await dealState(request, id);
    expect(after.project_id).toBeTruthy();   // the spine row this whole step exists for
  });

  test('a won orphan carries 🚚 on its card, and it opens the modal', async ({ page }) => {
    const id = deals.cardBtn;
    await openCrm(page);
    await revealCard(page, id, 'won');
    const btn = card(page, id).locator('.crm-deliver-btn');
    await expect(btn).toBeVisible();
    await btn.click();
    await expect(page.locator(MODAL)).toBeVisible();
    await page.locator('#deliver-cancel-btn').click();
    await expect(page.locator(MODAL)).toBeHidden();
  });

  test('?entity=deal:X&action=deliver opens the drawer with the modal armed', async ({ page }) => {
    const id = deals.deepLink;
    await page.goto(`/?entity=deal:${id}&action=deliver`);
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#entity-drawer')).toBeVisible();
    await expect(page.locator('#ed-kind')).toHaveText('deal');
    await expect(page.locator(MODAL)).toBeVisible();
  });
});

test.describe('`delivered` retired from the deal vocabulary', () => {
  test('no DEAL stage picker offers it; the Speaking one still does', async ({ page, request }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // The deal edit modal's stage select.
    await expect(page.locator('#de-stage option[value="delivered"]')).toHaveCount(0);
    // The Speaking engagement status — a DIFFERENT entity, whose vocabulary
    // legitimately ends at `delivered`. This is the guard against a blanket
    // find-and-replace.
    await expect(page.locator('#tk-status option[value="delivered"]')).toHaveCount(1);

    // The sub-deal select is built from a JS template literal, so the DOM cannot
    // see it until the form is opened — assert on the served template instead.
    // Exactly ONE `delivered` option may survive in the whole document: Speaking.
    const src = await (await request.get('/')).text();
    const hits = (src.match(/<option value="delivered">/g) || []).length;
    expect(hits, 'only the Speaking tk-status option may keep `delivered`').toBe(1);
  });
});
