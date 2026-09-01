// e2e for P0-11: the Sessions tab renders each session's linked tasks (session →
// tasks), completing the Sessions↔Tasks bidirectional link. A task chip on a
// session card opens that task in the shared entity drawer (P0-4). The task→
// session direction already exists (task cards/drawer point at a session).
//
// The link is exercised through the existing endpoints only: PATCH
// /api/tasks/{id}/session sets tasks.session_id; the card matches a task on the
// session's id / display name / tmux name. Runs against the wiped-cycle DB copy
// from playwright.config.js (tasks/sessions preserved).
//
// Run: `npx playwright test drawer-sessions`.
const { test, expect } = require('@playwright/test');

const openSessions = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');   // let window.onload → switchTab('today') settle
  await page.evaluate(() => switchTab('sessions'));
  await expect(page.locator('#content-sessions')).toBeVisible();
  await expect(page.locator('#sessions-list [data-session-id]').first()).toBeAttached();
  // Idle sessions live inside collapsed <details> — open them so any card is reachable.
  await page.locator('#sessions-list details').evaluateAll((els) => els.forEach((d) => (d.open = true)));
};

test('session card renders its linked tasks; a task chip opens the task drawer', async ({ page }) => {
  await openSessions(page);

  // The first session card + the first task in the system.
  const sid = await page.locator('#sessions-list [data-session-id]').first().getAttribute('data-session-id');
  expect(sid).toBeTruthy();
  const taskId = await page.evaluate(async () => (await fetch('/api/tasks?limit=0').then((r) => r.json())).tasks[0].id);

  // Link that task to the session via the EXISTING endpoint, then re-render.
  await page.evaluate(
    async ({ taskId, sid }) => {
      await fetch(`/api/tasks/${taskId}/session`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_id: sid }),
      });
      await loadSessions();
    },
    { taskId, sid },
  );
  await page.locator('#sessions-list details').evaluateAll((els) => els.forEach((d) => (d.open = true)));

  // The card now shows a session→tasks panel with a clickable task chip.
  const chip = page
    .locator(`#sessions-list [data-session-id="${sid}"] [data-session-tasks] button[onclick*="openEntity('task'"]`)
    .first();
  await expect(chip).toBeVisible();

  // Clicking the chip opens the TASK in the drawer (not the live-terminal panel).
  await chip.click();
  await expect(page.locator('#entity-drawer')).toBeVisible();
  await expect(page.locator('#ed-kind')).toHaveText('task');
});
