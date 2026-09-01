// e2e for the shared entity-drawer core contract (P0-4), the foundation the
// CRM/Roadmap/Sessions drilldowns build on: deep-link opens the drawer for EVERY
// entity type, the breadcrumb navigates UP the spine, browser Back walks the
// navigation stack, and Esc closes + clears the URL. Runs against the wiped-cycle
// DB copy from playwright.config.js (all entities preserved).
//
// Run: `npx playwright test drawer-core`.
const { test, expect } = require('@playwright/test');

const DRAWER = '#entity-drawer';
const KIND = '#ed-kind';

// One real id per entity type, derived from the live APIs (no hardcoded ids).
const gatherIds = (page) =>
  page.evaluate(async () => {
    const j = async (u) => await fetch(u).then((r) => r.json());
    const tasks = (await j('/api/tasks?limit=0')).tasks || [];
    const projects = await j('/api/projects');
    const roadmap = await j('/api/roadmap');
    const pipe = await j('/api/crm/pipeline');
    const firstDeal = Object.values(pipe.by_stage || {}).flat()[0];
    const sessions = await j('/api/sessions');
    const sess = [...(sessions.claude_code || []), ...(sessions.opencode || [])][0];
    return {
      task: (tasks.find((t) => t.project_id) || tasks[0] || {}).id,   // one with a project ancestor
      project: (projects[0] || {}).id,
      initiative: ((roadmap.initiatives || [])[0] || {}).id,
      deal: (firstDeal || {}).id,
      session: (sess || {}).session_id,
    };
  });

test.describe('Entity drawer core contract (P0-4 / P0-12)', () => {
  test('deep-link ?entity=<type>:<id> opens the drawer for every entity type', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    const ids = await gatherIds(page);

    for (const type of ['task', 'project', 'initiative', 'deal', 'session']) {
      const id = ids[type];
      expect(id, `no ${type} id available in the test DB`).toBeTruthy();
      await page.goto(`/?entity=${type}:${encodeURIComponent(id)}`);
      await page.waitForLoadState('networkidle');
      await expect(page.locator(DRAWER)).toBeVisible();
      await expect(page.locator(KIND)).toHaveText(type);
    }
    // Unknown type / missing id degrade gracefully (no drawer, no crash).
    await page.goto('/?entity=widget:nope');
    await page.waitForLoadState('networkidle');
    await expect(page.locator(DRAWER)).toBeHidden();
  });

  test('breadcrumb navigates up the spine; Back returns; Esc closes + clears the URL', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    const ids = await gatherIds(page);

    await page.goto(`/?entity=task:${ids.task}`);
    await page.waitForLoadState('networkidle');
    await expect(page.locator(KIND)).toHaveText('task');

    // Project ancestor in the breadcrumb → drawer switches to the project.
    const projCrumb = page.locator(`${DRAWER} #ed-crumb button[onclick*="openEntity('project'"]`).first();
    await expect(projCrumb).toBeVisible();
    await projCrumb.click();
    await expect(page.locator(KIND)).toHaveText('project');
    expect(new URL(page.url()).searchParams.get('entity')).toMatch(/^project:/);

    // Browser Back returns to the task (the pushState navigation stack).
    await page.goBack();
    await expect(page.locator(KIND)).toHaveText('task');

    // Esc closes the drawer and clears ?entity=.
    await page.keyboard.press('Escape');
    await expect(page.locator(DRAWER)).toBeHidden();
    expect(new URL(page.url()).searchParams.get('entity')).toBeNull();
  });
});
