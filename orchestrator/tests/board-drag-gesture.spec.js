// e2e for the unified Board drag GESTURE — the board half of the operator's two
// verbatim complaints about "el kanban del backlog no arrastra correctamente":
//
//   (a) "se selecciona el texto cuando quiero arrastrar y cambia la intención"
//       Every board here runs SortableJS 1.15 with forceFallback:true, so there is
//       NO native HTML5 drag to suppress the browser's selection gesture, and
//       Sortable ships zero user-select handling of its own — it calls
//       getSelection().removeAllRanges() exactly once, inside _triggerDragStart,
//       which fires only AFTER fallbackTolerance (4px) is crossed and while the
//       button is still down, so the live selection simply re-extends from its
//       anchor. Suppression has to be CSS. Test 1 pins it.
//
//   (b) "es difícil atinar al área donde permite el cambio"
//       Sortable is bound to .kanban-list, never to .kanban-column. A column is
//       min-height:400px while its list was content-height (floor 3rem) — so in a
//       column holding one card roughly 300px of obviously-droppable-looking
//       column was silently dead. Tests 2 + 3 pin the geometry AND a real drop
//       aimed at the bottom edge of the column.
//
// Own-your-state (lq-4c53d622): BORROWS one existing task, targets it by id, and
// restores its status in afterAll. It never asserts on ambient column counts —
// which is precisely how the older board-* specs rotted (see the report).
//
// NB: never POST /api/tasks from an e2e spec — that route shells out to the
// `hermes` CLI, which does NOT honour $HERMES_KANBAN_DB and would write straight
// into the operator's REAL ~/.hermes/kanban.db. PATCH {status} is safe (it goes
// through sprints.set_task_status, pure Python against the temp copy).
//
// Run: `npx playwright test board-drag-gesture`.
const { test, expect } = require('@playwright/test');

const COL = (key) => `#board-kanban .kanban-list[data-col="${key}"]`;
const COLUMN = (key) => `#board-kanban .kanban-column[data-column="${key}"]`;
const cardSel = (id) => `#board-kanban .kanban-card[data-task-id="${id}"]`;

// Land on the kanban board with every filter wide open, before any app JS runs —
// localStorage is where BOARD_MODE / BOARD_WHO / BOARD_WHEN are read from.
// (`setBoardLens` no longer exists; the lens model is WHO × WHEN × MODE.)
async function openBoard(page) {
  await page.addInitScript(() => {
    localStorage.setItem('boardMode', 'kanban');
    localStorage.setItem('boardWho', 'all');
    localStorage.setItem('boardWhen', 'all');
  });
  await page.setViewportSize({ width: 1600, height: 950 });
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('board'));
  await expect(page.locator('#content-board')).toBeVisible();
  await expect(page.locator(COLUMN('pool_inbox'))).toBeVisible();
}

const taskStatus = async (request, id) => {
  const j = await (await request.get(`/api/tasks/${id}`)).json();
  return (j.task || j || {}).status;
};

test.describe('Board drag gesture', () => {
  let taskId, priorStatus;

  test.afterAll(async ({ request }) => {
    if (taskId && priorStatus) await request.patch(`/api/tasks/${taskId}`, { data: { status: priorStatus } });
  });

  // Borrow a card that is ACTUALLY RENDERED, read off the board itself — picking
  // one from /api/tasks instead hands back rows the board legitimately filters out
  // (the first non-archived task here is `done`, which the Done column caps away),
  // and the spec then fails for a reason that has nothing to do with dragging.
  async function borrowRenderedCard(page, request) {
    const picked = await page.evaluate(() => {
      const cols = [...document.querySelectorAll('#board-kanban .kanban-column')];
      // Prefer the fullest column as the SOURCE so a card is certainly there.
      cols.sort((a, b) => b.querySelectorAll('.kanban-card').length - a.querySelectorAll('.kanban-card').length);
      const card = cols[0] && cols[0].querySelector('.kanban-card');
      return card ? { id: card.getAttribute('data-task-id'), col: cols[0].getAttribute('data-column') } : null;
    });
    expect(picked, 'a rendered board card exists to borrow').toBeTruthy();
    taskId = picked.id;
    priorStatus = await taskStatus(request, taskId);
    expect(priorStatus, 'borrowed card has a server status').toBeTruthy();
    return picked;
  }

  // ---- RED-PROOF ---------------------------------------------------------
  // Against the pre-fix template this fails: with no user-select:none on
  // .kanban-card, mousedown + move selects the card's title text and the gesture
  // reads as a text drag instead of a card drag.
  test('starting a drag on a board card selects NO text', async ({ page }) => {
    await openBoard(page);
    const card = page.locator(`#board-kanban .kanban-card`).first();
    await expect(card).toBeVisible();
    await card.scrollIntoViewIfNeeded();

    const s = await card.boundingBox();
    await page.mouse.move(s.x + s.width / 2, s.y + 14);
    await page.mouse.down();
    // Well past fallbackTolerance (4px) — the window the native selection won.
    await page.mouse.move(s.x + s.width / 2 + 40, s.y + 44, { steps: 12 });
    await page.mouse.move(s.x + s.width / 2 + 90, s.y + 84, { steps: 12 });

    // The gesture is genuinely a drag — otherwise "nothing got selected" is trivia
    // (a press that misses the card selects nothing either, and looks green).
    expect(await page.evaluate(() => document.body.classList.contains('dragging')),
           'SortableJS actually started the drag').toBe(true);
    const sel = await page.evaluate(() => String(window.getSelection() || ''));
    await page.mouse.up();
    expect(sel, 'drag gesture must not select card text').toBe('');
  });

  // ---- RED-PROOF ---------------------------------------------------------
  // The structural half: pre-fix the list stopped at its content while the column
  // stayed 400px tall, so the gap below it was a dead strip that still LOOKED
  // like the column.
  test('every board list FILLS its column — no dead strip at the bottom', async ({ page }) => {
    await openBoard(page);
    const gaps = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll('#board-kanban .kanban-column').forEach((col) => {
        const list = col.querySelector(':scope > .kanban-list');
        if (!list) return;
        const cb = col.getBoundingClientRect(), lb = list.getBoundingClientRect();
        // Anything rendered after the list (e.g. the quick-add ghost) legitimately
        // owns space — MARGINS included, or the column's own padding reads as a gap.
        let after = 0;
        for (let n = list.nextElementSibling; n; n = n.nextElementSibling) {
          const cs = getComputedStyle(n);
          after += n.getBoundingClientRect().height
                 + parseFloat(cs.marginTop) + parseFloat(cs.marginBottom);
        }
        out.push({ col: col.getAttribute('data-column'), gap: cb.bottom - lb.bottom - after });
      });
      return out;
    });
    expect(gaps.length, 'the four board columns rendered').toBeGreaterThan(0);
    // Tolerance covers the column's own bottom padding (p-3 = 12px) + subpixel.
    // Pre-fix this gap was THOUSANDS of px, so the bound is not delicate.
    for (const g of gaps) expect(g.gap, `dead strip under the ${g.col} column`).toBeLessThanOrEqual(16);
  });

  // ---- RED-PROOF ---------------------------------------------------------
  // The user-facing half: a drop aimed at the BOTTOM EDGE of the emptiest column.
  // Pre-fix that point sat outside .kanban-list, so Sortable never adopted it and
  // the card snapped home with no status change.
  test('a card dropped at the BOTTOM of a column lands in that column', async ({ page, request }) => {
    await openBoard(page);
    await borrowRenderedCard(page, request);
    await expect(page.locator(cardSel(taskId))).toHaveCount(1);

    // Choose the target from MEASURED slack, never a hard-coded column: how tall
    // each column's content is depends on live data, and a column that happens to
    // be full has no dead strip at all — a test pinned to it would pass with the
    // bug intact (exactly how the older board-* specs rotted). `done` is excluded:
    // it is a terminal write.
    const target = await page.evaluate((id) => {
      const own = document.querySelector(`.kanban-card[data-task-id="${id}"]`)
        .closest('.kanban-column').getAttribute('data-column');
      const cols = [...document.querySelectorAll('#board-kanban .kanban-column')].map((c) => {
        const list = c.querySelector(':scope > .kanban-list');
        if (!list) return null;
        const cards = list.querySelectorAll('.kanban-card');
        const last = cards[cards.length - 1];
        const cb = c.getBoundingClientRect();
        const contentBottom = last ? last.getBoundingClientRect().bottom
                                   : list.getBoundingClientRect().top;
        return { key: c.getAttribute('data-column'), status: list.getAttribute('data-status'),
                 contentBottom, colBottom: cb.bottom, x: cb.x + cb.width / 2,
                 slack: cb.bottom - contentBottom };
      }).filter(Boolean).filter((c) => c.key !== own && c.key !== 'done');
      cols.sort((a, b) => b.slack - a.slack);
      return cols[0] || null;
    }, taskId);
    expect(target, 'a non-own target column with a lower dead strip exists').toBeTruthy();
    expect(target.slack, 'the chosen column really has empty body below its cards').toBeGreaterThan(150);

    const card = page.locator(cardSel(taskId));
    await card.scrollIntoViewIfNeeded();
    const s = await card.boundingBox();
    const wantStatus = target.status;

    // Aim BELOW the target column's last card — the strip that looked droppable and
    // was not (an empty list floors at min-height:3rem, and emptyInsertThreshold
    // only reaches 24px past it). Clamped to the viewport: these columns run
    // thousands of px tall, so the literal bottom edge is unreachable for a real
    // user and for a mouse driver alike.
    const x = target.x;
    const y = Math.min(target.contentBottom + 120, target.colBottom - 10, 900);
    expect(y, 'the drop point is below the target column content').toBeGreaterThan(target.contentBottom);

    await page.mouse.move(s.x + s.width / 2, s.y + 14);
    await page.mouse.down();
    await page.mouse.move(s.x + s.width / 2, s.y + 26, { steps: 6 });
    // Fail here rather than downstream if the press missed the card entirely.
    expect(await page.evaluate(() => document.body.classList.contains('dragging')),
           'SortableJS took the gesture').toBe(true);
    // Descend first, then cross (an L, not a diagonal): a straight line clips the
    // TOP of the target list, where cards already live, so Sortable would adopt the
    // card there and the drop would "work" with the dead strip intact.
    await page.mouse.move(s.x + s.width / 2, y, { steps: 14 });
    await page.mouse.move(x, y, { steps: 20 });
    await page.mouse.move(x, y + 2, { steps: 3 });
    await page.waitForTimeout(150);
    await page.mouse.up();

    // It landed in the target column…
    await expect(page.locator(`${COL(target.key)} .kanban-card[data-task-id="${taskId}"]`)).toBeVisible({ timeout: 8000 });
    // …and it persisted.
    await expect.poll(() => taskStatus(request, taskId), { timeout: 8000 }).toBe(wantStatus);
  });
});
