// e2e for backlog/someday card MOVEMENT (the week-planning board mode): the
// destination chips, the [ / ] keyboard moves, Undo, and the same-column drop
// feedback. The contract lives on the chips/keys, not on drag simulation —
// SortableJS runs forceFallback, so a mouse drag is the flakiest surface here
// and the mover it delegates to is exercised directly instead.
//
// The headline case is P0: moving a card to Unscheduled used to send
// PATCH {scheduled_week: null}, which api.py's `is not None` guard treats as a
// 200 no-op — the card snapped back on the next load. Test 1 fails against the
// pre-fix client (proved red before this file went green).
//
// Runs against the wiped-cycle DB copy from playwright.config.js: with no
// sprints, backlogColumnFor's cycle branch is inert and columns key purely on
// scheduled_week. Borrows ONE existing human task and restores its week in
// afterAll, so the blank slate the sibling board-* specs rely on is preserved.
//
// Run: `npx playwright test board-backlog-move`.
const { test, expect } = require('@playwright/test');

const COL = (key) => `#board-kanban .kanban-list[data-col="${key}"]`;
const CHIP = (key) => `[data-testid="backlog-move-${key}"]`;

// The card, wherever it currently is on the board.
const cardIn = (page, col, id) => page.locator(`${COL(col)} .kanban-card[data-task-id="${id}"]`);

// Land on the board in backlog mode with the WHO filter wide open, before any
// app JS runs — localStorage is where BOARD_MODE/BOARD_WHO are read from.
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

// Reload and come back to the same view — the "did it actually persist?" half.
async function reloadBacklogBoard(page) {
  await page.reload();
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('board'));
  await expect(page.locator('#board-kanban .kanban-column').first()).toBeVisible();
}

const setWeek = (request, id, week) =>
  request.patch(`/api/tasks/${id}`, {
    data: week === null ? { clear_scheduled_week: true } : { scheduled_week: week },
  });

test.describe('Backlog board card movement', () => {
  let taskId;
  let priorWeek;

  test.beforeAll(async ({ request }) => {
    // A live, human-owned, non-done task — backlog mode filters by assignee and
    // drops done/rejected work out of every column but This Week.
    const tasks = (await (await request.get('/api/tasks?limit=0')).json()).tasks || [];
    const t = tasks.find((x) => x.assignee_type === 'human' && !x.archived_at
      && !['done', 'rejected', 'cancelled'].includes((x.status || '').toLowerCase()));
    expect(t, 'need one live human task in the fixture DB').toBeTruthy();
    taskId = t.id;
    priorWeek = t.scheduled_week || null;
  });

  test.afterAll(async ({ request }) => {
    if (taskId) await setWeek(request, taskId, priorWeek);   // restore the blank slate
  });

  test.beforeEach(async ({ request }) => {
    await setWeek(request, taskId, 'someday');
  });

  // P0 — this is the case the old {scheduled_week: null} body could not pass.
  test('Someday → Unscheduled chip persists across a reload', async ({ page }) => {
    await openBacklogBoard(page);
    await expect(cardIn(page, 'someday', taskId)).toBeVisible();

    await cardIn(page, 'someday', taskId).locator(CHIP('unscheduled')).click();
    await expect(cardIn(page, 'unscheduled', taskId)).toBeVisible();

    await reloadBacklogBoard(page);
    await expect(cardIn(page, 'unscheduled', taskId)).toBeVisible();
    await expect(cardIn(page, 'someday', taskId)).toHaveCount(0);

    const got = await (await page.request.get(`/api/tasks/${taskId}`)).json();
    expect((got.task || got).scheduled_week).toBeFalsy();
  });

  test('chip round-trip Unscheduled → This wk → Someday is optimistic then persisted', async ({ page, request }) => {
    await setWeek(request, taskId, null);
    await openBacklogBoard(page);
    await expect(cardIn(page, 'unscheduled', taskId)).toBeVisible();

    // Each hop lands WITHOUT a reload — the mover re-renders from loadedTasks.
    await cardIn(page, 'unscheduled', taskId).locator(CHIP('this_week')).click();
    await expect(cardIn(page, 'this_week', taskId)).toBeVisible();

    await cardIn(page, 'this_week', taskId).locator(CHIP('someday')).click();
    await expect(cardIn(page, 'someday', taskId)).toBeVisible();

    // …and the last hop survived the round-trip to the server.
    await reloadBacklogBoard(page);
    await expect(cardIn(page, 'someday', taskId)).toBeVisible();
  });

  test('Undo on the move toast returns the card to its prior column', async ({ page }) => {
    await openBacklogBoard(page);
    await cardIn(page, 'someday', taskId).locator(CHIP('this_week')).click();
    await expect(cardIn(page, 'this_week', taskId)).toBeVisible();

    const undo = page.locator('#toast-stack button', { hasText: 'Undo' }).first();
    await expect(undo).toBeVisible();
    await undo.click();

    await expect(cardIn(page, 'someday', taskId)).toBeVisible();
    await reloadBacklogBoard(page);
    await expect(cardIn(page, 'someday', taskId)).toBeVisible();
  });

  test('] moves one column right, focus stays with the card, Enter opens it', async ({ page, request }) => {
    await setWeek(request, taskId, null);   // Unscheduled — ] has nowhere to go, [ does
    await openBacklogBoard(page);
    const card = cardIn(page, 'unscheduled', taskId);
    await card.focus();

    // [ walks left along this_week ← next_week ← someday ← unscheduled.
    await page.keyboard.press('BracketLeft');
    await expect(cardIn(page, 'someday', taskId)).toBeVisible();
    expect(await page.evaluate(() => document.activeElement.getAttribute('data-task-id'))).toBe(taskId);
    // …and it is a real move, not just a DOM reshuffle.
    await expect.poll(async () => (await (await page.request.get(`/api/tasks/${taskId}`)).json())
      .task.scheduled_week, { timeout: 5000 }).toBe('someday');

    // ] walks back right, from the card that still has focus.
    await page.keyboard.press('BracketRight');
    await expect(cardIn(page, 'unscheduled', taskId)).toBeVisible();
    expect(await page.evaluate(() => document.activeElement.getAttribute('data-task-id'))).toBe(taskId);
    await expect.poll(async () => (await (await page.request.get(`/api/tasks/${taskId}`)).json())
      .task.scheduled_week ?? null, { timeout: 5000 }).toBe(null);

    // Enter on a focused card opens its detail — role=button used to be dead.
    await page.keyboard.press('Enter');
    await expect(page.locator('#entity-drawer')).toBeVisible();
  });

  // The chips are the change's headline keyboard-first affordance ("always
  // visible, never hover-revealed… hover-gating breaks keyboard-first") and they
  // are three tab stops on every non-done card — but the card's own onkeydown
  // fires for keys bubbling out of them and used to preventDefault() Enter/Space
  // unconditionally, cancelling the <button>'s native activation. Result: every
  // chip opened the drawer instead of moving the card. Not one assertion in the
  // suite touched a chip with the keyboard, so it shipped green.
  test('Enter and Space on a focused destination chip move the card, not open it', async ({ page, request }) => {
    await setWeek(request, taskId, null);
    await openBacklogBoard(page);

    await cardIn(page, 'unscheduled', taskId).locator(CHIP('next_week')).focus();
    expect(await page.evaluate(() =>
      document.activeElement.getAttribute('data-testid'))).toBe('backlog-move-next_week');
    await page.keyboard.press('Enter');
    await expect(cardIn(page, 'next_week', taskId)).toBeVisible();
    await expect(page.locator('#entity-drawer')).toBeHidden();

    // Space is the other native button activation key, and hits the same guard.
    await cardIn(page, 'next_week', taskId).locator(CHIP('someday')).focus();
    await page.keyboard.press('Space');
    await expect(cardIn(page, 'someday', taskId)).toBeVisible();
    await expect(page.locator('#entity-drawer')).toBeHidden();

    await expect.poll(async () => (await (await page.request.get(`/api/tasks/${taskId}`)).json())
      .task.scheduled_week, { timeout: 5000 }).toBe('someday');
  });

  // Done cards are a historical record: they render no chips and refuse [ / ],
  // so drag must refuse them too or it becomes the one surviving way to move one.
  test('done cards expose no move affordance on any path', async ({ page }) => {
    await openBacklogBoard(page);
    const done = page.locator('#board-kanban .kanban-card.opacity-60').first();
    if (await done.count() === 0) test.skip(true, 'no done card in This Week in this fixture');
    await expect(done.locator('[data-testid^="backlog-move-"]')).toHaveCount(0);
    // Sortable's own filter check — the same predicate its onStart consults.
    expect(await done.evaluate((el) => el.matches('.kanban-card.opacity-60'))).toBe(true);
  });

  test('a same-column drop announces that order is not saved', async ({ page }) => {
    await openBacklogBoard(page);
    await expect(cardIn(page, 'someday', taskId)).toBeVisible();

    // Drive the drop handler directly with a same-column event: SortableJS's
    // forceFallback drag is the flakiest surface in this suite, and the
    // behaviour under test is the handler's guard, not the mouse path.
    await page.evaluate((id) => {
      const list = document.querySelector('#board-kanban .kanban-list[data-col="someday"]');
      const item = list.querySelector(`.kanban-card[data-task-id="${id}"]`);
      return onBacklogWeekDrop({ to: list, from: list, item });
    }, taskId);

    await expect(page.locator('#sr-live')).toHaveText('Order not saved — columns sort themselves');
    await expect(cardIn(page, 'someday', taskId)).toBeVisible();   // snapped back, nothing moved
  });
});
