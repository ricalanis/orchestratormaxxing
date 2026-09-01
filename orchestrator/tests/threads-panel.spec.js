// e2e for the Telegram surface — consolidation spec §4 ("Telegram surface":
// exactly two additions, then stop) and §4 ("Delete — relocate first").
//
// The threads panel is not a list of chats. It is the ROUTING TABLE
// `dispatch._resolve_thread` reads: binding a project here is what makes the
// dispatch toast "Sent to 🧑‍💻 Code" true instead of a silent fall-back to Hoy.
// That is why the load-bearing assertion is #2 — an edit must leave the browser
// as a PATCH and come back as a re-render from the SERVER's row. A panel that
// paints the optimistic local value is the exact class of quiet lie this phase
// exists to remove.
//
// Four contracts:
//   1. every active thread is listed (no truncation) and "Hoy" — the sole
//      destination of all three daily briefs — is among them;
//   2. changing a role fires PATCH /api/threads/{id} with just that field, and
//      the row re-renders from the response;
//   3. archived topics are COLLAPSED by default behind "N archivados" — the
//      13 dead topics are history, and history never outranks a live thread in
//      a list a human scans top-down (spec §2);
//   4. /planning and /orchestration are GONE. RED-PROOF: both answer 200 at
//      HEAD (they are live FastAPI routes rendering planning.html /
//      orchestration.html), so this assertion has been watched failing against
//      the state it claims to catch. Their unique capabilities were relocated
//      FIRST — "+ Project" into Work, the pending-input queue into Today —
//      which is red line 5.
//
// Runs against the wiped-cycle DB COPY from playwright.config.js, so the role
// PATCH never touches ~/.hermes/kanban.db. It is still reverted at the end: the
// suite shares one server, and a leaked edit would make a later run's "before"
// depend on this run's "after".
//
// Run: `npx playwright test threads-panel`.
const { test, expect } = require('@playwright/test');

// The hand-seeded registry (spec §2): 8 live topics given human names, plus the
// new "Hoy" brief thread = 9. It is an invariant, not a snapshot — a thread is
// never auto-created for a project, so this count cannot drift with the backlog.
const ACTIVE_THREADS = 9;

const openAgents = async (page) => {
  await page.goto('/?tab=sessions');
  await page.waitForLoadState('networkidle');
  // loadThreads() is fired by switchTab and resolves independently of the
  // (slow, multi-host) session scan — wait on the panel's own state.
  await expect.poll(() => page.evaluate(() =>
    (typeof THREADS_DATA !== 'undefined' && THREADS_DATA) ? 1 : 0), { timeout: 15000 }).toBe(1);
  await expect(page.locator('[data-testid="threads-panel"]')).toBeVisible();
};

// --- 1. every active thread is listed, and Hoy is one of them ---------------
test('the threads panel lists every active thread, including Hoy', async ({ page, request }) => {
  await openAgents(page);

  const data = await request.get('/api/threads').then(r => r.json());
  expect(data.active, 'the hand-seeded registry is 8 live topics + Hoy').toBe(ACTIVE_THREADS);

  // The panel shows ALL of them — a registry that truncates is a routing table
  // with rows you cannot fix.
  const rows = page.locator('#threads-active [data-testid="thread-row"]');
  await expect(rows).toHaveCount(data.active);

  // Names live in the inline-edit inputs, so read VALUES, not text content —
  // asserting textContent here would pass vacuously against an empty table.
  const names = await page.locator('#threads-active [data-testid="thread-name"]')
    .evaluateAll(els => els.map(e => e.value));
  expect(names).toHaveLength(data.active);
  expect(names, 'Hoy is the sole destination of the three daily briefs').toContain('Hoy');
  expect(names).toContain('🧑‍💻 Code');
  await expect(page.locator('#threads-count')).toContainText(`${data.active} activos`);

  // Every row carries the four editable/derived columns.
  const first = rows.first();
  for (const testid of ['thread-name', 'thread-role', 'thread-project', 'thread-status']) {
    await expect(first.locator(`[data-testid="${testid}"]`)).toHaveCount(1);
  }
  // The bound thread shows its PROJECT selected, by name, not as a raw id — the
  // binding is the whole point of the table, so it must be legible at a glance.
  const boundRow = data.threads.find(t => !t.archived && t.project_id);
  expect(boundRow, 'the seed binds 🧑‍💻 Code to the orchestrator project').toBeTruthy();
  const bound = page.locator(`#threads-active [data-thread-id="${boundRow.thread_id}"] [data-testid="thread-project"]`);
  await expect(bound).toHaveValue(boundRow.project_id);
  await expect(bound.locator('option:checked')).toHaveText(boundRow.project_name);
  // …and an unbound thread shows the em-dash, not a silently pre-selected project.
  const freeRow = data.threads.find(t => !t.archived && !t.project_id);
  await expect(page.locator(`#threads-active [data-thread-id="${freeRow.thread_id}"] [data-testid="thread-project"]`))
    .toHaveValue('');
});

// --- 2. a role edit is a PATCH, and the row re-renders from the response -----
test('changing a role PATCHes /api/threads/{id} and re-renders from the server row', async ({ page, request }) => {
  await openAgents(page);

  // Pick a thread whose role we can flip to something else in the 5-value enum.
  const target = await page.evaluate(() => {
    const t = THREADS_DATA.threads.find(x => !x.archived && x.role !== 'ops');
    return t ? { id: String(t.thread_id), role: t.role } : null;
  });
  expect(target, 'need one active non-ops thread to flip').not.toBeNull();

  const row = page.locator(`#threads-active [data-thread-id="${target.id}"]`);
  const select = row.locator('[data-testid="thread-role"]');
  await expect(select).toHaveValue(target.role);

  const [req] = await Promise.all([
    page.waitForRequest(r => r.method() === 'PATCH' && r.url().includes(`/api/threads/${target.id}`)),
    select.selectOption('ops'),
  ]);
  // Exactly the edited field — PATCH semantics, not a full-row overwrite that
  // would clobber a binding the operator never touched.
  expect(JSON.parse(req.postData())).toEqual({ role: 'ops' });

  // The re-render is from the SERVER's row, so the panel and the DB agree.
  await expect(page.locator(`#threads-active [data-thread-id="${target.id}"] [data-testid="thread-role"]`))
    .toHaveValue('ops');
  const after = await request.get('/api/threads').then(r => r.json());
  expect(after.threads.find(t => String(t.thread_id) === target.id).role).toBe('ops');

  // Hygiene: put it back through the same API the UI uses.
  await request.patch(`/api/threads/${target.id}`, { data: { role: target.role } });
});

// --- 3. archived topics are collapsed by default -----------------------------
test('archived threads are collapsed behind an "N archivados" toggle', async ({ page, request }) => {
  await openAgents(page);
  const data = await request.get('/api/threads').then(r => r.json());
  expect(data.archived, 'the seed archives the 13 unnamed topics').toBeGreaterThan(0);

  const toggle = page.locator('[data-testid="threads-archived-toggle"]');
  await expect(toggle).toBeVisible();
  await expect(toggle).toContainText(`${data.archived} archivados`);

  // Collapsed on arrival — history must not outrank the live rows.
  await expect(page.locator('#threads-archived')).toBeHidden();
  // …and they are not smuggled into the active table either.
  await expect(page.locator('#threads-active [data-testid="thread-row"]')).toHaveCount(data.active);

  await toggle.click();
  await expect(page.locator('#threads-archived')).toBeVisible();
  await expect(page.locator('#threads-archived [data-testid="thread-row"]')).toHaveCount(data.archived);

  await toggle.click();
  await expect(page.locator('#threads-archived')).toBeHidden();
});

// --- 4. the relocated-then-deleted pages are gone (RED-PROOF) ----------------
// Both routes answer 200 at HEAD. planning.html's "+ Project" (the only
// project-creation UI in the product) now lives in Work; orchestration.html's
// pending-input queue (the only attention queue) now lives in Today's
// "💬 Esperan respuesta" block — it survived the ⚠️ Te necesita zone's own
// deletion for exactly this reason. Relocate first, then delete — red line 5.
test('/planning and /orchestration are deleted', async ({ request }) => {
  for (const path of ['/planning', '/orchestration']) {
    const res = await request.get(path);
    expect(res.status(), `${path} must be gone, not merely empty`).toBe(404);
  }
  // The API verbs those pages consumed were never the page and stay live —
  // deleting a surface must not delete a capability.
  expect((await request.get('/api/projects')).status()).toBe(200);
  // The pending-input queue itself — the one capability /orchestration owned.
  const ev = await request.get('/api/session-events');
  expect(ev.status()).toBe(200);
  expect(await ev.json()).toHaveProperty('pending_input');
});
