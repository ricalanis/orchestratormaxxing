// e2e for the CRM pipeline drag GESTURE (not the stage semantics — those live in
// board-crm-deliver.spec.js). Three of the operator's verbatim complaints, each turned
// into a reproduction:
//
//   1. "se selecciona el texto cuando quiero arrastrar y cambia la intención"
//      → pressing on a deal card and moving must leave the selection EMPTY.
//        SortableJS 1.15 runs forceFallback here, so there is no native HTML5 drag
//        to suppress selection, and Sortable ships zero user-select handling — it
//        clears the range once inside _triggerDragStart, after fallbackTolerance,
//        while the button is still down, so the selection just re-extends. The fix
//        is CSS (user-select:none on .crm-deal-card); this pins it.
//
//   2. "difícil poner oportunidades en la parte baja de la columna"
//      → Sortable is bound to .crm-stage-list, never to .crm-col. The pipeline is
//        a flex row with items-stretch, so every column is as tall as the TALLEST
//        one, while a short column's list stopped at its content. `min-h-[3rem]`
//        does not help: it is an arbitrary tailwind value and dashboard/static/
//        tailwind.css is a curated subset with NO arbitrary values, so that class
//        resolves to nothing. Result: the bottom of a short column looked
//        droppable and silently was not. Tests 2 + 3 pin the geometry AND a real
//        drop at the bottom edge.
//
//   3. "la puerta con letrero" — the ✅ Entregado tile is deliberately NOT a drop
//      target (crmClosedTile renders `crm-history-tile`, not `crm-stage-list`), so
//      a deal dropped on it used to snap back in total silence. It must STAY a
//      non-target (the stage is retired server-side and the write could only 400)
//      but it must now SAY so. Test 4 pins the sign, and that no stage moved.
//
// Own-your-state (lq-4c53d622): fabricates its own account + deals and parks every
// one of them in `lost` in afterAll, so `/api/crm/deals?stage=won` stays clean for
// the sibling specs. It never asserts on ambient pipeline counts.
//
// NB: never POST /api/tasks from an e2e spec — that route shells out to the
// `hermes` CLI, which does NOT honour $HERMES_KANBAN_DB and would write to the
// operator's REAL ~/.hermes/kanban.db. Deals are safe (pure Python, temp DB).
//
// Run: `npx playwright test crm-drag-gesture`.
const { test, expect } = require('@playwright/test');

const PIPE = '#crm-pipeline';
const stageList = (page, s) => page.locator(`${PIPE} .crm-stage-list[data-stage="${s}"]`);
const stageCol = (page, s) => page.locator(`${PIPE} .crm-col`).filter({ has: page.locator(`.crm-stage-list[data-stage="${s}"]`) });
const card = (page, id) => page.locator(`${PIPE} .crm-deal-card[data-deal-id="${id}"]`);
const deliveredTile = (page) => page.locator(`${PIPE} .crm-history-tile[data-stage="delivered"]`);

// The tile is the right-most thing on a horizontally scrolling row and the whole
// pipeline sits well below the fold, so nothing is measurable until it is scrolled
// in. The extra -200px matters as much as the scroll itself: the page's sticky
// chrome is TWO stacked elements (<header> to ~77px, then #tabs-sticky to ~141px),
// and scrollIntoViewIfNeeded happily parks the stage row UNDER that band. A press
// there lands on the sticky bar — elementFromPoint returns the header, not the card
// — so Sortable never starts and the whole test silently measures nothing.
async function showPipeline(page) {
  await expect(deliveredTile(page)).toBeVisible();
  await deliveredTile(page).scrollIntoViewIfNeeded();
  await page.evaluate(() => window.scrollBy(0, -200));
  await page.waitForTimeout(250);
}

// Guard against the vacuous pass: assert the press point really is the card, and
// that Sortable really took the gesture. Without this a test that never started a
// drag reports "no text was selected" and looks green.
async function assertDragLive(page, dealId) {
  const state = await page.evaluate((id) => ({
    dragging: document.body.classList.contains('dragging'),
    chosen: !!document.querySelector(`.crm-deal-card[data-deal-id="${id}"].kanban-chosen`)
         || !!document.querySelector('.crm-deal-card.kanban-chosen'),
  }), dealId);
  expect(state.dragging, 'SortableJS actually started the drag (else this test proves nothing)').toBe(true);
}

async function openCrm(page) {
  // Wide + tall on purpose: #crm-pipeline is a horizontally scrolling flex row and
  // its columns stretch to the tallest one, so at 1900×1000 the ✅ tile sits both
  // off to the right AND ~2800px down the page — a mouse driver can never reach it.
  await page.setViewportSize({ width: 2400, height: 1300 });
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('crm'));
  await expect(page.locator('#content-crm')).toBeVisible();
  await expect(stageList(page, 'won')).toBeVisible();   // `won` is always a full column
}

// A stage column caps at CRM_CARD_CAP cards; expand it if our fixture fell past it.
async function revealCard(page, dealId, stage) {
  const c = card(page, dealId);
  if (await c.count() === 0) await page.evaluate((s) => crmToggleStage(s), stage);
  await expect(c).toBeVisible();
  await c.scrollIntoViewIfNeeded();
  return c;
}

// Server truth (there is no single-deal REST GET).
async function dealState(request, id) {
  const res = await request.get(`/api/context/deal/${encodeURIComponent(id)}`);
  const e = (await res.json()).entity || {};
  return { stage: e.stage, project_id: e.project_id || null };
}

// Press the card's TITLE (the Sortable filter excludes buttons/selects, and the
// card's lower rows are all buttons), then walk to an explicit point. forceFallback
// means native dragTo never fires Sortable — a real mouse is the only driver.
async function pressAndDragTo(page, handle, x, y, opts = {}) {
  const s = await handle.boundingBox();
  const sx = s.x + s.width / 2, sy = s.y + s.height / 2;
  await page.mouse.move(sx, sy);
  await page.mouse.down();
  await page.mouse.move(sx, sy + 12, { steps: 6 });
  // Fail loudly here rather than later: if the press missed the card (sticky
  // chrome, off-viewport coords), every assertion downstream would be vacuous.
  expect(await page.evaluate(() => document.body.classList.contains('dragging')),
         'SortableJS took the gesture (else the press missed the card)').toBe(true);
  if (opts.via === 'L') await page.mouse.move(sx, y, { steps: 14 });   // descend first, then cross
  await page.mouse.move(x, y, { steps: 20 });
  await page.mouse.move(x, y + 2, { steps: 3 });
  await page.waitForTimeout(150);
  await page.mouse.up();
}

test.describe('CRM pipeline drag gesture', () => {
  let accountId;
  const deals = {};

  test.beforeAll(async ({ request }) => {
    const acc = await (await request.post('/api/crm/accounts', {
      data: { name: `E2E DragGesture ${Date.now()}`, notes: 'playwright fixture' },
    })).json();
    accountId = acc.account_id || acc.id;
    expect(accountId, 'fixture account was created').toBeTruthy();

    // Several in `proposal` so that column is TALL, and one in `qualified` so that
    // column renders as a real column (an empty selling stage collapses to a rail,
    // which carries no .crm-stage-list and is therefore not a drop target at all).
    const seed = [
      ['tall1', 'proposal'], ['tall2', 'proposal'], ['tall3', 'proposal'],
      ['selection', 'proposal'], ['bottomDrop', 'proposal'], ['doorSign', 'proposal'],
      ['anchor', 'qualified'],
      // Won with no delivering project — the one case where the sign is also a door.
      ['doorSignWon', 'won'],
    ];
    for (const [label, stage] of seed) {
      const d = await (await request.post('/api/crm/deals', {
        data: { account_id: accountId, title: `E2E drag · ${label}`, stage, value: 1000 },
      })).json();
      deals[label] = d.deal_id;
      expect(deals[label], `fixture deal ${label}`).toBeTruthy();
    }
  });

  test.afterAll(async ({ request }) => {
    for (const id of Object.values(deals)) {
      if (id) await request.patch(`/api/crm/deals/${id}`, { data: { stage: 'lost', lost_reason: 'other' } });
    }
  });

  // ---- RED-PROOF ---------------------------------------------------------
  // Against the pre-fix template this fails: with no user-select:none on
  // .crm-deal-card, mousedown + move selects the card's title text.
  test('starting a drag on a deal card selects NO text', async ({ page }) => {
    await openCrm(page);
    await showPipeline(page);
    const c = await revealCard(page, deals.selection, 'proposal');
    const handle = c.locator('.font-medium').first();

    const s = await handle.boundingBox();
    await page.mouse.move(s.x + s.width / 2, s.y + s.height / 2);
    await page.mouse.down();
    // Well past fallbackTolerance (4px) — this is the window in which the browser's
    // native selection gesture used to run and win.
    await page.mouse.move(s.x + s.width / 2 + 40, s.y + s.height / 2 + 30, { steps: 12 });
    await page.mouse.move(s.x + s.width / 2 + 80, s.y + s.height / 2 + 60, { steps: 12 });

    // The gesture is genuinely a drag — otherwise "nothing got selected" is trivia.
    await assertDragLive(page, deals.selection);
    const sel = await page.evaluate(() => String(window.getSelection() || ''));
    await page.mouse.up();
    expect(sel, 'drag gesture must not select card text').toBe('');
  });

  // ---- RED-PROOF ---------------------------------------------------------
  // Pre-fix, a short column's list stopped at its content while the column
  // stretched to the tallest sibling — the gap was the dead zone the operator hit.
  test('every stage list FILLS its column — no dead strip at the bottom', async ({ page }) => {
    await openCrm(page);
    const gaps = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll('#crm-pipeline .crm-col').forEach((col) => {
        const list = col.querySelector(':scope > .crm-stage-list');
        if (!list) return;
        const cb = col.getBoundingClientRect(), lb = list.getBoundingClientRect();
        // Anything after the list (the "+ N more" button) legitimately owns space —
        // MARGINS included, or the column's own padding reads as a dead strip.
        let after = 0;
        for (let n = list.nextElementSibling; n; n = n.nextElementSibling) {
          const cs = getComputedStyle(n);
          after += n.getBoundingClientRect().height
                 + parseFloat(cs.marginTop) + parseFloat(cs.marginBottom);
        }
        out.push({ stage: list.getAttribute('data-stage'), gap: cb.bottom - lb.bottom - after });
      });
      return out;
    });
    expect(gaps.length, 'at least one full stage column is on screen').toBeGreaterThan(0);
    // Tolerance covers the column's own bottom padding (p-3 = 12px) + subpixel.
    // Pre-fix this gap ran to hundreds of px, so the bound is not delicate.
    for (const g of gaps) expect(g.gap, `dead strip under the ${g.stage} column`).toBeLessThanOrEqual(16);
  });

  // ---- RED-PROOF ---------------------------------------------------------
  // The user-facing half of the same defect: a drop aimed at the BOTTOM EDGE of a
  // short target column. Pre-fix that point sat outside .crm-stage-list, so
  // Sortable never adopted it and the card snapped home with no stage change.
  test('a deal dropped at the BOTTOM of a column lands in that stage', async ({ page, request }) => {
    await openCrm(page);
    const id = deals.bottomDrop;
    expect((await dealState(request, id)).stage).toBe('proposal');

    await showPipeline(page);
    const c = await revealCard(page, id, 'proposal');

    // Choose the target from MEASURED slack, never a hard-coded stage: the live
    // pipeline decides how tall each column's content is, and a stage that happens
    // to be full today has no dead strip at all — a test pinned to it would pass
    // with the bug intact (which is exactly how the older board-* specs rotted).
    // Win/lost/history stages are excluded: dropping there opens an intercept modal.
    const target = await page.evaluate(() => {
      const cols = [...document.querySelectorAll('#crm-pipeline .crm-col')].map((col) => {
        const list = col.querySelector(':scope > .crm-stage-list');
        if (!list) return null;
        const cards = list.querySelectorAll('.crm-deal-card');
        const last = cards[cards.length - 1];
        const cb = col.getBoundingClientRect();
        const contentBottom = last ? last.getBoundingClientRect().bottom
                                   : list.getBoundingClientRect().top;
        return { stage: list.getAttribute('data-stage'), contentBottom,
                 colBottom: cb.bottom, x: cb.x + cb.width / 2, slack: cb.bottom - contentBottom };
      }).filter(Boolean).filter((c) => !['won', 'lost', 'delivered', 'stalled', 'proposal'].includes(c.stage));
      cols.sort((a, b) => b.slack - a.slack);
      return cols[0] || null;
    });
    expect(target, 'a stage column with a lower dead strip exists').toBeTruthy();
    expect(target.slack, 'the chosen column really has empty body below its cards').toBeGreaterThan(150);

    // Aim BELOW the target column's last card — the strip that looked droppable and
    // was not. Well past Sortable's 24px emptyInsertThreshold.
    const dropY = Math.min(target.contentBottom + 120, target.colBottom - 10, 1250);

    // Route the pointer DOWN first, then ACROSS at that depth (an L, not a
    // diagonal). A straight line clips the TOP of the target list on the way —
    // where cards already live — so Sortable adopts the card there and the drop
    // "works" even with the dead strip intact. The L enters the target column only
    // at the depth under test, which is what makes this a real discriminator.
    await pressAndDragTo(page, c.locator('.font-medium').first(), target.x, dropY, { via: 'L' });

    await expect.poll(async () => (await dealState(request, id)).stage,
                      { timeout: 8000 }).toBe(target.stage);
    await expect(stageList(page, target.stage).locator(`.crm-deal-card[data-deal-id="${id}"]`)).toBeVisible();
  });

  // ---- RED-PROOF ---------------------------------------------------------
  // Pre-fix the ✅ tile swallowed the gesture in silence. It must stay a
  // non-target, but explain itself — and never move the stage.
  test('dragging onto the ✅ Entregado tile teaches instead of silently ignoring', async ({ page, request }) => {
    await openCrm(page);
    const id = deals.doorSign;
    const before = (await dealState(request, id)).stage;

    await showPipeline(page);
    const c = await revealCard(page, id, 'proposal');
    const t = await deliveredTile(page).boundingBox();

    await pressAndDragTo(page, c.locator('.font-medium').first(),
                         t.x + t.width / 2, Math.min(t.y + 200, t.y + t.height / 2));

    // The sign is up, in Spanish, naming the real path.
    await expect(page.locator('#toast-stack'))
      .toContainText('la entrega se marca en el proyecto', { timeout: 5000 });

    // …and the door stayed shut: no stage write.
    expect((await dealState(request, id)).stage).toBe(before);
  });

  // ---- RED-PROOF ---------------------------------------------------------
  // A won deal whose delivery is still pending is the one case where the sign can
  // also BE the door: the toast carries the real verb as a one-tap action.
  test('a won-with-delivery-pending deal is offered the Entregar modal in one tap', async ({ page, request }) => {
    await openCrm(page);
    const id = deals.doorSignWon;
    const before = await dealState(request, id);
    expect(before.stage, 'fixture is won').toBe('won');
    expect(before.project_id, 'fixture has no delivering project yet').toBeFalsy();

    await showPipeline(page);
    const c = await revealCard(page, id, 'won');
    const t = await deliveredTile(page).boundingBox();

    await pressAndDragTo(page, c.locator('.font-medium').first(),
                         t.x + t.width / 2, Math.min(t.y + 200, t.y + t.height / 2));

    const toastBtn = page.locator('#toast-stack button', { hasText: 'Entregar' });
    await expect(toastBtn).toBeVisible({ timeout: 5000 });

    // One tap opens the real verb…
    await toastBtn.click();
    await expect(page.locator('#deliver-modal')).toBeVisible();
    await page.locator('#deliver-cancel-btn').click();

    // …and the drag itself still wrote nothing.
    expect((await dealState(request, id)).stage).toBe('won');
  });
});
