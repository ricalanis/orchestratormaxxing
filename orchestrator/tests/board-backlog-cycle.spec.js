// e2e for the backlog mover's CYCLE half — the surface board-backlog-move.spec.js
// is structurally blind to.
//
// That sibling spec runs against the wiped-sprints DB precisely so
// `backlogColumnFor`'s cycle branch is inert. That makes its Undo test vacuous
// for the P2 contract: with no active cycle, `assign_active_cycle` is a server
// no-op, so a client that assigns a cycle on the way in and never un-commits it
// on the way out still passes. Two shipped blockers lived in exactly that gap:
//
//   1. Undo of a move INTO This Week left the task committed to the cycle. The
//      mover set `scheduled_week` locally but not `sprint_id` (the server set it),
//      so the reverse move read a stale local null, computed clearCycle=false and
//      never sent PATCH /sprint {sprint_id:null} — and undoWeekMove's own diff
//      check saw null === null and skipped the restore too. The card came back on
//      the next 45s poll, still leaking into cycle board / velocity / scorecard.
//   2. Same root cause with no Undo at all: This wk → Unsched in two ordinary
//      chip clicks re-pinned the card to This Week.
//
// So this file creates a REAL active cycle for the current ISO week, exercises
// the client sequence end to end, and asserts *persisted* `sprint_id` — not just
// DOM column membership. afterAll deletes the cycle, which returns every task it
// holds to the icebox (sprint_id NULL) and drops its ledger rows: byte-for-byte
// the blank slate the sibling board-* specs rely on.
//
// Run: `npx playwright test board-backlog-cycle`.
const { test, expect } = require('@playwright/test');

const COL = (key) => `#board-kanban .kanban-list[data-col="${key}"]`;
const CHIP = (key) => `[data-testid="backlog-move-${key}"]`;
const cardIn = (page, col, id) => page.locator(`${COL(col)} .kanban-card[data-task-id="${id}"]`);

async function openBacklogBoard(page) {
  await page.addInitScript(() => {
    localStorage.setItem('boardMode', 'backlog');
    localStorage.setItem('boardWho', 'all');
  });
  await page.setViewportSize({ width: 1400, height: 900 });
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('board'));
  await expect(page.locator('#board-kanban .kanban-column').first()).toBeVisible();
}

async function reloadBacklogBoard(page) {
  await page.reload();
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('board'));
  await expect(page.locator('#board-kanban .kanban-column').first()).toBeVisible();
}

const getTask = async (request, id) => {
  const body = await (await request.get(`/api/tasks/${id}`)).json();
  return body.task || body;
};

const setWeek = (request, id, week) =>
  request.patch(`/api/tasks/${id}`, {
    data: week === null ? { clear_scheduled_week: true } : { scheduled_week: week },
  });

// Server truth, polled — the mover PATCHes in the background, so a click
// returning is not the same as the write having landed.
const expectPersisted = (request, id, field, value) =>
  expect.poll(async () => (await getTask(request, id))[field] ?? null,
              { timeout: 5000 }).toBe(value);

test.describe('Backlog mover — active-cycle commitment', () => {
  let taskId;
  let priorWeek;
  let cycleId;

  test.beforeAll(async ({ request }) => {
    // A real ACTIVE cycle for the current ISO week: without one, every assertion
    // below is vacuous (assign_active_cycle no-ops server-side).
    const created = await (await request.post('/api/sprints', {
      data: { name: 'e2e backlog-move cycle', goal: 'backlog mover cycle contract' },
    })).json();
    cycleId = created.id;
    expect(cycleId, 'cycle create must return an id').toBeTruthy();
    await request.post(`/api/sprints/${cycleId}/start`);
    const sprints = await (await request.get('/api/sprints')).json();
    expect((sprints || []).find((s) => s.id === cycleId && s.status === 'active'),
      'the fixture cycle must be ACTIVE — the whole file is vacuous otherwise').toBeTruthy();

    const tasks = (await (await request.get('/api/tasks?limit=0')).json()).tasks || [];
    const t = tasks.find((x) => x.assignee_type === 'human' && !x.archived_at
      && !['done', 'rejected', 'cancelled'].includes((x.status || '').toLowerCase()));
    expect(t, 'need one live human task in the fixture DB').toBeTruthy();
    taskId = t.id;
    priorWeek = t.scheduled_week || null;
  });

  test.afterAll(async ({ request }) => {
    // Deleting the cycle returns every committed task to the icebox and drops the
    // ledger rows — the wiped-sprints slate the sibling specs assume.
    if (cycleId) await request.delete(`/api/sprints/${cycleId}`);
    if (taskId) {
      await request.patch(`/api/tasks/${taskId}/sprint`, { data: { sprint_id: null } });
      await setWeek(request, taskId, priorWeek);
    }
  });

  test.beforeEach(async ({ request }) => {
    await request.patch(`/api/tasks/${taskId}/sprint`, { data: { sprint_id: null } });
    await setWeek(request, taskId, null);
  });

  // BLOCKER 1 — red against the pre-fix client: server kept sprint_id = cycleId.
  test('Undo of a move into This Week un-commits the cycle, not just the week', async ({ page, request }) => {
    await openBacklogBoard(page);
    await expect(cardIn(page, 'unscheduled', taskId)).toBeVisible();

    await cardIn(page, 'unscheduled', taskId).locator(CHIP('this_week')).click();
    await expect(cardIn(page, 'this_week', taskId)).toBeVisible();
    await expectPersisted(request, taskId, 'sprint_id', cycleId);   // forward move really committed

    const undo = page.locator('#toast-stack button', { hasText: 'Undo' }).first();
    await expect(undo).toBeVisible();
    await undo.click();

    await expect(cardIn(page, 'unscheduled', taskId)).toBeVisible();
    await expectPersisted(request, taskId, 'scheduled_week', null);
    await expectPersisted(request, taskId, 'sprint_id', null);      // ← the leak

    // …and it stays put: a cycle-pinned card would be back in This Week here,
    // because backlogColumnFor pins sprint_id === activeCycle when week is empty.
    await reloadBacklogBoard(page);
    await expect(cardIn(page, 'unscheduled', taskId)).toBeVisible();
    await expect(cardIn(page, 'this_week', taskId)).toHaveCount(0);
  });

  // BLOCKER 2 — the same leak with no Undo involved: two ordinary chip clicks
  // inside one poll window, nothing refreshing sprint_id between them.
  test('This wk → Unsched in two chip clicks does not re-pin the card to the cycle', async ({ page, request }) => {
    await setWeek(request, taskId, 'someday');
    await openBacklogBoard(page);
    await expect(cardIn(page, 'someday', taskId)).toBeVisible();

    await cardIn(page, 'someday', taskId).locator(CHIP('this_week')).click();
    await expect(cardIn(page, 'this_week', taskId)).toBeVisible();
    // Wait for the first move's toast, so the second click is ordered after the
    // first PATCH — the bug under test is the missing local refresh, not a race.
    await expect(page.locator('#toast-stack')).toContainText('Moved to This Week');
    await expectPersisted(request, taskId, 'sprint_id', cycleId);

    await cardIn(page, 'this_week', taskId).locator(CHIP('unscheduled')).click();
    await expect(cardIn(page, 'unscheduled', taskId)).toBeVisible();

    await expectPersisted(request, taskId, 'scheduled_week', null);
    await expectPersisted(request, taskId, 'sprint_id', null);

    await reloadBacklogBoard(page);
    await expect(cardIn(page, 'unscheduled', taskId)).toHaveCount(1);
    await expect(cardIn(page, 'this_week', taskId)).toHaveCount(0);
  });

  // The client's explicit un-commit is load-bearing for exactly ONE destination.
  // set_scheduled_week sprint-syncs on its own when the new week differs from the
  // active cycle's (Next wk / Someday), but that branch requires a truthy week —
  // the Unscheduled path sends clear_scheduled_week, so the server leaves the
  // commitment standing and only the client's PATCH /sprint {sprint_id:null}
  // breaks the pin. Asserted through the UI so a refactor that "simplifies away"
  // the second request is caught here, not by a bouncing card in production.
  test('This Week → Someday is un-committed by the server; → Unscheduled only by the client', async ({ page, request }) => {
    await setWeek(request, taskId, 'someday');
    await openBacklogBoard(page);

    // Someday: the server's own sprint-sync would cover this one.
    await cardIn(page, 'someday', taskId).locator(CHIP('this_week')).click();
    await expectPersisted(request, taskId, 'sprint_id', cycleId);
    await expect(page.locator('#toast-stack')).toContainText('Moved to This Week');
    await cardIn(page, 'this_week', taskId).locator(CHIP('someday')).click();
    await expect(cardIn(page, 'someday', taskId)).toBeVisible();
    await expectPersisted(request, taskId, 'sprint_id', null);

    // Unscheduled: clear_scheduled_week skips the server branch entirely.
    await cardIn(page, 'someday', taskId).locator(CHIP('this_week')).click();
    await expectPersisted(request, taskId, 'sprint_id', cycleId);
    await cardIn(page, 'this_week', taskId).locator(CHIP('unscheduled')).click();
    await expect(cardIn(page, 'unscheduled', taskId)).toBeVisible();
    await expectPersisted(request, taskId, 'scheduled_week', null);
    await expectPersisted(request, taskId, 'sprint_id', null);
  });
});
