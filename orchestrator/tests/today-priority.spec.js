// e2e contract for the PRIORITY CHIP — the click-editable 0/1/2/3 pill that now
// rides every task card, on the Today plan and on the unified Board.
//
// What is load-bearing here, and why each assertion exists:
//   · Normal is visually silent. A tag every card wears carries no signal, so
//     level 0 renders as a zero-layout-shift ghost that only appears on hover —
//     and on a READ-ONLY surface it is omitted outright rather than dangling an
//     affordance that does nothing.
//   · One PATCH per intent. The rejected alternative (a click-to-cycle icon)
//     would have cost three clicks and three audit rows to reach Low.
//   · Esc/blur must not write. An editor that commits on cancel is worse than no
//     editor: it makes the audit trail lie.
//   · A RAISE bumps the card up the Do list once, through the existing reorder
//     path (so `u` inverts it like any other reorder); a LOWER must never move a
//     deliberately-placed card. The asymmetry is deliberate, not an oversight —
//     it is the assertion most likely to be "cleaned up" by a later refactor.
//
// Runs against the wiped-cycle DB COPY from playwright.config.js — it plans,
// re-prioritises and unplans freely, and never touches the real kanban.
//
// Run: `npx playwright test today-priority`.
const { test, expect } = require('@playwright/test');

// --- helpers (same shape as today-plan.spec.js) ----------------------------

// Mutating endpoints need the dashboard bearer token (auth_middleware); in the
// browser auth-injector.js attaches it, the request fixture must do it by hand
// or every setup PATCH silently 401s and the assertions test nothing.
let _token = null;
const auth = async (request) => {
  if (_token === null) {
    const html = await (await request.get('/')).text();
    const m = html.match(/name="dashboard-token" content="([^"]*)"/);
    _token = (m && m[1]) || '';
  }
  return _token ? { Authorization: `Bearer ${_token}` } : {};
};
const patch = async (request, url, data) => {
  const r = await request.patch(url, { data, headers: await auth(request) });
  expect(r.status(), `PATCH ${url} must succeed for this test's setup to be real`).toBeLessThan(300);
  return r;
};

// Workable, unplanned picks only: a blocked/parked card lands in the waiting band
// by design and is not reorderable, so a pick like that would measure the pick
// instead of the product.
const pickTasks = async (request, n = 2) => {
  const d = await (await request.get('/api/day-plan?candidates=true')).json();
  const lg = d.later_groups || {};
  const pool = [].concat(lg.this_week || [], lg.next_week || [], lg.future || [], lg.backlog || []);
  const ids = pool.filter(t => t.status !== 'blocked' && !t.pinned_bottom)
    .map(t => t.id).filter(Boolean).slice(0, n);
  expect(ids.length, 'the test DB must expose at least ' + n + ' workable unplanned tasks').toBe(n);
  return ids;
};

const planRaw = (request, id, order) =>
  patch(request, `/api/tasks/${id}/plan`, { planned_for: 'today', plan_order: order });
const unplanRaw = (request, id) => patch(request, `/api/tasks/${id}/plan`, { clear: true });
const setPrio = (request, id, p) => patch(request, `/api/tasks/${id}`, { priority: p });

const openToday = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('today'));
  await expect.poll(() => page.evaluate(() => (typeof TODAY_DATA !== 'undefined' && TODAY_DATA) ? 1 : 0),
    { timeout: 15000 }).toBe(1);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(400);
};

const domOrder = (page) => page.evaluate(() =>
  [...document.querySelectorAll('#today-do-list [data-task-id]')].map(el => el.dataset.taskId));

const serverOrder = async (request) =>
  ((await (await request.get('/api/day-plan')).json()).do || []).map(t => t.id);

const serverPrio = async (request, id) =>
  ((await (await request.get(`/api/tasks/${id}`)).json()).task || {}).priority;

// Records every PATCH the PAGE fires at /api/tasks/<id> (the commit path), so a
// test can assert both "it wrote exactly this" and "it wrote nothing at all".
const recordTaskPatches = (page) => {
  const seen = [];
  page.on('request', (r) => {
    if (r.method() === 'PATCH' && /\/api\/tasks\/[^/]+$/.test(new URL(r.url()).pathname)) {
      let body = null;
      try { body = JSON.parse(r.postData() || 'null'); } catch (e) { /* not json */ }
      seen.push({ url: new URL(r.url()).pathname, body });
    }
  });
  return seen;
};

const chip = (page, id) => page.locator(`#today-do-list [data-task-id="${id}"] [data-testid="prio-chip"]`);
const select = (page, id) => page.locator(`#today-do-list [data-task-id="${id}"] [data-testid="prio-select"]`);

// Leftovers from a failed run would change what pickTasks returns and what the Do
// list holds — every test starts from an empty plan.
test.beforeEach(async ({ request }) => {
  const d = await (await request.get('/api/day-plan')).json();
  for (const t of (d.do || [])) await unplanRaw(request, t.id);
});

// …but "empty the plan" is destructive to a SHARED test server. The DB copy is
// made once per server process (tests/serve_test_dashboard.py) and webServer
// reuses a running one, so an emptied plan outlives this file: today-groups'
// j/k contract needs planned cards and went red — a false failure manufactured
// by another spec's cleanup, in a suite whose whole job is to tell real breakage
// from measurement artifacts. Snapshot the plan this file inherited and put it
// back. (A worker-scoped request context: the `request` fixture is test-scoped
// and is not available in beforeAll/afterAll.)
let _inheritedPlan = null;
const apiCtx = (playwright, baseURL) => playwright.request.newContext({ baseURL });
test.beforeAll(async ({ playwright, baseURL }) => {
  const ctx = await apiCtx(playwright, baseURL);
  _inheritedPlan = ((await (await ctx.get('/api/day-plan')).json()).do || []).map(t => t.id);
  await ctx.dispose();
});
test.afterAll(async ({ playwright, baseURL }) => {
  if (!_inheritedPlan || !_inheritedPlan.length) return;
  const ctx = await apiCtx(playwright, baseURL);
  for (let i = 0; i < _inheritedPlan.length; i++) {
    await ctx.patch(`/api/tasks/${_inheritedPlan[i]}/plan`,
      { data: { planned_for: 'today', plan_order: i }, headers: await auth(ctx) });
  }
  await ctx.dispose();
});

// --- 1. the four states, and Normal's silence -------------------------------
test('the chip renders one pill per level, and Normal is silent until hover', async ({ page, request }) => {
  const [a, b, c, d] = await pickTasks(request, 4);
  await planRaw(request, a, 0); await planRaw(request, b, 1);
  await planRaw(request, c, 2); await planRaw(request, d, 3);
  await setPrio(request, a, 3); await setPrio(request, b, 2);
  await setPrio(request, c, 1); await setPrio(request, d, 0);
  try {
    await openToday(page);
    await expect(chip(page, a)).toHaveText('▲ Urgent');
    await expect(chip(page, b)).toHaveText('▲ High');
    await expect(chip(page, c)).toHaveText('▽ Low');
    // Level 0: the ghost exists (so revealing it shifts NOTHING) but is invisible
    // at rest and opaque once the card is hovered — the ✏️-rename idiom.
    //
    // The reveal is measured on the BUTTON, which is what the browser hovers and
    // focuses. It used to live on the inner <span>, where `focus:` could never
    // match anything: the span is not focusable and there is no .group between
    // the two, so every level-0 ghost was a tab stop with zero visual feedback.
    await expect(chip(page, d)).toHaveText('▽');
    const opacity = (sel) => page.evaluate(([id, s]) => getComputedStyle(
      document.querySelector(`#today-do-list [data-task-id="${id}"] ${s}`)).opacity, [d, sel]);
    expect(Number(await opacity('[data-testid="prio-chip"]'))).toBe(0);
    await page.locator(`#today-do-list [data-task-id="${d}"]`).hover();
    await expect.poll(async () => Number(await opacity('[data-testid="prio-chip"]'))).toBe(1);

    // Keyboard focus alone must reveal it — the assertion that was previously
    // unfalsifiable. Move the mouse away first so hover is not what we measure.
    await page.mouse.move(0, 0);
    await expect.poll(async () => Number(await opacity('[data-testid="prio-chip"]'))).toBe(0);
    await chip(page, d).focus();
    expect(await page.evaluate((id) => document.activeElement ===
      document.querySelector(`#today-do-list [data-task-id="${id}"] [data-testid="prio-chip"]`), d)).toBe(true);
    // Polled, not read once: transition-opacity means a synchronous read lands
    // mid-transition and reports 0 for a chip that IS revealing. Reading it once
    // is how you manufacture a bug that isn't there.
    await expect.poll(async () => Number(await opacity('[data-testid="prio-chip"]')),
      { message: 'a focused ghost must be visible — an invisible tab stop is a keyboard trap' }).toBe(1);

    // The chip lives in the META row, never in the action row: that row's button
    // budget (≤7) is a shipped contract and priority is state, not a verb.
    expect(await page.evaluate((id) => document.querySelectorAll(
      `#today-do-list [data-task-id="${id}"] [data-testid="today-card-actions"] [data-testid="prio-chip"]`).length, a)).toBe(0);

    // A READ-ONLY card omits the level-0 chip entirely rather than showing a
    // hover affordance that cannot be clicked.
    const readOnly = await page.evaluate(() => ({
      normal: priorityChip({ priority: 0 }),
      high: priorityChip({ priority: 2 }),
    }));
    expect(readOnly.normal).toBe('');
    expect(readOnly.high).toContain('▲ High');
    expect(readOnly.high).not.toContain('prio-chip');
  } finally {
    for (const id of [a, b, c, d]) { await setPrio(request, id, 0); await unplanRaw(request, id); }
  }
});

// --- 2. one intent, one PATCH ----------------------------------------------
test('chip → select → commit writes exactly one PATCH {priority}', async ({ page, request }) => {
  const [a] = await pickTasks(request, 1);
  await planRaw(request, a, 0);
  try {
    await openToday(page);
    const patches = recordTaskPatches(page);
    await chip(page, a).click();
    await expect(select(page, a)).toBeVisible();
    // Severity order, not scale order — the list reads as "how loud is this".
    expect(await select(page, a).evaluate(el => [...el.options].map(o => o.textContent)))
      .toEqual(['Urgent', 'High', 'Normal', 'Low']);
    await select(page, a).selectOption('2');

    await expect.poll(() => patches.length, { timeout: 5000 }).toBe(1);
    expect(patches[0].url).toBe(`/api/tasks/${a}`);
    expect(patches[0].body).toEqual({ priority: 2 });
    await expect(chip(page, a)).toHaveText('▲ High');
    await expect.poll(() => serverPrio(request, a)).toBe(2);
  } finally {
    await setPrio(request, a, 0); await unplanRaw(request, a);
  }
});

// --- 3. cancel must not write ----------------------------------------------
// An editor that commits on Esc makes the audit trail lie about what the user
// decided. Esc reverts the picked value AND fires nothing.
test('Esc cancels the picker with no PATCH and restores the chip', async ({ page, request }) => {
  const [a, b] = await pickTasks(request, 2);
  await planRaw(request, a, 0); await planRaw(request, b, 1);
  await setPrio(request, a, 1);
  try {
    await openToday(page);
    const patches = recordTaskPatches(page);
    // A cancel changes nothing, so it must not rebuild the surface either. This
    // probe is on ANOTHER card: cancel used to call renderToday()/renderTasks(),
    // replacing every card's DOM — destroying transient state on cards the user
    // never touched (an open ⋯ menu, a mid-edit title) to undo a single click.
    await page.evaluate((id) => document.querySelector(
      `#today-do-list [data-task-id="${id}"]`).setAttribute('data-probe', '1'), b);

    await chip(page, a).click();
    await expect(select(page, a)).toBeVisible();
    // Pick a DIFFERENT value without firing `change` (a real user's keyboard
    // navigation inside an open native popup), then bail out.
    await select(page, a).evaluate((el) => { el.value = '3'; });
    await select(page, a).press('Escape');

    await expect(chip(page, a)).toHaveText('▽ Low');
    await expect(select(page, a)).toHaveCount(0);
    await page.waitForTimeout(500);
    expect(patches, 'Esc must not commit anything').toEqual([]);
    expect(await page.locator('#today-do-list [data-probe="1"]').count(),
      'cancelling one chip must not re-render the whole list').toBe(1);
    expect(await serverPrio(request, a)).toBe(1);
  } finally {
    for (const id of [a, b]) { await setPrio(request, id, 0); await unplanRaw(request, id); }
  }
});

// --- 4. the Do-list bump ----------------------------------------------------
// plan_order stays the only sort key, so a RAISE is honoured as a one-shot bump
// through the existing reorder path — which is why `u` inverts it like any other
// reorder instead of needing its own undo machinery.
test('raising a card bumps it above its lower-priority peers, and undo restores the rank', async ({ page, request }) => {
  const [a, b, c, d] = await pickTasks(request, 4);
  await planRaw(request, a, 0); await planRaw(request, b, 1);
  await planRaw(request, c, 2); await planRaw(request, d, 3);
  for (const id of [a, b, c, d]) await setPrio(request, id, 0);
  try {
    await openToday(page);
    expect(await domOrder(page)).toEqual([a, b, c, d]);

    await chip(page, d).click();
    await select(page, d).selectOption('3');

    // Urgent enters the Front Three: the anchors shift down one slot. That is the
    // one deliberate, user-initiated interaction with the anchor region.
    await expect.poll(() => domOrder(page)).toEqual([d, a, b, c]);
    expect(await page.evaluate(() => (_todayUndo || {}).label)).toBe('reorder');
    await expect.poll(() => serverOrder(request), { timeout: 5000 }).toEqual([d, a, b, c]);

    await page.evaluate(() => todayUndoLast());
    await expect.poll(() => serverOrder(request), { timeout: 5000 }).toEqual([a, b, c, d]);
    await expect.poll(() => domOrder(page)).toEqual([a, b, c, d]);
  } finally {
    for (const id of [a, b, c, d]) { await setPrio(request, id, 0); await unplanRaw(request, id); }
  }
});

// --- 4b. a FAILED PATCH must not leave the bump behind -----------------------
// The bump is a *server* write (POST /api/day-plan {replace:true}), so it may not
// ride ahead of the PATCH that justifies it. It used to: a 5xx reverted the chip,
// toasted "reverted", and left the card permanently at plan position 0 — with the
// server already told so, and `u` ("Undid reorder") the only way back from an
// action the user never took.
test('a failed priority PATCH reverts the chip AND never persists a bump', async ({ page, request }) => {
  const [a, b, c, d] = await pickTasks(request, 4);
  await planRaw(request, a, 0); await planRaw(request, b, 1);
  await planRaw(request, c, 2); await planRaw(request, d, 3);
  for (const id of [a, b, c, d]) await setPrio(request, id, 0);
  try {
    await openToday(page);
    expect(await domOrder(page)).toEqual([a, b, c, d]);

    // Every plan write the page attempts, whether or not it reaches the server.
    const planPosts = [];
    await page.route('**/api/day-plan', (route) => {
      if (route.request().method() === 'POST') planPosts.push(route.request().postData());
      return route.continue();
    });
    await page.route(`**/api/tasks/${d}`, (route) =>
      route.request().method() === 'PATCH'
        ? route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"nope"}' })
        : route.continue());

    await chip(page, d).click();
    await select(page, d).selectOption('3');

    await expect(chip(page, d)).toHaveText('▽');                       // chip reverted
    await page.waitForTimeout(1200);                                   // > the 400ms persist debounce
    expect(planPosts, 'a failed PATCH must not write a plan order').toEqual([]);
    expect(await domOrder(page), 'the card must not have moved').toEqual([a, b, c, d]);
    expect(await page.evaluate(() => _todayUndo),
      'no move → no undo entry the user has to discover to get their order back').toBe(null);
    expect(await serverOrder(request)).toEqual([a, b, c, d]);
    expect(await serverPrio(request, d)).toBe(0);
  } finally {
    await page.unrouteAll({ behavior: 'ignoreErrors' });
    for (const id of [a, b, c, d]) { await setPrio(request, id, 0); await unplanRaw(request, id); }
  }
});

// --- 5. a demotion must NOT yank the card -----------------------------------
// The symmetric "lower also moves it" version was rejected by product: a card you
// placed on purpose must not slide away because you relabelled it.
test('lowering a priority leaves the card exactly where it was', async ({ page, request }) => {
  const [a, b, c, d] = await pickTasks(request, 4);
  await planRaw(request, a, 0); await planRaw(request, b, 1);
  await planRaw(request, c, 2); await planRaw(request, d, 3);
  for (const id of [a, b, d]) await setPrio(request, id, 0);
  await setPrio(request, c, 3);          // an Urgent parked mid-list on purpose
  try {
    await openToday(page);
    expect(await domOrder(page)).toEqual([a, b, c, d]);

    await chip(page, c).click();
    await select(page, c).selectOption('0');

    await expect(chip(page, c)).toHaveText('▽');
    await expect.poll(() => serverPrio(request, c)).toBe(0);
    expect(await domOrder(page), 'a demotion must not move the card').toEqual([a, b, c, d]);
    expect(await page.evaluate(() => _todayUndo), 'no move → no reorder undo entry').toBe(null);
  } finally {
    for (const id of [a, b, c, d]) { await setPrio(request, id, 0); await unplanRaw(request, id); }
  }
});

// --- 5b. the shelf band sort is actually WIRED UP ---------------------------
// The unit tests pin TodayPlanner.sortByPriority; this pins that the renderer
// calls it. Both halves are needed: the live DB has every shelf card at priority
// 0, so the rule is unobservable there and a regression (dropping the call,
// re-inlining a comparator that mutates or destabilises) would ship silently.
// Synthetic candidates, injected the same way today-plan.spec.js does.
test('the shelf sorts by priority WITHIN a band, and the band grouping still wins', async ({ page, request }) => {
  const [a] = await pickTasks(request, 1);
  try {
    await openToday(page);
    const shelf = await page.evaluate(() => {
      const c = (id, why, priority) => ({ id, title: id, status: 'todo', why, priority });
      TODAY_DATA.candidates = { candidates: [
        // Arrival (age) order deliberately fights priority order inside 'cycle',
        // and a HIGHER-priority overdue card sits in an EARLIER band than an
        // Urgent cycle card — the band must still win.
        c('zz_over_lo', 'overdue', 0),
        c('zz_cyc_norm1', 'cycle', 0),
        c('zz_cyc_urgent', 'cycle', 3),
        c('zz_cyc_norm2', 'cycle', 0),
        c('zz_cyc_high', 'cycle', 2),
      ] };
      TODAY_DATA.later_groups = { this_week: [], next_week: [], future: [], backlog: [] };
      _todayCollapsedBands.clear();
      toggleTodayShelf(true);
      renderTodayShelf(TODAY_DATA);
      return [...document.querySelectorAll('#today-shelf-list [data-task-id]')].map(e => e.dataset.taskId);
    });
    expect(shelf).toEqual([
      'zz_over_lo',                                  // bands group first, always
      'zz_cyc_urgent', 'zz_cyc_high',                // then priority DESC…
      'zz_cyc_norm1', 'zz_cyc_norm2',                // …and equals keep age order
    ]);
    await page.evaluate(() => toggleTodayShelf(false));
  } finally {
    await unplanRaw(request, a);
  }
});

// --- 6. the keyboard twin ---------------------------------------------------
// `!` mirrors Quick-Add's `!p` grammar. It OPENS the picker with focus inside it
// (native type-ahead then commits) rather than cycling four states blind.
test('! opens the picker on the focused card with focus inside the select', async ({ page, request }) => {
  const [a, b] = await pickTasks(request, 2);
  await planRaw(request, a, 0); await planRaw(request, b, 1);
  try {
    await openToday(page);
    await page.evaluate((id) => { _todayFocus = { pane: 'do', id }; applyTodayFocus(true); }, b);
    await page.keyboard.press('Shift+Digit1');

    await expect(select(page, b)).toBeVisible();
    expect(await page.evaluate(() => document.activeElement.getAttribute('data-testid'))).toBe('prio-select');
    // …and only on the focused card.
    await expect(select(page, a)).toHaveCount(0);
  } finally {
    await unplanRaw(request, a); await unplanRaw(request, b);
  }
});

// A done card carries no chip by design, so `!` has nothing to open on it. The
// keymap card advertises the key unconditionally, so "nothing happens" reads as
// a broken key rather than as a rule — it has to say why.
test('! on a done card explains itself instead of silently doing nothing', async ({ page, request }) => {
  const [a] = await pickTasks(request, 1);
  await planRaw(request, a, 0);
  await patch(request, `/api/tasks/${a}`, { status: 'done' });
  try {
    await openToday(page);
    await expect(chip(page, a), 'a done card has no chip to open').toHaveCount(0);
    await page.evaluate((id) => { _todayFocus = { pane: 'do', id }; applyTodayFocus(true); }, a);
    await page.keyboard.press('Shift+Digit1');

    await expect(page.locator('#toast-stack .toast')).toContainText('Priority');
    await expect(page.locator('[data-testid="prio-select"]')).toHaveCount(0);
  } finally {
    await patch(request, `/api/tasks/${a}`, { status: 'ready' });
    await unplanRaw(request, a);
  }
});

// --- 7. the board column re-sorts under the edit -----------------------------
// The board's column sort already ran parked → priority DESC → age; the chip just
// makes it legible and editable. The movement after an edit IS the feedback.
const openBoard = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('board'));
  await expect(page.locator('#content-board')).toBeVisible();
  await expect
    .poll(() => page.evaluate(() => (typeof loadedTasks !== 'undefined' && loadedTasks.length) || 0), { timeout: 15000 })
    .toBeGreaterThan(0);
  // The DB copy has a WIPED cycle, so the default WHO=Mine × WHEN=Cycle view is
  // the cycle-empty state, not a kanban. Force the full firehose — the column
  // sort is what's under test, not the filter grid.
  await page.evaluate(() => { setBoardMode('kanban'); setBoardWho('all'); setBoardWhen('all'); });
  await expect(page.locator('.kanban-column[data-column="pool_inbox"]')).toBeVisible();
};

// The filter grid is sticky in localStorage — leaving it on "all" would silently
// change what every later board spec renders.
const restoreBoardPrefs = (page) => page.evaluate(() =>
  ['boardWho', 'boardWhen', 'boardMode'].forEach(k => localStorage.removeItem(k)));

// Ids of the WORK swimlane only: the personal lane is a separate run of cards
// below a divider, so mixing the two would compare positions across lanes.
const workLaneIds = (page) => page.evaluate(() => {
  const col = document.querySelector('.kanban-column[data-column="pool_inbox"] .kanban-list')
    || document.querySelector('.kanban-column[data-column="pool_inbox"]');
  const out = [];
  for (const el of (col ? col.children : [])) {
    if (el.matches && el.matches('[data-lane="personal"]')) break;
    if (el.dataset && el.dataset.taskId) out.push(el.dataset.taskId);
  }
  return out;
});

// --- 8. the ghost must not crowd the chip row off the card -------------------
// The Normal ghost is present on EVERY Normal card, so whatever it costs at rest
// it costs ~30 times per board. It used to be a full pill (28px) inside a row
// that neither wrapped nor scrolled under .kanban-card's overflow-hidden, so the
// last chip — the project — was sheared off the card edge on 18 of 46 cards at
// 1440px. Two independent guards, because either alone can regress silently.
test('the level-0 ghost stays cheap and no board chip row clips', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await openBoard(page);
  try {
    const m = await page.evaluate(() => {
      const rows = [...document.querySelectorAll('.kanban-card .card-chips')];
      const ghost = document.querySelector('.kanban-card [data-prio-chip][data-prio="0"]');
      return {
        rows: rows.length,
        ghosts: document.querySelectorAll('.kanban-card [data-prio-chip][data-prio="0"]').length,
        clipped: rows.filter(r => r.scrollWidth - r.clientWidth > 1)
          .map(r => r.closest('.kanban-card').dataset.taskId),
        ghostWidth: ghost ? Math.round(ghost.getBoundingClientRect().width) : null,
        wraps: rows.length ? getComputedStyle(rows[0]).flexWrap : null,
      };
    });
    expect(m.rows, 'the board must render cards for this to measure anything').toBeGreaterThan(3);
    // Guard 1 — the row wraps instead of shearing, at desktop width too.
    expect(m.wraps).toBe('wrap');
    expect(m.clipped, 'no chip row may overflow its card').toEqual([]);
    // Guard 2 — the always-present ghost carries no pill chrome (spec §2's
    // crowding fallback, applied to the level that actually caused crowding).
    if (m.ghosts) expect(m.ghostWidth, 'a chrome-less ghost, not a 28px pill').toBeLessThan(16);
  } finally {
    await restoreBoardPrefs(page);
  }
});

test('editing a board card re-sorts its column, and the card visibly moves', async ({ page, request }) => {
  await openBoard(page);
  let a = null, b = null;
  try {
    const ids = await workLaneIds(page);
    expect(ids.length, 'the test DB must render at least two work-lane Pool/Inbox cards').toBeGreaterThan(1);
    [a, b] = ids;
    await setPrio(request, a, 0); await setPrio(request, b, 0);
    await page.evaluate(() => loadBoard());
    await expect.poll(() => workLaneIds(page).then(o => o.indexOf(b))).toBe(1);

    const card = page.locator(`.kanban-column[data-column="pool_inbox"] [data-task-id="${b}"]`);
    await card.locator('[data-testid="prio-chip"]').click();
    await card.locator('[data-testid="prio-select"]').selectOption('3');

    const after = await workLaneIds(page);
    expect(after.indexOf(b), 'the raised card must now outrank its peer').toBeLessThan(after.indexOf(a));
    await expect(card.locator('[data-testid="prio-chip"]')).toHaveText('▲ Urgent');
    await expect.poll(() => serverPrio(request, b)).toBe(3);
  } finally {
    if (a) await setPrio(request, a, 0);
    if (b) await setPrio(request, b, 0);
    await restoreBoardPrefs(page);
  }
});
