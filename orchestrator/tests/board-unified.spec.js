// e2e for the unified Board (P0-5 lenses/columns, P0-6 contextual card actions).
// The pre-existing board-*.spec.js files test the CYCLE tab; this covers the new
// Board tab. Runs against the wiped-cycle DB copy from playwright.config.js.
//
// Self-contained: the tests that need a claimable card FORCE a specific task into
// the required status via PATCH (the single real 'ready' task can be consumed by
// an earlier serial spec — never depend on ambient board state).
//
// Run: `npx playwright test board-unified`.
const { test, expect } = require('@playwright/test');

const openBoard = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');   // let window.onload → switchTab('today') settle
  await page.evaluate(() => switchTab('board'));
  await expect(page.locator('#content-board')).toBeVisible();
  // loadBoard is async; wait until it has populated loadedTasks, then force the
  // All lens so column contents are deterministic (full firehose).
  await expect
    .poll(() => page.evaluate(() => (typeof loadedTasks !== 'undefined' && loadedTasks.length) || 0), { timeout: 15000 })
    .toBeGreaterThan(0);
  // The single `setBoardLens('all')` call this helper used to make was removed when
  // the lens model was refactored into WHO × WHEN × MODE (boardTaskMatch replaced
  // boardLensMatch). The spec kept calling it, so every test in this file died in
  // the helper with `ReferenceError: setBoardLens is not defined` — a spec/API
  // drift, NOT a product defect. Same firehose, current API.
  await page.evaluate(() => { setBoardMode('kanban'); setBoardWho('all'); setBoardWhen('all'); });
  await expect(page.locator('.kanban-column[data-column="pool_inbox"]')).toBeVisible();
};

const firstTaskIds = (page, n) =>
  page.evaluate((n) => fetch('/api/tasks?limit=0').then((r) => r.json()).then((d) => d.tasks.slice(0, n).map((t) => t.id)), n);

const setStatus = (page, id, status) =>
  page.evaluate(
    ({ id, status }) =>
      fetch(`/api/tasks/${id}`, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status }) }),
    { id, status },
  );

test.describe('Unified Board (P0-5 / P0-6)', () => {
  // RE-PINNED (was: "…+ five lenses", asserting 5 buttons inside #board-lenses).
  // That element no longer exists: the five-lens strip (Mine / This Cycle / The
  // Fleet / All / Backlog) was deliberately replaced by two ORTHOGONAL tablists —
  // #board-who × #board-when — plus Backlog as a MODE toggle. The replacement is
  // documented in the template itself ("Replaces the old per-lens boardLensMatch —
  // Mine/Fleet/All/Cycle are now just WHO×WHEN combinations", and renderBoardControls'
  // "replaces the stale 'four lenses' copy"). So the old assertion pinned a RETIRED
  // design, not a requirement. What survives the refactor — four columns, and a
  // selectable filter cluster — is re-pinned here against the current controls.
  test('renders the four Pool/Inbox → In Progress → Review → Done columns + the WHO×WHEN filter cluster', async ({ page }) => {
    await openBoard(page);
    await expect(page.locator('#board-kanban .kanban-column')).toHaveCount(4);
    for (const label of ['Pool / Inbox', 'In Progress', 'Review', 'Done']) {
      await expect(page.locator('#board-kanban')).toContainText(label);
    }
    // WHO is a fixed 3-pill tablist; WHEN is dynamic (live sprint short-names), so
    // it is asserted as non-empty rather than pinned to a count that moves weekly.
    await expect(page.locator('#board-who button')).toHaveCount(3);
    await expect(page.locator('#board-who button[aria-selected="true"]')).toHaveCount(1);
    expect(await page.locator('#board-when button').count()).toBeGreaterThan(0);
    // Backlog is a MODE now, not a fifth lens.
    await expect(page.locator('#board-backlog-toggle')).toBeVisible();
  });

  // RE-PINNED for the same reason. The behaviour worth keeping is "picking a filter
  // marks it selected and clears the previous one" — now expressed on #board-who.
  test('WHO filter switching updates the active pill', async ({ page }) => {
    await openBoard(page);
    const mine = page.locator('#board-who button', { hasText: 'Mine' });
    const agents = page.locator('#board-who button', { hasText: 'Agents' });
    await mine.click();
    await expect(mine).toHaveAttribute('aria-selected', 'true');
    await agents.click();
    await expect(agents).toHaveAttribute('aria-selected', 'true');
    await expect(mine).toHaveAttribute('aria-selected', 'false');
  });

  test('contextual card actions per column: Claim on Pool/Inbox, → Done on In Progress (§5.3)', async ({ page }) => {
    await openBoard(page);
    const [a, b] = await firstTaskIds(page, 2);
    await setStatus(page, a, 'ready');         // → Pool/Inbox
    await setStatus(page, b, 'in_progress');   // → In Progress
    await page.evaluate(() => loadBoard());

    await expect(page.locator(`.kanban-column[data-column="pool_inbox"] [data-task-id="${a}"] button`, { hasText: /Claim/ })).toBeVisible();
    await expect(page.locator(`.kanban-column[data-column="in_progress"] [data-task-id="${b}"] button`, { hasText: /Done/ })).toBeVisible();
  });

  test('Claim moves a Pool/Inbox task into In Progress and assigns it to you (P0-6)', async ({ page }) => {
    await openBoard(page);
    const [tid] = await firstTaskIds(page, 1);
    await setStatus(page, tid, 'ready');
    await page.evaluate(() => loadBoard());

    const card = page.locator(`.kanban-column[data-column="pool_inbox"] [data-task-id="${tid}"]`);
    await expect(card).toBeVisible();
    await card.locator('button', { hasText: /Claim/ }).click();

    await expect(page.locator(`.kanban-column[data-column="in_progress"] [data-task-id="${tid}"]`)).toBeVisible();
    const state = await page.evaluate((id) => {
      const t = loadedTasks.find((x) => x.id === id);
      return { status: t.status, assignee: t.assignee };
    }, tid);
    expect(state.status).toBe('in_progress');
    expect(state.assignee).toBe('ricardo');
  });

  test('a board card opens the shared entity drawer', async ({ page }) => {
    await openBoard(page);
    // Click a non-title region of the card — clicking the TITLE now enters inline
    // edit (Phase 1 follow-up); the rest of the card still opens the drawer.
    await page.locator('#board-kanban .kanban-card').first().locator('.card-grip').click();
    await expect(page.locator('#entity-drawer')).toBeVisible();
    await expect(page.locator('#ed-kind')).toHaveText('task');
  });

  test('clicking a card title inline-edits it (no drawer) and saves via PATCH', async ({ page }) => {
    await openBoard(page);
    const title = page.locator('#board-kanban .kanban-card .card-title').first();
    const taskId = await title.evaluate((el) => el.closest('[data-task-id]').dataset.taskId);
    await title.click();
    // Drawer must NOT open; an input takes over the title.
    await expect(page.locator('#entity-drawer')).toBeHidden();
    const input = title.locator('input.card-title-input');
    await expect(input).toBeVisible();
    const newTitle = 'Edited title ' + Date.now();
    await input.fill(newTitle);
    await input.press('Enter');
    // Persisted: the server now returns the new title.
    await expect
      .poll(() => page.evaluate((id) => fetch(`/api/tasks/${id}`).then((r) => r.json()).then((d) => d.task.title), taskId))
      .toBe(newTitle);
  });

  test('the card ⋯ menu speaks the unified board vocabulary (P1-4)', async ({ page }) => {
    await openBoard(page);
    const [tid] = await firstTaskIds(page, 1);
    await setStatus(page, tid, 'ready');            // Pool/Inbox → menu offers the other columns
    await page.evaluate(() => loadBoard());

    // Scoped to #board-kanban like every other selector in this file. Unscoped,
    // `[data-task-id=…].first()` matches whichever surface renders that id FIRST in
    // document order — the Today/planner panes keep their DOM after switchTab, and
    // their rows carry data-task-id but no .card-menu-btn, so the click waited
    // forever on a hidden element. Ambient-DOM leakage, not a product defect.
    await page.locator(`#board-kanban .kanban-card[data-task-id="${tid}"]`).first()
      .locator('.card-menu-btn').click();
    const menu = page.locator('[role="menu"]');
    await expect(menu).toBeVisible();
    const text = await menu.innerText();
    for (const v of ['In Progress', 'Review', 'Done', 'Blocked']) expect(text).toContain(v);  // unified vocab
    for (const old of ['Doing', 'Working']) expect(text).not.toContain(old);                  // old per-board vocab gone
  });
});
