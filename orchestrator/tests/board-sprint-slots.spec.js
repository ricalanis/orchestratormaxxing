// The unified Board must consume exact calendar slots, never ordinal future rows.
const { test, expect } = require('@playwright/test');

const W33 = { id: 'active-w33', project_id: null, name: 'Cycle 2026-W33', status: 'active', start_date: 1786341600, end_date: 1786946399 };
const W34 = { id: 'next-w34', project_id: null, name: 'Cycle 2026-W34', status: 'planning', start_date: 1786946400, end_date: 1787551199 };
const W35 = { id: 'plus2-w35', project_id: null, name: 'Cycle 2026-W35', status: 'planning', start_date: 1787551200, end_date: 1788155999 };
const POLLUTED = [
  { id: 'polluted-w21', project_id: null, name: 'Cycle 2027-W21', status: 'planning', start_date: 1811138400, end_date: 1811743199 },
  { id: 'polluted-w36', project_id: null, name: 'Cycle 2027-W36', status: 'planning', start_date: 1820752376, end_date: 1821357176 },
];

const slot = (offset, iso, start, end, cycle) => ({ offset, iso_week: iso, start_date: start, end_date: end, cycle });

async function openBoardWith(page, sprints, slots, onCreate = null) {
  await page.route('**/api/sprints', route => {
    if (route.request().method() === 'POST') {
      if (onCreate) onCreate(route.request().postDataJSON());
      return route.fulfill({ json: { id: 'created-slot', status: 'created' } });
    }
    return route.fulfill({ json: sprints });
  });
  await page.route('**/api/sprints/slots', route => route.fulfill({ json: slots }));
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('board'));
  await expect(page.locator('#content-board')).toBeVisible();
  await expect.poll(() => page.evaluate(() => BOARD_CYCLE && BOARD_CYCLE.id))
    .toBe(W33.id);
  await expect(page.locator('#board-when button')).toHaveCount(4);
  await expect(page.locator('#board-when')).toContainText('W33');
}

test.describe('Board exact sprint slots', () => {
  test('distant polluted cycles do not become Next or +2', async ({ page }) => {
    await openBoardWith(page, [...POLLUTED, W33], {
      anchor: W33,
      next: slot(1, '2026-W34', 1786946400, 1787551199, null),
      plus2: slot(2, '2026-W35', 1787551200, 1788155999, null),
    });

    const labels = await page.locator('#board-when button').allTextContents();
    expect(labels.join(' ')).toContain('W33');
    expect(labels.join(' ')).toContain('Next');
    expect(labels.join(' ')).toContain('+2');
    expect(labels.join(' ')).not.toContain('W21');
    expect(labels.join(' ')).not.toContain('W36');
  });

  test('planned W34/W35 occupy only their exact slots regardless of API order', async ({ page }) => {
    const projectW34 = { ...W34, id: 'project-w34', project_id: 'proj-client', name: 'Client W34' };
    await openBoardWith(page, [POLLUTED[1], W35, projectW34, W33, POLLUTED[0], W34], {
      anchor: W33,
      next: slot(1, '2026-W34', 1786946400, 1787551199, W34),
      plus2: slot(2, '2026-W35', 1787551200, 1788155999, W35),
    });

    const labels = await page.locator('#board-when button').allTextContents();
    expect(labels).toEqual(expect.arrayContaining([
      expect.stringContaining('W33'),
      expect.stringContaining('W34'),
      expect.stringContaining('W35'),
    ]));
    expect(labels.join(' ')).not.toContain('W21');
    expect(labels.join(' ')).not.toContain('W36');
  });

  test('planning a missing Next posts W34 even when W35 already exists', async ({ page }) => {
    let created = null;
    await openBoardWith(page, [POLLUTED[0], W35, W33], {
      anchor: W33,
      next: slot(1, '2026-W34', 1786946400, 1787551199, null),
      plus2: slot(2, '2026-W35', 1787551200, 1788155999, W35),
    }, body => { created = body; });

    await page.evaluate(async () => {
      BOARD_WHEN = 'next';
      await planNextSprint();
    });
    expect(created).toEqual({ start_date: 1786946400 });
  });
});
