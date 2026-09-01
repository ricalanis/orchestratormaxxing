// e2e for the journey fase-1 step 2: the CONTEXT chip.
//
// Journey principle 2 — "el contexto es un linaje que viaja pegado al átomo":
// a task never carries just *which project*, it carries *who it is for*. The
// one chip every task surface renders therefore has three mutually-exclusive
// branches, ordered most-specific-first:
//
//   1. task.deal_id      → the CLIENT chip (commercial lineage outranks
//                          delivery lineage). DORMANT until m06 ships
//                          tasks.deal_id in step 4 — the code is live, the
//                          data is not, so it is covered here by a synthetic
//                          fixture rather than a seeded row (see below).
//   2. the inbox project → a deliberately DISTINCT gray "sin triar" chip.
//                          Live day one: the inbox is the ABSENCE of context,
//                          and rendering it in the same pill as a real project
//                          disguised untriaged work as triaged work.
//   3. otherwise         → the project chip as before, its dot tinted by the
//                          delivering account when the project carries one.
//
// HONEST-OPTION NOTES (both verified, not assumed):
//  * Inbox resolution is client-side via `PROJECTS_BY_ID[project_id].slug ===
//    'inbox'`. That is the signal the page ALREADY uses for the board's 📥
//    badge, and it needs no new endpoint. `identity.inbox_id` is server-side
//    and is not in any payload the page fetches; the project *name*
//    ("Inbox · untriaged") is display text and would break on a rename.
//  * The deal branch is exercised by calling contextChip() with a synthetic
//    task through page.evaluate, NOT by seeding a row. Seeding is impossible
//    today by construction: `tasks` has no deal_id COLUMN yet (m06 adds it in
//    step 4), so both the request fixture and a direct sandbox-DB insert would
//    fail on "no such column". Testing the pure render function against a
//    synthetic atom is the only honest way to keep the dormant branch covered.
//
// Run: `npx playwright test board-context-chip`.
const { test, expect } = require('@playwright/test');

// The board renders its cards into #my-kanban (the My-Tasks workspace), and a
// task's chip lives inside that card's .card-chips row.
const BOARD_CARD = '#my-kanban .kanban-card';

async function openBoard(page) {
  await page.goto('/?tab=board');
  await page.waitForLoadState('networkidle');
  await page.waitForFunction(() => document.querySelectorAll('#my-kanban .kanban-card').length > 0);
}

// Resolve, from the page's own loaded state, the id of a rendered board card
// whose project matches `pred(project)`. Returns null when none is rendered.
async function findCardId(page, kind) {
  return page.evaluate((kind) => {
    const inboxIds = Object.values(PROJECTS_BY_ID).filter(p => p.slug === 'inbox').map(p => p.id);
    for (const el of document.querySelectorAll('#my-kanban .kanban-card')) {
      const t = (loadedTasks || []).find(x => x.id === el.dataset.taskId);
      if (!t || !t.project_id) continue;
      // Branch 1 (deal_id → CLIENT chip) outranks BOTH branches this helper picks
      // for. It was dormant when this spec was written ("the code is live, the data
      // is not"); m06 has since shipped tasks.deal_id, so real cards now carry it —
      // and the first non-inbox task happened to be one, which is why this spec
      // started expecting `project` and getting `client`. The precedence is correct
      // and deliberate; the SELECTOR was what assumed dormancy. Skip deal-bearing
      // tasks here — branch 1 has its own dedicated test below.
      if (t.deal_id) continue;
      const isInbox = inboxIds.includes(t.project_id);
      if (kind === 'inbox' ? isInbox : (!isInbox && PROJECTS_BY_ID[t.project_id])) return t.id;
    }
    return null;
  }, kind);
}

test.describe('context chip (journey fase 1, step 2)', () => {
  test('an inbox task reads "sin triar", not a project chip', async ({ page }) => {
    await openBoard(page);
    const id = await findCardId(page, 'inbox');
    expect(id, 'the fixture DB must render at least one inbox task on the board').not.toBeNull();

    const chip = page.locator(`${BOARD_CARD}[data-task-id="${id}"] [data-context-chip]`);
    await expect(chip).toHaveAttribute('data-context-chip', 'untriaged');
    await expect(chip).toContainText('sin triar');
    // The whole point of the branch: it must NOT wear the project's display
    // name, which is what disguised untriaged work as triaged work.
    await expect(chip).not.toContainText('Inbox');
  });

  test('a non-inbox task still renders its project chip', async ({ page }) => {
    await openBoard(page);
    const id = await findCardId(page, 'project');
    expect(id, 'the fixture DB must render at least one non-inbox project task').not.toBeNull();

    const expected = await page.evaluate((tid) => {
      const t = (loadedTasks || []).find(x => x.id === tid);
      return PROJECTS_BY_ID[t.project_id].name;
    }, id);

    const chip = page.locator(`${BOARD_CARD}[data-task-id="${id}"] [data-context-chip]`);
    await expect(chip).toHaveAttribute('data-context-chip', 'project');
    await expect(chip).toContainText(expected);
  });

  test('a task carrying deal_id renders the client chip (dormant branch)', async ({ page }) => {
    await openBoard(page);
    const out = await page.evaluate(() => {
      const html = contextChip({
        id: 't_synthetic', project_id: 'proj_inbox',
        deal_id: 'd_fixture01', account_id: 'acc_fixture01', account_name: 'Fixture Client SA',
      });
      // Render it for real so the assertions read the parsed DOM, not a string.
      const box = document.createElement('div');
      box.innerHTML = html;
      const el = box.firstElementChild;
      const dot = el.querySelector('span[style]');
      return {
        kind: el.getAttribute('data-context-chip'),
        text: el.textContent.trim(),
        onclick: el.getAttribute('onclick') || '',
        dot: dot ? dot.getAttribute('style') : '',
        // Determinism of the account tint: same account → same colour, always.
        tintA: accountTint('acc_fixture01'),
        tintB: accountTint('acc_fixture01'),
        tintOther: accountTint('acc_someone_else'),
      };
    });

    expect(out.kind).toBe('client');
    // The CLIENT wins over the project, even though this synthetic task also
    // sits in the inbox — the branches are ordered, not additive.
    expect(out.text).toContain('Fixture Client SA');
    expect(out.text).not.toContain('sin triar');
    expect(out.onclick).toContain("openEntity('deal', 'd_fixture01')");
    // Accounts have no colour column, so the dot is a derived tint — it must be
    // deterministic (same input → same colour) or a client changes colour on
    // every render, which is worse than no colour at all.
    expect(out.tintA).toBe(out.tintB);
    expect(out.dot).toContain(out.tintA);
  });

  test('every call site renders the chip, and none was missed', async ({ page }) => {
    // (a) the board, live: real cards, real payload, real DOM.
    await openBoard(page);
    expect(await page.locator(`${BOARD_CARD} [data-context-chip]`).count()).toBeGreaterThan(0);

    // (b) all six call sites, exercised directly. Counting live rows in the
    // cycle/backlog drawers would make this contract depend on how many tasks
    // happen to be scheduled — mutable state that other specs churn, so it
    // would report "broken" when nothing is broken. Instead each renderer is
    // called with a REAL task re-pointed at the inbox project: a real object
    // keeps every field the renderers read, and the re-point makes the expected
    // branch deterministic.
    const rendered = await page.evaluate(() => {
      const t = Object.assign({}, (loadedTasks || [])[0], { project_id: 'proj_inbox', deal_id: null });
      renderCycleIcebox({ has_active: true, icebox: [t] });
      return {
        cycleCard: cycleCard(t),
        weekDrawerRow: weekDrawerRow(t, ''),
        renderCycleIcebox: (document.getElementById('cycle-icebox') || {}).innerHTML || '',
        createBacklogCard: createBacklogCard(t, true),
        renderBacklogTriage: renderBacklogTriage([t]),
        createTaskCard: createTaskCard(t, 'mine'),
      };
    });
    expect(Object.keys(rendered)).toHaveLength(6);
    for (const [site, html] of Object.entries(rendered)) {
      expect(html, `${site} must render a context chip`).toContain('data-context-chip="untriaged"');
    }

    // (c) Hoy + the plan cards — todayLane is a SEPARATE renderer fed by the
    // canvas payload's project_name/project_color (step 4 widens that payload).
    // This is the regression guard that the index.html edits left it intact,
    // asserted on the renderer rather than on a count of plan cards for the
    // same reason as (b): how many cards sit in today's plan is mutable state.
    await page.goto('/');
    await page.waitForLoadState('networkidle');
    await page.waitForFunction(() => document.querySelectorAll('#content-today [data-task-id]').length > 0);
    expect(await page.locator('#content-today [data-task-id]').count()).toBeGreaterThan(0);
    const lane = await page.evaluate(() =>
      todayLane({ project_name: 'Proyecto Fixture', project_color: '#123456' }));
    expect(lane).toContain('Proyecto Fixture');
    expect(lane).toContain('#123456');

    // (d) the deterministic sweep: the old name is gone from the served page and
    // every one of the six interpolated call sites now calls the new renderer.
    const src = await page.request.get('/').then(r => r.text());
    expect(src.match(/projectChip/g)).toBeNull();
    expect((src.match(/\$\{contextChip\(/g) || []).length).toBe(6);
  });
});
