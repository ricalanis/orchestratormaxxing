// e2e for cross-column drag on the Cycle board (Playwright): dragging a card from
// one status column to another changes its status to the target column, persists,
// and updates the count badges.
//
// Self-contained: creates its own cycle + seeds cards via the API in beforeAll,
// and DELETES it in afterAll so the blank-slate the board-empty-state spec relies
// on is restored (files run alphabetically; this one runs first). Run:
//   PW_BASE_URL=http://127.0.0.1:8931 npx playwright test   (against a running server)
//   or `npx playwright test` to let webServer start one.
const { test, expect } = require('@playwright/test');

const IN_PROGRESS = '#cycle-kanban .kanban-list[data-col="in_progress"]';
const DONE = '#cycle-kanban .kanban-list[data-col="done"]';

// SortableJS uses forceFallback (simulated drag), so native dragTo won't fire it —
// drive a real mouse: press on the card, nudge past the drag tolerance, move to the
// TOP of the target column (emptyInsertThreshold gives room), release.
async function dragCardTo(page, card, targetList) {
  await card.scrollIntoViewIfNeeded();
  const s = await card.boundingBox();
  const d = await targetList.boundingBox();
  const targetY = Math.max(d.y + 16, Math.min(d.y + d.height - 16, 860));
  await page.mouse.move(s.x + s.width / 2, s.y + s.height / 2);
  await page.mouse.down();
  await page.mouse.move(s.x + s.width / 2, s.y + s.height / 2 + 12, { steps: 6 });
  // Enter the measured lower body of the now-full-height list. The old helper
  // aimed 14px below the list's top, which is an ambient card whenever Done is
  // non-empty; Sortable then kept the dragged card in its source column.
  await page.mouse.move(s.x + s.width / 2, targetY, { steps: 12 });
  await page.mouse.move(d.x + d.width / 2, targetY, { steps: 20 });
  await page.mouse.move(d.x + d.width / 2, targetY + 2, { steps: 3 });
  await page.waitForTimeout(120);
  await page.mouse.up();
}

test.describe('Cycle board cross-column status drag', () => {
  let sprintId;
  let seeded = [];   // the ids THIS spec put in the cycle

  test.beforeAll(async ({ request }) => {
    const cyc = await (await request.post('/api/sprints', { data: {} })).json();
    sprintId = cyc.id;
    await request.post(`/api/sprints/${sprintId}/start`);
    const tasks = (await (await request.get('/api/tasks')).json()).tasks.slice(0, 3);
    for (const t of tasks) {
      await request.patch(`/api/tasks/${t.id}/sprint`, { data: { sprint_id: sprintId } });
      await request.patch(`/api/tasks/${t.id}`, { data: { status: 'in_progress' } });
      seeded.push(t.id);
    }
  });

  test.afterAll(async ({ request }) => {
    if (sprintId) await request.delete(`/api/sprints/${sprintId}`);   // restore blank slate
  });

  test('drag In Progress → Done changes status, persists, and updates counts', async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });   // 4 columns fully visible
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    await page.evaluate(() => switchTab('cycle'));
    await expect(page.locator('#content-cycle')).toBeVisible();

    const inProg = page.locator(IN_PROGRESS);
    const done = page.locator(DONE);

    // OWN-YOUR-STATE (lq-4c53d622). This used to assert absolute counts —
    // `in_progress === 3`, `done === 0` — which was only ever true on a pristine
    // DB. The fixture copies the operator's REAL kanban.db and wipes sprints only,
    // and a cycle board legitimately also shows UNCOMMITTED tasks whose
    // scheduled_week lands in that cycle's ISO week. So as soon as the operator had
    // work scheduled for the current week, this spec went red at its PRE-CONDITION
    // and never reached the drag it exists to test (observed: 6 cards, not 3).
    // Assert on the ids this spec seeded, and on count DELTAS, instead.
    for (const id of seeded) {
      await expect(inProg.locator(`.kanban-card[data-task-id="${id}"]`)).toBeVisible();
    }
    const inProgBefore = await inProg.locator('.kanban-card').count();
    const doneBefore = await done.locator('.kanban-card').count();

    const taskId = seeded[0];
    const card = inProg.locator(`.kanban-card[data-task-id="${taskId}"]`);
    await expect(card).toBeVisible();

    await dragCardTo(page, card, done);

    // The card is now in Done, and the columns rebalanced by exactly one.
    await expect(done.locator(`.kanban-card[data-task-id="${taskId}"]`)).toBeVisible();
    await expect(inProg.locator('.kanban-card')).toHaveCount(inProgBefore - 1);
    await expect(done.locator('.kanban-card')).toHaveCount(doneBefore + 1);

    // The Done column's count badge moved with it.
    const doneColumn = page.locator('#cycle-kanban .kanban-column')
      .filter({ has: page.locator('.kanban-list[data-col="done"]') });
    await expect(doneColumn.locator('[data-count]')).toHaveText(String(doneBefore + 1));

    // Status persisted server-side.
    const t = await (await page.request.get(`/api/tasks/${taskId}`)).json();
    expect((t.task || t).status).toBe('done');
  });
});
