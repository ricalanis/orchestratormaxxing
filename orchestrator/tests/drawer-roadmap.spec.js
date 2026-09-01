// e2e for the roadmap AFTER the initiative fold (spec §1: "Initiative → folded
// into Project. Roadmap fields move onto projects").
//
// RE-PINNED from P0-10. The old file asserted `#roadmap-container
// [data-initiative-id]` cards, an initiative→project breadcrumb hop and a
// per-initiative burndown — all of which described the *deleted* noun. Every
// assertion here is the same question asked of the surviving one: the Roadmap
// renders PROJECTS grouped by quarter, a card opens the PROJECT drawer, and the
// chain stays clickable down to a task. The last test is the new ratchet — the
// initiative creation surface must stay gone.
//
// The initiative DRAWER itself is not tested here and is deliberately still
// alive: it is read-only archive, and tests/drawer-core.spec.js already deep-
// links it. The 410 on both write frontends lives in tests/test_initiatives_410.py.
//
// Runs against the wiped-cycle DB copy from playwright.config.js (projects and
// tasks are preserved — only cycles are wiped).
//
// Run: `npx playwright test drawer-roadmap` (webServer auto-starts one), or
//      `PW_BASE_URL=http://127.0.0.1:8931 npx playwright test drawer-roadmap`.
const { test, expect } = require('@playwright/test');

const DRAWER = '#entity-drawer';
const KIND = '#ed-kind';
const CARD = '#roadmap-container [data-roadmap-project-id]';
const childBtn = (type) => `${DRAWER} #ed-body button[onclick*="openEntity('${type}'"]`;

const openRoadmap = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');   // let window.onload → switchTab('today') settle
  await page.evaluate(() => switchTab('roadmap'));
  await expect(page.locator('#content-roadmap')).toBeVisible();
  await expect(page.locator(CARD).first()).toBeVisible();
};

// The roadmap payload, so a spec can pick a card by DATA instead of guessing a
// project name that a DB copy may or may not carry.
const roadmap = async (page) => (await page.request.get('/api/roadmap')).json();

test.describe('Roadmap → projects by quarter (initiative fold)', () => {
  test('project card opens the PROJECT drawer', async ({ page }) => {
    await openRoadmap(page);

    // Click the card TITLE (bubbles to the card's own openEntity).
    await page.locator(CARD).first().locator('.font-medium').first().click();

    await expect(page.locator(DRAWER)).toBeVisible();
    await expect(page.locator(KIND)).toHaveText('project');

    await page.keyboard.press('Escape');
    await expect(page.locator(DRAWER)).toBeHidden();
    expect(new URL(page.url()).searchParams.get('entity')).toBeNull();
  });

  test('cards are grouped by quarter, unscheduled projects included and last', async ({ page }) => {
    await openRoadmap(page);
    const { quarters } = await roadmap(page);

    const groups = page.locator('#roadmap-container [data-roadmap-quarter]');
    await expect(groups).toHaveCount(quarters.length);

    // Every group renders its quarter as the heading; the null bucket — a
    // project without a quarter is still a project — is labelled and sorts last.
    for (let i = 0; i < quarters.length; i++) {
      const expected = quarters[i].quarter || 'Sin quarter';
      await expect(groups.nth(i).locator('h3')).toHaveText(expected);
    }
    // Every live project has a card: the fold must not hide projects.
    const total = quarters.reduce((n, g) => n + g.projects.length, 0);
    await expect(page.locator(CARD)).toHaveCount(total);
  });

  test('a card shows DERIVED task progress, not a stored number', async ({ page }) => {
    await openRoadmap(page);
    const { quarters } = await roadmap(page);
    const withTasks = quarters.flatMap(g => g.projects).find(p => p.task_total > 0);
    test.skip(!withTasks, 'no project carries tasks in this DB copy');

    await expect(
      page.locator(`[data-roadmap-project-id="${withTasks.id}"] [data-roadmap-project-tasks]`)
    ).toContainText(`${withTasks.task_done}/${withTasks.task_total} done`);
  });

  test('Project → Task: a tasks-bearing card drills to its tasks', async ({ page }) => {
    await openRoadmap(page);
    const { quarters } = await roadmap(page);
    const withTasks = quarters.flatMap(g => g.projects).find(p => p.task_total > 0);
    test.skip(!withTasks, 'no project carries tasks in this DB copy');

    await page.locator(`[data-roadmap-project-id="${withTasks.id}"] .font-medium`).first().click();
    await expect(page.locator(KIND)).toHaveText('project');

    await expect(page.locator(childBtn('task')).first()).toBeVisible();
    await page.locator(childBtn('task')).first().click();
    await expect(page.locator(KIND)).toHaveText('task');
  });

  test('the initiative creation surface is gone from the roadmap', async ({ page }) => {
    await openRoadmap(page);
    // The ratchet: initiatives were folded into projects, so the Roadmap must
    // not offer the deleted noun back. No cards, no "+ Initiative", no modal.
    await expect(page.locator('#roadmap-container [data-initiative-id]')).toHaveCount(0);
    await expect(page.locator('#content-roadmap button', { hasText: '+ Initiative' })).toHaveCount(0);
    await expect(page.locator('#initiative-modal')).toHaveCount(0);
  });
});
