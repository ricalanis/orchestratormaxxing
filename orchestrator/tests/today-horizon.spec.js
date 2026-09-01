// e2e contract for LA LÍNEA DE HORIZONTE and for the deletion it paid for
// (Visión V1 — "la disrupción central: Hoy se convierte en UN stream").
//
// The Visión is disruptive by SUBTRACTION: the three remaining Today blocks
// (💰 Dinero · 🤖 Agentes · 🧭 Último brief) are deleted and their job collapses
// into ONE glanceable line of six numbers at the top of the column. Ley 2 — he
// acts on what is IN the stream and ignores what is BESIDE it: those blocks
// showed the same contents for twelve consecutive briefs and produced zero
// actions, so the answer is a line plus the stream, not three more zones.
//
// Four contracts:
//   1. the line renders SIX segments, in the composer's order, ABOVE the plan
//      pane — and a zero segment is DIMMED, never hidden (the fixed structure is
//      the accommodation: a line whose shape changes every morning has to be
//      re-read, which is the cost it exists to remove);
//   2. the three blocks are GONE from the page — markup, renderers AND globals.
//      RED-PROOF: every selector and every global asserted absent here EXISTS at
//      HEAD, so the test was watched red against the pre-deletion template
//      before being kept (CLAUDE.md Tier-1c: a contract that never failed
//      against the thing it claims to catch is unfalsified);
//   3. what SURVIVED is intact — the Do/Shelf plan pane (spec §4, "the best
//      thing in the codebase") and 💬 Esperan respuesta, which lives on by red
//      line 5 (relocate first, then delete) and is the one capability of the
//      retired ⚠️ zone that had nowhere else to go;
//   4. a segment is a deep link, not a label: clicking it routes to the surface
//      that can act on the number.
//
// Runs against the wiped-cycle DB copy from playwright.config.js.
// Run: `npx playwright test today-horizon`.
const { test, expect } = require('@playwright/test');

// The composer's fixed order (dashboard/pulse.py HORIZON_ORDER). Asserted, not
// assumed: the order IS the line, and the client renders whatever the server
// sends — so a reorder on either side has to be a deliberate edit in both.
const HORIZON_ORDER = [
  'clientes_activos', 'oportunidades_trabadas', 'proyectos_vivos',
  'delegando', 'entregables', 'hoy_pendientes', 'cobro',
];

// switchTab fires loadToday() asynchronously; settle it before asserting, or the
// test measures the race instead of the product. The horizon lands on a second,
// independent fetch — wait for that too.
const openToday = async (page) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await page.evaluate(() => switchTab('today'));
  await expect.poll(() => page.evaluate(() => (typeof TODAY_DATA !== 'undefined' && TODAY_DATA) ? 1 : 0),
    { timeout: 15000 }).toBe(1);
  await expect.poll(() => page.evaluate(() => (typeof TODAY_HORIZON !== 'undefined' && TODAY_HORIZON) ? 1 : 0),
    { timeout: 15000 }).toBe(1);
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(300);
};

// --- 1. the line: six segments, in order, above the plan --------------------
test('the horizon line renders six segments in order, above the plan pane', async ({ page }) => {
  await openToday(page);

  const segs = page.locator('[data-testid="today-horizon-seg"]');
  await expect(segs, 'seven questions → seven segments, always').toHaveCount(7);
  expect(await segs.evaluateAll(els => els.map(e => e.dataset.key))).toEqual(HORIZON_ORDER);

  // Above the plan. Both are direct children of #today-col, so "above" is a
  // fact about the DOM, not about pixels.
  const idx = await page.evaluate(() => {
    const kids = [...document.getElementById('today-col').children];
    return {
      horizon: kids.findIndex(el => el.id === 'today-horizon'),
      plan: kids.findIndex(el => el.id === 'today-plan-wrap'),
    };
  });
  expect(idx.horizon, '#today-horizon must be a DIRECT child of #today-col').toBeGreaterThan(-1);
  expect(idx.plan).toBeGreaterThan(-1);
  expect(idx.horizon, 'the horizon line is the first thing on the tab').toBeLessThan(idx.plan);

  // One line: every segment carries a number and its Spanish label, and no
  // segment is a card (the whole point — six numbers, zero cards).
  const line = (await page.locator('#today-horizon').innerText()).replace(/\s+/g, ' ').trim();
  expect(line).toMatch(/\d+ clientes/);
  for (const word of ['trabadas', 'proyectos', 'delegando', 'entregables', 'hoy']) {
    expect(line, `the line must name ${word}`).toContain(word);
  }
  expect(await page.locator('#today-horizon h3').count(), 'a line has no headings').toBe(0);
});

// --- 1b. a zero is DIMMED, never hidden -------------------------------------
// The fixed structure IS the ADHD accommodation. Driven through the real
// renderer with an all-zero payload, so this asserts the product's own zero
// state rather than a hand-made fixture of it.
test('zero-count segments render dimmed and stay on the line', async ({ page }) => {
  await openToday(page);
  await page.evaluate((order) => {
    TODAY_HORIZON = {
      status: 'ok', date: '2026-08-02', order,
      ...Object.fromEntries(order.map(k => [k, {
        key: k, count: 0, label: k, hint: 'x', target: '?tab=crm',
      }])),
    };
    renderTodayHorizon();
  }, HORIZON_ORDER);

  const segs = page.locator('[data-testid="today-horizon-seg"]');
  await expect(segs, 'all seven survive a zero day').toHaveCount(7);
  for (let i = 0; i < 7; i++) {
    await expect(segs.nth(i)).toBeVisible();
    await expect(segs.nth(i)).toHaveAttribute('data-zero', '1');
  }

  // …and a FAILED read is not a zero: an unmeasured horizon shows an em-dash
  // where the number goes, so "I could not read it" cannot be mistaken for 0.
  await page.evaluate(() => { TODAY_HORIZON = null; renderTodayHorizon(); });
  await expect(segs).toHaveCount(7);
  const dashes = (await page.locator('#today-horizon').innerText()).match(/—/g) || [];
  expect(dashes.length, 'an unreadable horizon is seven em-dashes, never seven zeros').toBe(7);
});

// --- 2. the three blocks are GONE (RED-PROOF) -------------------------------
// Every selector and every global below MATCHES at HEAD. Watched red against the
// pre-deletion template, then kept.
test('the Dinero, Agentes and Último-brief blocks are deleted, not hidden', async ({ page }) => {
  await openToday(page);

  for (const sel of ['#today-money-zone', '#today-money', '#today-agents-zone',
                     '#today-agents', '#today-brief-zone', '#today-brief-md',
                     '#today-brief-slots', '#today-done-wall']) {
    expect(await page.locator(sel).count(), `${sel} must be deleted, not hidden`).toBe(0);
  }

  // No orphaned heading text survives the deletion (the headings were the
  // contract with the Telegram message; the message keeps them, the web does not).
  const col = await page.locator('#today-col').innerText();
  for (const heading of ['💰 Dinero', '🤖 Agentes', 'Último brief']) {
    expect(col, `"${heading}" must not survive on the page`).not.toContain(heading);
  }

  // The renderers and their state are gone too — a dead function nobody calls is
  // the next surface someone re-mounts.
  const gone = await page.evaluate(() => ({
    load: typeof loadTodayBrief,
    render: typeof renderTodayBrief,
    money: typeof renderTodayMoney,
    agents: typeof renderTodayAgents,
    mirror: typeof renderTodayBriefMirror,
    payload: typeof briefPayload,
    brief: typeof TODAY_BRIEF,
    done: typeof TODAY_DONE_TODAY,
  }));
  expect(gone).toEqual({
    load: 'undefined', render: 'undefined', money: 'undefined', agents: 'undefined',
    mirror: 'undefined', payload: 'undefined', brief: 'undefined', done: 'undefined',
  });

  // The web MIRROR died; the brief itself did not. GET /api/brief/latest stays an
  // API (Telegram is the channel now) — deleting the endpoint would have deleted
  // the ritual, which is not what the evidence said.
  const seen = [];
  page.on('request', r => seen.push(r.url()));
  await page.evaluate(() => loadToday());
  await page.waitForTimeout(600);
  expect(seen.some(u => u.includes('/api/brief/latest')),
    'Today must no longer fetch the brief mirror').toBe(false);
  expect(seen.some(u => u.includes('/api/journey/horizon')),
    'the horizon refreshes on the existing Hoy poll').toBe(true);
});

// --- 3. what survived is intact ---------------------------------------------
test('the plan pane and 💬 Esperan respuesta survive the deletion', async ({ page }) => {
  await openToday(page);

  // The Do/Shelf plan pane — untouched by this wave.
  await expect(page.locator('#today-plan-wrap #today-plan-pane')).toBeVisible();
  await expect(page.locator('#today-plan-pane h3').first()).toHaveText('✅ Hoy');
  await expect(page.locator('[data-testid="today-plan-toggle"]')).toBeVisible();
  await page.evaluate(() => toggleTodayShelf(true));
  await expect(page.locator('[data-testid="today-shelf"]')).toBeVisible();
  await page.evaluate(() => toggleTodayShelf(false));

  // 💬 Esperan respuesta — relocated, not deleted (red line 5). Its markup and
  // its renderer are both still here; hidden at zero is the quiet-strip rule,
  // which is why the ELEMENT is asserted rather than its visibility.
  expect(await page.locator('#today-input-zone').count()).toBe(1);
  expect(await page.evaluate(() => typeof renderTodayInputQueue)).toBe('function');
  await page.evaluate(() => {
    TODAY_DATA.needs_you = { blocked: [], input_needed: [{
      id: 1, host: 'local', session_key: 'sess-horizon', session_display: 'demo',
      payload: JSON.stringify({ message: '¿sigo?' }),
    }] };
    renderTodayInputQueue();
  });
  await expect(page.locator('#today-input-zone')).toBeVisible();
  await expect(page.locator('#today-input-zone h3')).toHaveText('💬 Esperan respuesta');
  await expect(page.locator('[data-testid="today-needs-input"]')).toHaveCount(1);
  await page.evaluate(() => {
    TODAY_DATA.needs_you = { blocked: [], input_needed: [] };
    renderTodayInputQueue();
  });
  await expect(page.locator('#today-input-zone')).toBeHidden();
});

// --- 4. a segment is a deep link, not a label -------------------------------
test('clicking a segment routes to the surface that can act on the number', async ({ page }) => {
  await openToday(page);

  await page.locator('[data-testid="today-horizon-seg"][data-key="proyectos_vivos"]').click();
  await expect.poll(() => page.evaluate(() => currentTab), { timeout: 5000 }).toBe('projects');

  await page.evaluate(() => switchTab('today'));
  await page.locator('[data-testid="today-horizon-seg"][data-key="delegando"]').click();
  await expect.poll(() => page.evaluate(() => currentTab), { timeout: 5000 }).toBe('agent-tasks');

  await page.evaluate(() => switchTab('today'));
  await page.locator('[data-testid="today-horizon-seg"][data-key="clientes_activos"]').click();
  await expect.poll(() => page.evaluate(() => currentTab), { timeout: 5000 }).toBe('crm');

  // `hoy` is an ANCHOR, not a tab: the plan is already on this screen, so the
  // number scrolls to it instead of navigating away from it.
  await page.evaluate(() => switchTab('today'));
  await page.locator('[data-testid="today-horizon-seg"][data-key="hoy_pendientes"]').click();
  await page.waitForTimeout(400);
  expect(await page.evaluate(() => currentTab)).toBe('today');
  await expect(page.locator('#today-plan-wrap')).toBeInViewport();
});
