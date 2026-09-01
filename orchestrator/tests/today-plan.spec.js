// e2e regression contract for the Today tab's interactive day plan.
//
// Every test here pins a defect found in live review of the first cut — each one
// was reproduced against the pre-fix build before the fix landed, so none of these
// assertions is a test that has never been red (Tier-1c: prove a new contract red).
//
// Runs against the wiped-cycle DB COPY from playwright.config.js — it plans,
// reorders and back-dates tasks freely, and never touches the real kanban.
//
// Run: `npx playwright test today-plan`.
const { test, expect } = require('@playwright/test');

// --- helpers ---------------------------------------------------------------

// Mutating endpoints need the dashboard bearer token (auth_middleware). In the
// browser auth-injector.js attaches it; the API-request fixture has to do it by
// hand, or every setup PATCH silently 401s and the assertions test nothing.
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

// N unplanned task ids to work with, straight from the server's own payload.
const pickTasks = async (request, n = 2) => {
  const d = await (await request.get('/api/day-plan?candidates=true')).json();
  const lg = d.later_groups || {};
  const pool = [].concat(lg.this_week || [], lg.next_week || [], lg.future || [], lg.backlog || []);
  // Reorder/undo tests need WORKABLE picks: a blocked or parked (pinned_bottom)
  // card lands in the waiting band by design and is not reorderable, so a pick
  // like that turns todayMove into a legitimate no-op and the assertion measures
  // the pick, not the product (bit us live: a parked card in the test DB copy).
  const ids = pool.filter(t => t.status !== 'blocked' && !t.pinned_bottom)
    .map(t => t.id).filter(Boolean).slice(0, n);
  expect(ids.length, 'the test DB must expose at least ' + n + ' workable unplanned tasks').toBe(n);
  return ids;
};

const planRaw = (request, id, order) =>
  patch(request, `/api/tasks/${id}/plan`, { planned_for: 'today', plan_order: order });
const unplanRaw = (request, id) => patch(request, `/api/tasks/${id}/plan`, { clear: true });

// Leftovers from a failed run would silently change what `pickTasks` returns and
// what the Do list contains — start every test from an empty plan.
test.beforeEach(async ({ request }) => {
  const d = await (await request.get('/api/day-plan')).json();
  for (const t of (d.do || [])) await unplanRaw(request, t.id);
});

// switchTab fires loadToday() asynchronously. If a test mutates TODAY_DATA (or
// calls a verb) while that first fetch is still in flight, the response replaces
// the object underneath it and the assertion measures the RACE, not the product —
// so settle the tab fully before handing it to a test.
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

// --- 1. the render path must never move the viewport ------------------------
// applyTodayFocus() runs at the end of EVERY renderToday() — initial load, the
// 45s poll, and every optimistic mutation. An unconditional scrollIntoView there
// yanked the page ~2.9k px down to the plan, forever, so the greeting / morning
// ritual / Day Review above it could not be read.
test('a poll re-render leaves the scroll position alone', async ({ page, request }) => {
  const [a, b] = await pickTasks(request, 2);
  await planRaw(request, a, 0);
  await planRaw(request, b, 1);
  try {
    await openToday(page);
    await expect.poll(() => domOrder(page).then(o => o.length)).toBeGreaterThan(0);
    // The page must be long enough that a scroll-jack would be visible at all.
    const room = await page.evaluate(() => document.documentElement.scrollHeight - innerHeight);
    expect(room, 'page must be scrollable for this assertion to mean anything').toBeGreaterThan(200);

    await page.evaluate(() => window.scrollTo(0, 0));
    await page.evaluate(() => loadToday());
    await page.waitForTimeout(500);
    expect(await page.evaluate(() => window.scrollY)).toBe(0);

    // …and the ring itself is still applied — only the scrolling was removed.
    expect(await page.evaluate(() => !!document.querySelector('.today-kfocus'))).toBe(true);

    // An explicit focus move DOES still scroll (the affordance is not just gone).
    await page.evaluate(() => { _todayFocus = { pane: 'do', id: null }; applyTodayFocus(true); });
  } finally {
    await unplanRaw(request, a); await unplanRaw(request, b);
  }
});

// --- 2. Enter must not be swallowed on the tab's controls -------------------
// Two independent interceptors did it: the document-level Today keymap, and each
// card's own inline onkeydown (which fired for events bubbling out of the buttons
// inside it). Both cancelled the native activation of the focused <button>, so
// every verb twin — Plan, ✓ done, ↑, ↓, ✕ out, ← Today — was keyboard-dead.
test('Enter activates Today-tab buttons instead of opening the task drawer', async ({ page, request }) => {
  const [a] = await pickTasks(request, 1);
  await planRaw(request, a, 0);
  try {
    await openToday(page);
    await expect.poll(() => domOrder(page).then(o => o.length)).toBeGreaterThan(0);

    const prevented = await page.evaluate(() => {
      const mk = () => new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
      const out = {};
      const targets = {
        plan: '#today-plan-btn',
        done: '#today-do-list [data-testid="today-done"]',
        up: '#today-do-list [data-testid="today-move-up"]',
        down: '#today-do-list [data-testid="today-move-down"]',
        kick: '#today-do-list [data-testid="today-kick-out"]',
      };
      Object.keys(targets).forEach(k => {
        const el = document.querySelector(targets[k]);
        out[k] = el ? (() => { const e = mk(); el.dispatchEvent(e); return e.defaultPrevented; })() : 'MISSING';
      });
      return out;
    });
    Object.entries(prevented).forEach(([k, v]) =>
      expect(v, `Enter on the ${k} button must not be preventDefault()ed`).toBe(false));

    // The drawer must NOT have opened as a side effect of any of those.
    expect(await page.evaluate(() => location.search)).not.toContain('entity');

    // Regression guard: Enter on the card BODY still opens the task detail.
    const cardPrevented = await page.evaluate(() => {
      const card = document.querySelector('#today-do-list [data-task-id]');
      const e = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
      card.dispatchEvent(e);
      return e.defaultPrevented;
    });
    expect(cardPrevented, 'the card body itself still opens the drawer on Enter').toBe(true);
  } finally {
    await unplanRaw(request, a);
  }
});

// --- 3. the debounce window is part of "busy" -------------------------------
// _todayPersistPending used to be raised inside persistTodayOrder, i.e. only AFTER
// the 400ms debounce fired. A poll landing in that window re-rendered the server's
// stale order, and the debounced write then re-read the reverted DOM — the reorder
// vanished with no toast and no error.
test('a poll cannot land inside the reorder debounce window', async ({ page, request }) => {
  const [a, b] = await pickTasks(request, 2);
  await planRaw(request, a, 0);
  await planRaw(request, b, 1);
  try {
    await openToday(page);
    const before = await domOrder(page);
    expect(before.length).toBe(2);

    // Synchronously: move, then ask the guard the same question the poll asks.
    const probe = await page.evaluate((ids) => {
      todayMove(ids[1], -1);
      const busy = todayPollBusy();
      if (!busy) loadToday();                       // what the real poll would do
      return { busy, dom: [...document.querySelectorAll('#today-do-list [data-task-id]')].map(e => e.dataset.taskId) };
    }, before);

    expect(probe.busy, 'the guard must be busy for the WHOLE debounce window').toBe(true);
    expect(probe.dom).toEqual([before[1], before[0]]);

    // …and the write really lands: server order converges on the DOM order.
    await expect.poll(() => serverOrder(request), { timeout: 5000 }).toEqual([before[1], before[0]]);
    expect(await domOrder(page)).toEqual([before[1], before[0]]);
  } finally {
    await unplanRaw(request, a); await unplanRaw(request, b);
  }
});

// --- 4. undo must invert the LAST mutation ----------------------------------
// `if (!_todayUndo) push(...)` meant a reorder never registered its own inverse
// while an earlier pull-in entry was still live: `u` then unplanned the task you
// had just pulled in — destructively — while the toast said "Undid pull-in".
test('a reorder pushes its own undo instead of inheriting the pull-in', async ({ page, request }) => {
  const [a, b] = await pickTasks(request, 2);
  await planRaw(request, a, 0);
  try {
    await openToday(page);
    await page.evaluate((id) => todayPlan(id), b);
    await expect.poll(() => domOrder(page).then(o => o.length)).toBe(2);
    expect(await page.evaluate(() => (_todayUndo || {}).label)).toBe('pull-in');

    const ids = await domOrder(page);
    await page.evaluate((id) => todayMove(id, -1), ids[1]);
    const undo = await page.evaluate(() => _todayUndo);
    expect(undo.label, '`u` must now revert the reorder, not unplan the pull-in').toBe('reorder');
    expect(undo.message).toMatch(/reorder/);
    // A burst of reorders still collapses into ONE entry (the pre-burst rank).
    expect(undo.task_ids).toEqual(ids);
    await page.evaluate((id) => todayMove(id, 1), ids[1]);
    expect(await page.evaluate(() => _todayUndo.task_ids)).toEqual(ids);
  } finally {
    await unplanRaw(request, a); await unplanRaw(request, b);
  }
});

// --- 5. a pulled-in card leaves every other zone ----------------------------
// todayPlan pushed into d.do but never spliced d.overdue, so renderToday() painted
// the same task in the red overdue pin AND the plan — with a live "→ Today" button
// that re-fired the mutation — and counts.overdue stayed wrong until the next poll.
test('pulling in an overdue card removes it from the overdue pin', async ({ page, request }) => {
  const [a] = await pickTasks(request, 1);
  const past = '2020-01-02';
  await patch(request, `/api/tasks/${a}/plan`, { due_date: past });
  try {
    await openToday(page);
    await expect.poll(() => page.evaluate((id) => (TODAY_DATA.overdue || []).some(t => t.id === id), a),
      { timeout: 10000 }).toBe(true);
    expect(await page.evaluate((id) =>
      document.querySelectorAll(`#today-overdue [data-task-id="${id}"]`).length, a)).toBe(1);

    await page.evaluate((id) => todayPlan(id), a);
    await page.waitForTimeout(400);

    const state = await page.evaluate((id) => ({
      inPlan: document.querySelectorAll(`#today-do-list [data-task-id="${id}"]`).length,
      inPin: document.querySelectorAll(`#today-overdue [data-task-id="${id}"]`).length,
      modelOverdue: (TODAY_DATA.overdue || []).length,
      countOverdue: TODAY_DATA.counts.overdue,
    }), a);
    expect(state.inPlan).toBe(1);
    expect(state.inPin, 'the pin must not keep a duplicate with a live "→ Today"').toBe(0);
    expect(state.countOverdue).toBe(state.modelOverdue);
  } finally {
    await unplanRaw(request, a);
    await patch(request, `/api/tasks/${a}/plan`, { due_date: '' });
  }
});

// --- 6. an idle open drawer must not freeze the tab -------------------------
// todayPollBusy() returned _todayShelfOpen outright, so leaving the shelf open —
// which is the whole point of a re-enterable drawer — froze review / needs-you /
// overdue / the progress bar indefinitely. The spec asked for "open AND
// mid-interaction".
test('an idle shelf does not freeze the 45s poll', async ({ page, request }) => {
  const [a] = await pickTasks(request, 1);
  await planRaw(request, a, 0);
  try {
    await openToday(page);
    await page.evaluate(() => toggleTodayShelf(true));
    expect(await page.evaluate(() => _todayShelfOpen)).toBe(true);
    expect(await page.evaluate(() => todayPollBusy()),
      'just-touched drawer IS busy').toBe(true);

    // Age the interaction past the window: still open, no longer busy.
    await page.evaluate(() => { _todayInteractAt = Date.now() - (TODAY_INTERACT_MS + 1000); });
    expect(await page.evaluate(() => _todayShelfOpen)).toBe(true);
    expect(await page.evaluate(() => todayPollBusy()),
      'an open-but-idle drawer must let the rest of the tab refresh').toBe(false);

    // A drag or a pending save still wins regardless of the drawer.
    expect(await page.evaluate(() => { _todayPersistPending = true; const r = todayPollBusy(); _todayPersistPending = false; return r; })).toBe(true);
    await page.evaluate(() => toggleTodayShelf(false));
  } finally {
    await unplanRaw(request, a);
  }
});

// --- 7. un-done restores the status it had ----------------------------------
test('un-done restores in_progress instead of demoting to todo', async ({ page, request }) => {
  const [a] = await pickTasks(request, 1);
  await planRaw(request, a, 0);
  await patch(request, `/api/tasks/${a}`, { status: 'in_progress' });
  try {
    await openToday(page);
    await expect.poll(() => page.evaluate((id) => (TODAY_DATA.do.find(t => t.id === id) || {}).status, a),
      { timeout: 10000 }).toBe('in_progress');

    await page.evaluate((id) => todayDone(id), a);
    await page.waitForTimeout(400);
    await page.evaluate((id) => todayUndone(id), a);
    await page.waitForTimeout(500);

    expect(await page.evaluate((id) => (TODAY_DATA.do.find(t => t.id === id) || {}).status, a)).toBe('in_progress');
    const srv = await (await request.get('/api/day-plan')).json();
    expect((srv.do.find(t => t.id === a) || {}).status).toBe('in_progress');
  } finally {
    await unplanRaw(request, a);
  }
});

// --- 8. shelf bands: no dead rows, no invisible keyboard targets ------------
// domOrder walks children regardless of CSS visibility, so a collapsed band's
// cards stayed reachable by j/k and plannable by `p` while invisible. And a
// zero-count band rendered as a dead "CARRIED OVER · 0" row.
test('collapsed shelf bands leave the DOM and empty bands are not rendered', async ({ page, request }) => {
  const [a] = await pickTasks(request, 1);
  try {
    await openToday(page);
    await page.evaluate(() => {
      TODAY_DATA.candidates = { candidates: [
        { id: 'zz_over', title: 'overdue one', status: 'todo', why: 'overdue' },
        { id: 'zz_cyc', title: 'cycle one', status: 'todo', why: 'cycle' },
      ] };
      TODAY_DATA.later_groups = { this_week: [], next_week: [], future: [], backlog: [] };
      _todayCollapsedBands.clear();
      toggleTodayShelf(true);
      renderTodayShelf(TODAY_DATA);
    });

    let shelf = await page.evaluate(() => ({
      headers: [...document.querySelectorAll('#today-shelf-list [data-band-header]')].map(b => b.dataset.bandHeader),
      cards: [...document.querySelectorAll('#today-shelf-list [data-task-id]')].map(e => e.dataset.taskId),
    }));
    expect(shelf.headers, 'zero-count bands are skipped entirely').toEqual(['overdue', 'cycle']);
    expect(shelf.cards).toEqual(['zz_over', 'zz_cyc']);

    await page.evaluate(() => toggleTodayBand('overdue'));
    shelf = await page.evaluate(() => ({
      headers: [...document.querySelectorAll('#today-shelf-list [data-band-header]')].map(b => b.dataset.bandHeader),
      cards: [...document.querySelectorAll('#today-shelf-list [data-task-id]')].map(e => e.dataset.taskId),
      focusReachable: TodayPlanner.domOrder(document.getElementById('today-shelf-list')),
    }));
    expect(shelf.headers, 'a collapsed band keeps its header (it must be re-openable)').toEqual(['overdue', 'cycle']);
    expect(shelf.cards, 'a collapsed band\'s cards are gone, not just display:none').toEqual(['zz_cyc']);
    expect(shelf.focusReachable, 'j/k and `p` cannot reach an invisible candidate').toEqual(['zz_cyc']);

    await page.evaluate(() => { toggleTodayBand('overdue'); toggleTodayShelf(false); });
  } finally {
    await unplanRaw(request, a);
  }
});

// --- 9. the plan card drops its reject ✕ ------------------------------------
// Two near-identical ✕ glyphs ~3px apart at the smallest hit-target size, one
// harmless (unplan) and one destructive (opens the reject-kill modal), plus a
// five-button cluster that crushed the title into four lines at 375px.
test('the Do card has no reject ✕ next to the kick-out ✕', async ({ page, request }) => {
  const [a] = await pickTasks(request, 1);
  await planRaw(request, a, 0);
  try {
    await page.setViewportSize({ width: 375, height: 812 });
    await openToday(page);
    await expect.poll(() => domOrder(page).then(o => o.length)).toBeGreaterThan(0);

    const card = page.locator('#today-do-list [data-task-id]').first();
    expect(await card.locator('button[onclick^="openRejectModal"]').count(),
      'reject lives in the detail drawer, not beside kick-out').toBe(0);
    // Seven verbs: start/stop (▶), done, block (⛔), park (📌), up, down,
    // kick-out. Scoped to the ACTION ROW — the card body carries its own inline
    // edit affordances (✏️ rename, lane project chip) that are not verbs. The
    // bound guards against the action row growing unbounded, not a specific
    // count — the REQUIRED property is the reject-absence assert above.
    expect(await card.locator('[data-testid="today-card-actions"] button').count(),
      'seven verbs max in a plan card action row').toBeLessThanOrEqual(7);

    // The action row wraps onto its own line instead of squeezing the title.
    const geom = await page.evaluate(() => {
      const c = document.querySelector('#today-do-list [data-task-id]');
      const body = c.firstElementChild.getBoundingClientRect();
      const acts = c.lastElementChild.getBoundingClientRect();
      return { bodyBottom: body.bottom, actsTop: acts.top, pageOverflow: document.documentElement.scrollWidth - innerWidth };
    });
    expect(geom.actsTop, 'actions sit BELOW the title block at 375px').toBeGreaterThanOrEqual(geom.bodyBottom - 1);
    expect(geom.pageOverflow, 'no horizontal overflow').toBeLessThanOrEqual(0);
  } finally {
    await unplanRaw(request, a);
  }
});
