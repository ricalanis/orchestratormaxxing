// e2e for P1-2: full URL routing — the current tab lands in ?tab=<key>, the open
// entity in ?entity=<type>:<id>, browser Back walks the history, and deep-links
// (incl. the ?tab=board&task=… shorthand) restore both on load. Builds on the
// P0-4 ?entity= drawer routing. Runs against the wiped-cycle DB copy.
//
// Run: `npx playwright test routing`.
const { test, expect } = require('@playwright/test');

test.describe('URL routing (P1-2)', () => {
  test('switching tabs reflects in ?tab= and browser Back walks the history', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    await expect(page).not.toHaveURL(/tab=/);   // Today = clean base URL

    await page.evaluate(() => switchTab('board'));
    await expect(page).toHaveURL(/[?&]tab=board(&|$)/);

    await page.evaluate(() => switchTab('roadmap'));
    await expect(page).toHaveURL(/[?&]tab=roadmap(&|$)/);

    await page.goBack();
    await expect(page).toHaveURL(/[?&]tab=board(&|$)/);
    await expect(page.locator('#content-board')).toBeVisible();

    await page.goBack();
    await expect(page).not.toHaveURL(/tab=/);
    await expect(page.locator('#content-today')).toBeVisible();
  });

  test('deep-link ?tab= restores the tab (and its workspace) on load', async ({ page }) => {
    await page.goto('/?tab=roadmap');
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#content-roadmap')).toBeVisible();
    // Nav 8→5: roadmap is a Work sub-view since the consolidation.
    await expect(page.locator('#ws-work')).toHaveClass(/active/);
  });

  test('Projects deep-link preserves the canonical selected project', async ({ page }) => {
    const projects = await page.request.get('/api/projects').then(r => r.json());
    const projectId = projects[0].id;
    await page.goto(`/?tab=projects&project_id=${encodeURIComponent(projectId)}`);
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#content-projects')).toBeVisible();
    await expect(page.locator('#projects-select')).toHaveValue(projectId);
    const u = new URL(page.url());
    expect(u.searchParams.get('tab')).toBe('projects');
    expect(u.searchParams.get('project_id')).toBe(projectId);
  });

  test('deep-link ?tab=board&task= opens board + drawer, canonicalized to ?entity=', async ({ page }) => {
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    const taskId = await page.evaluate(async () => (await fetch('/api/tasks?limit=0').then((r) => r.json())).tasks[0].id);

    await page.goto(`/?tab=board&task=${taskId}`);
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#content-board')).toBeVisible();
    await expect(page.locator('#entity-drawer')).toBeVisible();
    await expect(page.locator('#ed-kind')).toHaveText('task');

    // The shorthand ?task= is canonicalized to ?entity=, and ?tab= is preserved.
    const u = new URL(page.url());
    expect(u.searchParams.get('tab')).toBe('board');
    expect(u.searchParams.get('entity')).toBe(`task:${taskId}`);
    expect(u.searchParams.get('task')).toBeNull();
  });

  test('deep-link ?tab=sessions&open= opens that session\'s live modal, then drops the param', async ({ page }) => {
    // The Telegram notification link (agent-done-notify) — the modal must open
    // without depending on the sessions list, so stub only the output fetch.
    await page.route('**/api/sessions/*/*/output*', (route) =>
      route.fulfill({ json: { host: 'local', session: 'claude-e2e', kind: 'terminal', output: 'hola', messages: [] } }));
    await page.goto('/?tab=sessions&open=local/claude-e2e');
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#session-output-modal')).toBeVisible();
    await expect(page.locator('#session-output-title')).toContainText('claude-e2e');
    await expect(page.locator('#session-input-row')).toBeVisible();  // command composer offered
    expect(new URL(page.url()).searchParams.get('open')).toBeNull(); // param consumed

    // URL-controlled input: anything not matching <host>/<session> is rejected.
    await page.goto('/?tab=sessions&open=local/<img%20src=x>');
    await page.waitForLoadState('networkidle');
    await expect(page.locator('#session-output-modal')).toBeHidden();
  });
});
