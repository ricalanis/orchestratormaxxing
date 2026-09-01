// e2e for the Cycle tab search/filter (Playwright): typing in #cycle-search
// narrows the board cards to those whose project name OR title matches
// (case-insensitive); the ✕ clear button restores the full board.
//
// Self-contained: creates its own cycle + seeds 3 in_progress cards in beforeAll
// and DELETES it in afterAll so the blank-slate the board-empty-state spec relies
// on is restored (files run alphabetically; this one runs last). Run:
//   PW_BASE_URL=http://127.0.0.1:8931 npx playwright test
const { test, expect } = require('@playwright/test');

const CARDS = '#cycle-kanban .kanban-card';

test.describe('Cycle board search/filter', () => {
  let sprintId;
  let seeded = [];   // {id, title} for the 3 seeded cards

  test.beforeAll(async ({ request }) => {
    const cyc = await (await request.post('/api/sprints', { data: {} })).json();
    sprintId = cyc.id;
    await request.post(`/api/sprints/${sprintId}/start`);
    const tasks = (await (await request.get('/api/tasks')).json()).tasks.slice(0, 3);
    for (const t of tasks) {
      await request.patch(`/api/tasks/${t.id}/sprint`, { data: { sprint_id: sprintId } });
      await request.patch(`/api/tasks/${t.id}`, { data: { status: 'in_progress' } });
      seeded.push({ id: t.id, title: t.title || '' });
    }
  });

  test.afterAll(async ({ request }) => {
    if (sprintId) await request.delete(`/api/sprints/${sprintId}`);   // restore blank slate
  });

  test('filters cards by title (case-insensitive) and clears', async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    await page.evaluate(() => switchTab('cycle'));
    await expect(page.locator('#content-cycle')).toBeVisible();

    // OWN-YOUR-STATE (lq-4c53d622): this used to assert `CARDS === 3`, true only on
    // a pristine DB. The fixture copies the operator's REAL kanban.db (sprints
    // wiped, nothing else) and a cycle board also shows UNCOMMITTED tasks whose
    // scheduled_week falls in that cycle's ISO week — so the board legitimately
    // carried 10 cards and the spec died at its pre-condition, never reaching the
    // filter it exists to test. Assert the seeded cards + a stable baseline count.
    const search = page.locator('#cycle-search');
    await expect(search).toBeVisible();
    for (const s of seeded) {
      await expect(page.locator(`${CARDS}[data-task-id="${s.id}"]`)).toBeVisible();
    }
    const baseline = await page.locator(CARDS).count();
    expect(baseline).toBeGreaterThanOrEqual(3);
    await expect(page.locator('#cycle-search-clear')).toBeHidden();

    // A query that matches nothing → zero cards; the clear button appears.
    await search.fill('zzz_no_such_card_qwerty');
    await expect(page.locator(CARDS)).toHaveCount(0);
    await expect(page.locator('#cycle-search-clear')).toBeVisible();

    // A substring of one card's title, typed in a DIFFERENT case, still matches
    // that card (case-insensitive) — proving the filter and case-folding work.
    const target = seeded.find(s => s.title.trim().length >= 4) || seeded[0];
    const frag = target.title.trim().slice(0, 5);
    await search.fill(frag.toUpperCase());
    await expect(page.locator(`${CARDS}[data-task-id="${target.id}"]`)).toBeVisible();
    const filtered = await page.locator(CARDS).count();
    expect(filtered).toBeGreaterThanOrEqual(1);
    expect(filtered).toBeLessThan(baseline);      // it genuinely narrowed the board

    // Clear button empties the input and restores the full board.
    await page.click('#cycle-search-clear');
    await expect(search).toHaveValue('');
    await expect(page.locator(CARDS)).toHaveCount(baseline);
    await expect(page.locator('#cycle-search-clear')).toBeHidden();
  });

  test('"/" focuses the search; Escape clears and blurs it', async ({ page }) => {
    await page.setViewportSize({ width: 1400, height: 900 });
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    await page.evaluate(() => switchTab('cycle'));
    await expect(page.locator('#content-cycle')).toBeVisible();

    const search = page.locator('#cycle-search');
    await expect(search).toBeVisible();
    await expect(search).not.toBeFocused();
    // Baseline read from the live board, not assumed (see the note in test 1).
    const baseline = await page.locator(CARDS).count();
    expect(baseline).toBeGreaterThanOrEqual(3);
    // The "/" hint shows while empty & unfocused.
    await expect(page.locator('#cycle-search-hint')).toBeVisible();

    // Press "/" anywhere (not in a field) → focus lands in the search box, and
    // the slash is NOT typed into it.
    await page.keyboard.press('/');
    await expect(search).toBeFocused();
    await expect(search).toHaveValue('');
    await expect(page.locator('#cycle-search-hint')).toBeHidden();   // hidden while focused

    // Type a filter, then Escape → clears the value, blurs, restores the board.
    await search.fill('zzz_no_such_card');
    await expect(page.locator(CARDS)).toHaveCount(0);
    await page.keyboard.press('Escape');
    await expect(search).toHaveValue('');
    await expect(search).not.toBeFocused();
    await expect(page.locator(CARDS)).toHaveCount(baseline);
  });
});
