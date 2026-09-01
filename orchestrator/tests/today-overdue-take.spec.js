// e2e contract: TAKING AN OVERDUE TASK MOVES EVERY TODAY PROJECTION.
//
// filosofía integral, principio 3 — las proyecciones no pueden divergir: una
// acción, todas las proyecciones se mueven. On Hoy the SAME task is projected in
// four places at once:
//   · the red ⏰ Overdue pin        (#today-overdue        ← TODAY_DATA.overdue)
//   · the ✅ Hoy list               (#today-do-list        ← TODAY_DATA.do)
//   · the 🛒 Shelf's "Overdue" band (#today-shelf-list     ← candidates[why=overdue])
//   · the horizonte's `hoy` number  (#today-horizon        ← TODAY_HORIZON)
// so "→ Today" on an overdue card has to move all four, and they have to STAY
// moved once the server re-composes.
//
// RED-PROOF (run against HEAD before the fix, all three watched red):
//   1. the horizonte's `hoy` count never moved on a take/kick-out —
//      `loadTodayHorizon()` was only ever called from `loadToday()`, so no plan
//      verb refreshed it (stale until the 45s poll);
//   2. `GET /api/day-plan` kept listing the taken task under `overdue` —
//      `canvas.get_day_plan`'s overdue projection had no `planned_for` exclusion
//      (its sibling `later` projection has one), so the row was in `do` AND in
//      `overdue` simultaneously;
//   3. consequently the red pin RESURRECTED the card on the next server read,
//      with a live "→ Today" button, while the same card sat in ✅ Hoy.
//
// FIXTURE OWNERSHIP (lq-4c53d622): this spec never depends on which live rows
// happen to be overdue today. It empties the plan, picks one unplanned workable
// task with NO due date, and makes it overdue ITSELF — then restores it. The
// only thing it asks of the DB copy is that a workable unplanned task exists.
// (It cannot POST /api/tasks: that endpoint shells out to the `hermes` CLI,
// which writes the operator's REAL kanban.db, not the test copy.)
//
// Run: `npx playwright test today-overdue-take`.
const { test, expect } = require('@playwright/test');

// --- auth (same helper as today-plan.spec.js) -------------------------------
// Mutating endpoints need the dashboard bearer token; the API-request fixture
// has to attach it by hand or every setup PATCH silently 401s and the
// assertions test nothing.
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
const dayPlan = async (request) => (await request.get('/api/day-plan?candidates=true')).json();
const commitPlan = async (request, task_ids) => {
  const r = await request.post('/api/day-plan', {
    data: { task_ids, replace: true }, headers: await auth(request) });
  expect(r.status(), 'POST /api/day-plan must succeed').toBeLessThan(300);
};

const dayBefore = (iso) => {
  const [y, m, d] = iso.split('-').map(Number);
  const dt = new Date(y, m - 1, d - 1);
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`;
};

// --- the fixture this spec OWNS ---------------------------------------------
let SEED = null;       // { id, title }
let PRIOR_PLAN = null; // the day plan as we found it — put back in afterEach

test.beforeEach(async ({ request }) => {
  // Leftovers from a failed run change both what `do` contains and what the
  // horizonte counts — start from an empty plan. It is RESTORED afterwards: the
  // whole `today-` family shares one long-lived DB copy, and a spec that leaves
  // the plan empty silently breaks a sibling whose assertions need planned cards
  // (today-groups' j/k grammar). Leave the shared state as you found it.
  const d = await dayPlan(request);
  PRIOR_PLAN = (d.do || []).map(t => t.id);
  for (const t of (d.do || [])) await patch(request, `/api/tasks/${t.id}/plan`, { clear: true });

  const lg = d.later_groups || {};
  const pool = [].concat(lg.this_week || [], lg.next_week || [], lg.future || [], lg.backlog || []);
  // Workable (a blocked/parked card lands in the waiting band by design) and
  // WITHOUT a due date, so restoring it in afterEach is exact and the seeded
  // overdue-ness is unambiguously ours.
  const pick = pool.find(t => t && t.id && t.status !== 'blocked' && !t.pinned_bottom && !t.due_date);
  expect(pick, 'the test DB copy must expose one workable, unplanned, un-dated task to seed').toBeTruthy();
  SEED = { id: pick.id, title: pick.title };
  await patch(request, `/api/tasks/${SEED.id}/plan`, { due_date: dayBefore(d.date) });
});

test.afterEach(async ({ request }) => {
  if (SEED) {
    await patch(request, `/api/tasks/${SEED.id}/plan`, { clear: true, due_date: '' });
    SEED = null;
  }
  if (PRIOR_PLAN) {
    if (PRIOR_PLAN.length) await commitPlan(request, PRIOR_PLAN);
    PRIOR_PLAN = null;
  }
});

// --- page helpers -----------------------------------------------------------
// switchTab fires loadToday() asynchronously and the horizonte lands on a
// second, independent fetch — settle both or the test measures the race.
const openToday = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('today'));
  await expect.poll(() => page.evaluate(() => (typeof TODAY_DATA !== 'undefined' && TODAY_DATA) ? 1 : 0),
    { timeout: 15000 }).toBe(1);
  await expect.poll(() => page.evaluate(() => (typeof TODAY_HORIZON !== 'undefined' && TODAY_HORIZON) ? 1 : 0),
    { timeout: 15000 }).toBe(1);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(300);
};

// The rendered `hoy` number, read off the segment the operator actually sees.
const horizonHoy = (page) => page.evaluate(() => {
  const el = document.querySelector('[data-testid="today-horizon-seg"][data-key="hoy_pendientes"]');
  const raw = el && el.getAttribute('data-count');
  return raw === null || raw === '' ? null : Number(raw);
});

const inZone = (page, zone, id) => page.locator(`${zone} [data-task-id="${id}"]`);

// The refresh budget for a projection that must move on the ACTION. 6s is the
// point: the 45s poll cannot have fired, so a green here means the verb itself
// refreshed the number — which is exactly what was broken.
const NO_POLL_MS = 6000;

// --- 1. take: all four projections move, and they STAY moved ----------------
test('taking an overdue task moves the plan, the red pin, the shelf and the horizonte', async ({ page, request }) => {
  await openToday(page);
  const id = SEED.id;

  const hoy0 = await horizonHoy(page);
  expect(hoy0, 'the horizonte must be readable before the take (— means a failed read, not 0)').not.toBeNull();
  await expect(inZone(page, '#today-overdue', id), 'the seeded task is pinned as overdue').toHaveCount(1);
  await expect(inZone(page, '#today-do-list', id)).toHaveCount(0);

  await page.locator(`#today-overdue [data-task-id="${id}"] button`, { hasText: '→ Today' }).click();

  // --- WITHOUT waiting for any poll -----------------------------------------
  await expect(inZone(page, '#today-do-list', id), 'it is in ✅ Hoy now').toHaveCount(1);
  await expect(inZone(page, '#today-overdue', id), 'and gone from the red pin').toHaveCount(0);
  await expect(inZone(page, '#today-shelf-list', id), 'and gone from the shelf band').toHaveCount(0);
  // Moved, not lost: the red pin is the only zone that carried the urgency, so the
  // plan card has to carry it now — otherwise "aligning" the projections would
  // silently DELETE the signal instead of relocating it.
  await expect(inZone(page, '#today-do-list', id),
    'the plan card keeps the overdue badge the red pin used to carry').toContainText('due ');
  // RED at HEAD: no plan verb refreshed the horizonte.
  await expect.poll(() => horizonHoy(page), { timeout: NO_POLL_MS })
    .toBe(hoy0 + 1);

  // --- and the SERVER's own projections agree -------------------------------
  // RED at HEAD: get_day_plan listed the row in `do` AND in `overdue`.
  const plan = await dayPlan(request);
  expect(plan.do.map(t => t.id), 'the server planned it').toContain(id);
  expect(plan.overdue.map(t => t.id),
    'a task already in today\'s plan is not an un-triaged overdue task').not.toContain(id);
  expect((plan.candidates.candidates || []).map(t => t.id),
    'nor is it still a candidate to pull in').not.toContain(id);

  // --- so a server re-read cannot resurrect the red pin ---------------------
  // RED at HEAD: the very next /api/day-plan painted the card twice — once in
  // the pin (with a live "→ Today") and once in the plan.
  await page.evaluate(() => loadToday());
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(300);
  await expect(inZone(page, '#today-overdue', id), 'the pin stays clear after the server re-composes').toHaveCount(0);
  await expect(inZone(page, '#today-do-list', id)).toHaveCount(1);
  expect(await horizonHoy(page)).toBe(hoy0 + 1);
});

// --- 2. the reverse direction: kicking it out moves them all back -----------
test('kicking the taken task out returns it to overdue and moves the horizonte back', async ({ page, request }) => {
  await openToday(page);
  const id = SEED.id;
  const hoy0 = await horizonHoy(page);

  await page.locator(`#today-overdue [data-task-id="${id}"] button`, { hasText: '→ Today' }).click();
  await expect(inZone(page, '#today-do-list', id)).toHaveCount(1);
  await expect.poll(() => horizonHoy(page), { timeout: NO_POLL_MS }).toBe(hoy0 + 1);

  await page.locator(`#today-do-list [data-task-id="${id}"] [data-testid="today-kick-out"]`).click();

  // WITHOUT waiting for any poll: out of the plan, back on the red pin…
  await expect(inZone(page, '#today-do-list', id)).toHaveCount(0);
  await expect(inZone(page, '#today-overdue', id), 'a kicked-out overdue card goes back to the pin').toHaveCount(1);
  // …and the horizonte follows it back (RED at HEAD).
  await expect.poll(() => horizonHoy(page), { timeout: NO_POLL_MS }).toBe(hoy0);

  // The server agrees in both directions.
  const plan = await dayPlan(request);
  expect(plan.do.map(t => t.id)).not.toContain(id);
  expect(plan.overdue.map(t => t.id), 'unplanned and still past due → overdue again').toContain(id);
});
