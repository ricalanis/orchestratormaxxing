// e2e contract for the commercial work that reaches the Today STREAM — what is
// left of this file after the Visión V1 deletion.
//
// It used to assert the dashboard's mirror of the 3x-daily Telegram brief: five
// blocks, in the brief's order, with the brief's headings. That mirror is GONE
// (💰 Dinero · 🤖 Agentes · 🧭 Último brief were deleted; the briefs still
// compose and send to Telegram), so the two contracts that pinned its markup —
// the block order/heading test and the per-block em-dash empty state — were
// deleted with it rather than rewritten: a test whose subject no longer exists
// is not a regression guard, it is a fossil. Their replacements live in
// `tests/today-horizon.spec.js`, which asserts the line that took their place
// AND that the blocks are really gone.
//
// What stayed here is everything that was never about the mirror:
//   3. a won orphan is a materialized 🚚 TASK the day planner can see, labelled
//      `why='cliente'` — the ⚠️ row's job, moved into the stream (journey fase 1
//      step 5). RED-PROOF: at HEAD~ POST /api/cadence/reconcile does not exist;
//  3b. the shelf actually RENDERS the fifth "Cliente / venta" band — a band
//      declared in BANDS but missing from the `buckets` literal would hide its
//      cards inside 'Active cycle' while every unit test stayed green;
//   4. the epic picker is GONE from the card menu. RED-PROOF: at HEAD~ the
//      selector in that test matches (the menu item exists and calls
//      openEpicPicker), so the assertion has failed against the bug it claims
//      to catch. See tests/README-style note in CLAUDE.md Tier-1c.
//
// Runs against the wiped-cycle DB copy from playwright.config.js. It composes a
// MIDDAY brief first: composing midday (the one slot with no write side effects;
// morning commits the day's plan) is what makes the seeded assertions
// deterministic. Idempotent per (date, slot).
//
// Run: `npx playwright test today-brief`.
const { test, expect } = require('@playwright/test');

// switchTab fires loadToday() asynchronously; settle it before asserting, or the
// test measures the race instead of the product (same helper as today-groups).
const openToday = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('today'));
  await expect.poll(() => page.evaluate(() => (typeof TODAY_DATA !== 'undefined' && TODAY_DATA) ? 1 : 0),
    { timeout: 15000 }).toBe(1);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(400);
};

// Compose the midday brief once per test — POST is idempotent per (date, slot),
// so a re-run returns the stored payload instead of composing twice.
test.beforeEach(async ({ request }) => {
  await request.post('/api/brief/midday');
});

// --- 3. the orphaned won deal is a TASK now (journey fase 1, step 5) --------
// The ⚠️ zone's job moved into the stream: `cadence.reconcile` mints
// "🚚 Entregar <deal> — $X" into proj_ventas, and the card reaches the day
// planner through `plan_candidates`' fourth source, labelled `why='cliente'`.
// So the assertion is no longer "the alert row has a button" but "the work is a
// card, and the planner can see it" — tareas-primero.
//
// The seeded DB is a copy of the real one, which carries 4 won deals with no
// delivering project, so the reconcile has something real to mint.
test('a won orphan becomes a 🚚 card the planner can see, in the cliente band', async ({ request }) => {
  const rec = await request.post('/api/cadence/reconcile', { data: {} });
  expect(rec.status(), await rec.text()).toBe(200);
  const out = await rec.json();
  expect(out.status).toBe('ok');

  const deliverCards = (out.minted || []).filter(m => m.kind === 'deliver');
  expect(deliverCards.length, 'the seeded DB has won deals with no project').toBeGreaterThan(0);
  expect(deliverCards[0].title).toContain('🚚 Entregar');

  // The card carries the deep link the ⚠️ row's button used to be.
  const got = await (await request.get(`/api/tasks/${deliverCards[0].task_id}`)).json();
  expect(got.task.body).toContain(`?entity=deal:${deliverCards[0].deal_id}&action=deliver`);
  expect(got.task.deal_id).toBe(deliverCards[0].deal_id);
  expect(got.task.stage_kind).toBe('entrega');

  // …and it reaches the day planner as a client candidate.
  // The SERVER's local date, never `toISOString()` — that is UTC, and this box
  // runs at UTC-6, so after 18:00 local the two disagree by a day and the card
  // (due today) reads as OVERDUE. Measured, not theorised: it is what made this
  // assertion fail at 23:30 CST while the product was correct.
  const today = (await (await request.get('/api/day-plan')).json()).date;
  const plan = await (await request.get(`/api/day-plan/candidates?date=${today}`)).json();
  const cands = (plan.candidates || []).filter(c => c.id === deliverCards[0].task_id);
  expect(cands.length, 'the 🚚 card must be a day-plan candidate').toBe(1);
  expect(cands[0].why).toBe('cliente');

  // Idempotent: a second pass mints nothing new (the storage engine refuses a
  // second open cadence card per deal, and the app never asks for one).
  const again = await (await request.post('/api/cadence/reconcile', { data: {} })).json();
  expect((again.minted || []).length).toBe(0);
});

// --- 3b. the shelf's FIFTH band renders the client cards -------------------
// The band is only real if it reaches the screen: `canvas.plan_candidates` tags
// the card `why='cliente'` and `today-planner.js` routes it into
// BANDS[3] — a band declared in BANDS but missing from the `buckets` literal
// would render EMPTY while its cards hid inside 'Active cycle' (green in the
// unit test that only reads BANDS, wrong on the page). This asserts the pixel.
test('the shelf renders a fifth Cliente / venta band with the minted cards', async ({ page, request }) => {
  const rec = await (await request.post('/api/cadence/reconcile', { data: {} })).json();
  expect(rec.status).toBe('ok');

  await openToday(page);
  await page.evaluate(() => toggleTodayShelf(true));
  await page.waitForTimeout(300);

  const header = page.locator('[data-band-header="cliente"]');
  await expect(header, 'the fifth band must render, not hide inside Active cycle').toBeVisible();
  await expect(header).toContainText('Cliente / venta');
  expect(await page.locator('#today-shelf-list [data-band="cliente"]').count())
    .toBeGreaterThan(0);
});

// --- 4. the epic picker is gone (RED-PROOF) ---------------------------------
// Epics were folded into projects; /api/epics and PATCH /api/tasks/{id}/epic
// answer 410 `epics_folded`. A menu item whose only possible outcome is an error
// toast is worse than no menu item. This test's selector MATCHES at HEAD~ — it
// was watched red against the pre-fix template before being kept.
test('the card menu no longer offers the epic picker', async ({ page }) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('my-tasks'));
  await expect.poll(() => page.evaluate(() =>
    (typeof loadedTasks !== 'undefined' && loadedTasks.some(t => t.project_id)) ? 1 : 0),
    { timeout: 15000 }).toBe(1);

  // Build the card menu for a task that HAS a project — the only condition under
  // which the retired item ever rendered.
  await page.evaluate(() => {
    const t = loadedTasks.find(x => x.project_id);
    openCardMenu(document.body, t.id, 'mine');
  });
  await expect(page.locator('[role="menu"]')).toBeVisible();
  expect(await page.locator('[role="menu"] button[onclick*="openEpicPicker"]').count(),
    'the retired epic picker must not be in the card menu').toBe(0);

  // …and the retired call sites are gone from the page, not merely unreachable.
  const gone = await page.evaluate(() => ({
    picker: typeof openEpicPicker,
    assign: typeof assignTaskEpic,
    edit: typeof openEditEpic,
    create: typeof createEpic,
  }));
  expect(gone).toEqual({ picker: 'undefined', assign: 'undefined', edit: 'undefined', create: 'undefined' });
});
