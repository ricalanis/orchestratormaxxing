// e2e contract for the Today tab's information architecture: the organizer
// leads the page, everything else lives in one of two named collapsible
// groups (Growth / Reference), and a highlight chip strip deep-links into them.
//
// Re-pinned 2026-08-02: 🕐 Rhythm was DELETED (all five of its widgets were
// measured dead or duplicated — see the note in index.html). This file used to
// drive its assertions through Rhythm because it was the one group open by
// default; every one of those now runs through Growth, which means the toggle
// tests must expand before they collapse instead of the other way around.
//
// The load-bearing assertion is #3: collapse is CLASS-ONLY, so the 45s poll —
// which re-renders the zones — can never reopen a group the user closed. It was
// proven red against a variant whose toggle did not persist the class.
//
// Run: `npx playwright test today-groups`.
const { test, expect } = require('@playwright/test');

// switchTab fires loadToday() asynchronously; settle it before asserting, or the
// test measures the race instead of the product (same helper as today-plan).
const openToday = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('today'));
  await expect.poll(() => page.evaluate(() => (typeof TODAY_DATA !== 'undefined' && TODAY_DATA) ? 1 : 0),
    { timeout: 15000 }).toBe(1);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(400);
};

// Every test starts from CLEAN storage — a leftover key from a previous test
// would silently decide the default-state assertions.
const clearGroupKeys = (page) => page.evaluate(() =>
  ['growth', 'reference'].forEach(g => localStorage.removeItem('hermes_today_group_' + g)));

const collapsed = (page, g) => page.evaluate(
  g => document.getElementById('today-sec-' + g + '-body').classList.contains('hidden'), g);

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await clearGroupKeys(page);
});

// --- 1. the organizer leads the page ---------------------------------------
// the operator's ask: "move it top of the page". The Do zone must precede EVERY
// group in DOM order, and it must stay a direct child of #today-col (a wrapper
// would break the shelf's side-by-side widening).
test('the organizer precedes every section group and stays unwrapped', async ({ page }) => {
  await openToday(page);
  const order = await page.evaluate(() => {
    const kids = [...document.getElementById('today-col').children];
    return {
      planIdx: kids.findIndex(el => el.id === 'today-plan-wrap'),
      firstGroupIdx: kids.findIndex(el => el.matches('section[data-today-group]')),
      overdueIdx: kids.findIndex(el => el.id === 'today-overdue'),
      reviewIdx: kids.findIndex(el => el.id === 'today-review-zone'),
      needsIdx: kids.findIndex(el => el.id === 'today-input-zone'),
      hlIdx: kids.findIndex(el => el.id === 'today-highlights'),
      groups: kids.filter(el => el.matches('section[data-today-group]')).map(el => el.dataset.todayGroup),
    };
  });
  expect(order.planIdx, '#today-plan-wrap must be a DIRECT child of #today-col').toBeGreaterThan(-1);
  expect(order.firstGroupIdx, 'the tab must have section groups').toBeGreaterThan(-1);
  expect(order.planIdx, 'the day plan comes before the first group').toBeLessThan(order.firstGroupIdx);
  expect(order.hlIdx, 'the highlight strip sits above the organizer').toBeLessThan(order.overdueIdx);
  // Interrupt queues ride WITH the organizer — never inside a collapsible group.
  expect(order.reviewIdx).toBeLessThan(order.firstGroupIdx);
  // 💬 Esperan respuesta — what the retired ⚠️ Te necesita zone became (journey
  // fase 1, step 5). It is still an interrupt queue, so it still rides with the
  // organizer rather than inside a collapsible group.
  expect(order.needsIdx).toBeLessThan(order.firstGroupIdx);
  expect(order.groups).toEqual(['growth', 'reference']);
});

// --- 2. toggling collapses, flips ARIA, and survives a reload ---------------
test('a group toggle collapses the body, flips aria-expanded, and persists', async ({ page }) => {
  await openToday(page);
  // Both surviving groups start FOLDED, so the toggle is exercised in both
  // directions here: open it first, then close it and assert the persistence.
  expect(await collapsed(page, 'growth'), 'Growth is folded by default').toBe(true);

  await page.click('[data-testid="today-sec-growth-toggle"]');
  expect(await collapsed(page, 'growth'), 'the first click expands it').toBe(false);
  await expect(page.locator('#today-sec-growth-hdr')).toHaveAttribute('aria-expanded', 'true');
  expect(await page.evaluate(() => localStorage.getItem('hermes_today_group_growth'))).toBe('0');

  await page.click('[data-testid="today-sec-growth-toggle"]');
  expect(await collapsed(page, 'growth')).toBe(true);
  await expect(page.locator('#today-sec-growth-hdr')).toHaveAttribute('aria-expanded', 'false');
  expect(await page.evaluate(() => localStorage.getItem('hermes_today_group_growth'))).toBe('1');

  // Survives a full reload — the state lives in localStorage, not in the DOM.
  await openToday(page);
  expect(await collapsed(page, 'growth'), 'still collapsed after a reload').toBe(true);
  await expect(page.locator('#today-sec-growth-hdr')).toHaveAttribute('aria-expanded', 'false');
});

// --- 3. THE regression: the poll must not reopen a collapsed group ----------
// The renderers write into the container divs on every 45s tick. Collapse is
// class-only precisely so those writes land in an INVISIBLE body — an
// innerHTML-based collapse would be undone by the very next render.
test('a forced re-render does not reopen a collapsed group', async ({ page }) => {
  await openToday(page);
  // Collapse it BY HAND (expand → collapse), so the assertion is about a user's
  // choice surviving the poll, not about a default that was never touched.
  await page.click('[data-testid="today-sec-growth-toggle"]');
  expect(await collapsed(page, 'growth')).toBe(false);
  await page.click('[data-testid="today-sec-growth-toggle"]');
  expect(await collapsed(page, 'growth')).toBe(true);

  await page.evaluate(() => loadToday());
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(600);

  expect(await collapsed(page, 'growth'), 'the poll must never reopen a collapsed group').toBe(true);
  await expect(page.locator('#today-sec-growth-hdr')).toHaveAttribute('aria-expanded', 'false');
  // …and the card inside is still being rendered, just hidden (compressed, not dropped).
  expect(await page.evaluate(() => {
    const el = document.getElementById('today-pipeline-health');
    return !!el && el.innerHTML.trim().length > 0;
  })).toBe(true);
});

// --- 4. defaults + a collapsed group still says something ------------------
test('Growth and Reference both start collapsed, each with a summary', async ({ page }) => {
  await openToday(page);
  expect(await collapsed(page, 'growth')).toBe(true);
  expect(await collapsed(page, 'reference')).toBe(true);
  // Compressed, not hidden: a folded header still carries a one-line signal.
  for (const g of ['growth', 'reference']) {
    const txt = await page.locator(`[data-testid="today-sec-${g}-summary"]`).textContent();
    expect((txt || '').trim().length, `${g} must show a summary strip when folded`).toBeGreaterThan(0);
  }
  expect((await page.locator('[data-testid="today-sec-reference-summary"]').textContent()))
    .toContain('ICP');
});

// --- 5. a highlight chip deep-links into its group -------------------------
// Chips only render when the signal is non-green, so drive the renderer with a
// known payload instead of hoping the test DB has an alert.
test('a Growth highlight chip expands the group and moves focus off the chip', async ({ page }) => {
  await openToday(page);
  await page.evaluate(() => {
    TODAY_PIPELINE = { levels: { red: { count: 2, label: 'Overdue touch', deals: [] }, yellow: { count: 1, label: 'Aging', deals: [] } } };
    renderTodayHighlights();
  });
  const chip = page.locator('[data-testid="today-hl-pipeline"]');
  await expect(chip).toBeVisible();
  await expect(chip).toHaveAttribute('aria-label', /Growth/);
  expect(await collapsed(page, 'growth'), 'Growth starts folded').toBe(true);

  await chip.click();
  expect(await collapsed(page, 'growth'), 'the chip expands its group').toBe(false);
  // Focus must LAND somewhere inside the destination, not merely leave the chip:
  // asserting `!== 'today-hl-pipeline'` also passes when focus went nowhere at
  // all (getAttribute returns null on <body>), which tests nothing.
  const landed = await page.evaluate(() => {
    const a = document.activeElement;
    const grp = document.querySelector('section[data-today-group="growth"]');
    return {
      tag: a ? a.tagName : null,
      testid: a ? a.getAttribute('data-testid') : null,
      inGroup: !!(a && grp && grp.contains(a)),
    };
  });
  expect(landed.testid, 'focus leaves the chip — it can vanish on the next render')
    .not.toBe('today-hl-pipeline');
  expect(landed.tag, 'focus must not fall back to <body>').not.toBe('BODY');
  expect(landed.inGroup, 'focus lands inside the Growth section it jumped to').toBe(true);
});

// --- 5b. THE deep-link landing: clear of the sticky chrome ------------------
// Shipped broken once: scroll-margin-top was 5.5rem/88px but the sticky chrome
// is TWO stacked elements (<header> then #tabs-sticky) whose combined bottom is
// ~141px, so every chip parked its target UNDER the tab bar and hid the very
// alert row the chip advertised. Nothing asserted the landing, so it went green.
test('a chip deep link lands its target clear of the sticky chrome', async ({ page }) => {
  await openToday(page);
  await page.evaluate(() => {
    TODAY_PIPELINE = { levels: { red: { count: 2, label: 'Overdue touch', deals: [] }, yellow: { count: 3, label: 'Aging', deals: [] } } };
    renderTodayHighlights();
    // The landing is only measurable on a page long enough to scroll freely: at
    // the natural height the browser clamps at maxScroll and the target stops
    // wherever the document ends, which reads as a verdict about scroll-margin
    // that it is not. Give the document room, then assert we are not clamped.
    const pad = document.createElement('div');
    pad.id = 'pw-scroll-pad';
    pad.style.height = '3000px';
    document.getElementById('today-col').appendChild(pad);
  });
  await page.click('[data-testid="today-hl-pipeline"]');
  await page.waitForTimeout(1500);   // smooth scroll must settle before measuring

  const m = await page.evaluate(() => {
    const stickyBottom = ['header', '#tabs-sticky']
      .map(s => document.querySelector(s))
      .filter(el => el && getComputedStyle(el).position === 'sticky')
      .reduce((b, el) => Math.max(b, el.getBoundingClientRect().bottom), 0);
    const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
    return {
      stickyBottom,
      top: document.getElementById('today-pipeline-health').getBoundingClientRect().top,
      clamped: Math.abs(window.scrollY - maxScroll) < 2,
    };
  });
  expect(m.clamped, 'page bottomed out — the landing is unmeasurable, not passing').toBe(false);
  expect(m.stickyBottom, 'both the header and the tab bar are sticky').toBeGreaterThan(100);
  expect(m.top, 'the deep-link target must land BELOW the sticky chrome, never under it')
    .toBeGreaterThanOrEqual(m.stickyBottom);
});

// --- 5c. the scorecard chip must not cry wolf on an empty week -------------
// `no data yet` is not `below target`: renderScorecardStrip paints an empty
// state when every KPI value is falsy, so a chip built from those zeros deep-
// linked into `No activity this week` — and on a fresh Monday it was the ONLY
// chip on the strip, i.e. the headline signal was a non-actionable alarm.
test('the scorecard chip stays silent when the week has no activity', async ({ page }) => {
  await openToday(page);
  const chipFor = (kpis) => page.evaluate((kpis) => {
    TODAY_SCORECARD = { kpis };
    renderTodayHighlights();
    const el = document.querySelector('[data-testid="today-hl-scorecard"]');
    return el ? el.textContent.trim() : null;
  }, kpis);

  const zeros = [1, 2, 3, 4, 5].map(i => ({ label: 'k' + i, value: 0, target: 3 }));
  expect(await chipFor(zeros), 'all-zero KPIs are no data, not five misses').toBe(null);

  // …but a week WITH activity and real misses still raises the chip, counting
  // the misses (same direction as every other chip on the strip), not the hits.
  const some = [{ label: 'k1', value: 4, target: 3 }, { label: 'k2', value: 1, target: 3 },
                { label: 'k3', value: 0, target: 3 }];
  expect(await chipFor(some)).toContain('2 off target');
});

// --- 6. digits toggle groups; the plan grammar is untouched ----------------
// Re-pinned with the Rhythm deletion: the digits renumbered 2/3 → 1/2 rather
// than leaving a hole where Rhythm's `1` used to be.
test('digit 1 toggles Growth and the j/k plan grammar still works', async ({ page }) => {
  await openToday(page);
  expect(await collapsed(page, 'growth')).toBe(true);
  await page.keyboard.press('1');
  expect(await collapsed(page, 'growth'), 'digit 1 unfolds Growth').toBe(false);
  await page.keyboard.press('1');
  expect(await collapsed(page, 'growth'), 'digit 1 folds it back').toBe(true);
  // …and digit 2 now belongs to Reference (nothing answers 3 any more).
  await page.keyboard.press('2');
  expect(await collapsed(page, 'reference'), 'digit 2 unfolds Reference').toBe(false);
  await page.keyboard.press('2');
  expect(await collapsed(page, 'reference'), 'digit 2 folds it back').toBe(true);

  // The letters are untouched: ? still opens the keymap, Escape still closes it.
  await page.keyboard.press('?');
  await expect(page.locator('[data-testid="today-keymap"]')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.locator('[data-testid="today-keymap"]')).toBeHidden();
  // j/k are still handled by the plan. `typeof _todayFocus === 'object'` is true
  // whether or not the keys did anything (it is true before any keypress), so
  // assert the focus actually MOVED and that the DOM ring followed it.
  const focus = () => page.evaluate(() => {
    const marked = document.querySelector('#today-do-list .today-kfocus');
    return {
      id: _todayFocus.id,
      marked: marked ? marked.getAttribute('data-task-id') : null,
      n: document.querySelectorAll('#today-do-list [data-task-id]').length,
    };
  });

  await page.keyboard.press('j');
  const a = await focus();
  expect(a.n, 'the Do zone needs cards for the j/k grammar to be observable').toBeGreaterThan(0);
  expect(a.id, 'j must select a card — a hijacked digit handler would leave it null').not.toBe(null);
  expect(a.marked, 'the keyboard ring follows _todayFocus').toBe(a.id);

  if (a.n >= 2) {
    await page.keyboard.press('j');
    const b = await focus();
    expect(b.id, 'a second j moves to the next card').not.toBe(a.id);
    expect(b.marked).toBe(b.id);
    await page.keyboard.press('k');
    expect((await focus()).id, 'k walks back').toBe(a.id);
  }
});
