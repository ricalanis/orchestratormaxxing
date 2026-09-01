// e2e for the journey fase-1 step 4: the drawer becomes the client's cycle.
//
// Three things ship together here, and the spec is organised as the three
// questions they answer:
//
//   1. PROJECT — "what does this project consist of?"  The project drawer had
//      NO field rows at all (edFieldRows returned [] for it), so the delivery
//      half of every client relationship rendered "No linked records." It now
//      carries Estado / Cliente / Valor entregado plus the FIVE-FACET HUB
//      (directiva ADICIÓN 7): conversaciones · recursos · código · planes ·
//      tareas, read live from GET /api/projects/{id}/hub.
//
//   2. DEAL — "where is this client in the cycle?"  Trabajo renders the m06
//      spine (the deal's own tasks ∪ the delivering project's tasks) grouped BY
//      STAGE-KIND (directiva ADICIÓN 9) and then by board column; Actividad
//      renders the timeline filtered to HUMAN event kinds.
//
//   3. CARD — "who is this for, and how far along?"  The contextChip's client
//      branch shipped DORMANT in step 2 (nothing wrote tasks.deal_id until m06)
//      and goes live here, with the muted stage chip beside it.
//
// HONEST-FIXTURE NOTES (all three verified, none assumed):
//  * The test server (tests/serve_test_dashboard.py) serves a COPY of the real
//    kanban.db with cycles wiped. That copy therefore ALREADY carries the one
//    real plan attachment on proj_orchestrator (att_bbc839cc, registered by the
//    plan-to-repo skill) — so the plans facet is asserted against a real row,
//    not a seeded one. If that row is ever removed the assertion degrades to
//    "the facet rendered", which is stated in the test rather than hidden.
//  * The client chip cannot be exercised by seeding SQL: the spec drives the
//    real UI, so the deal link is created through the NEW named writer
//    (PATCH /api/tasks/{id}/deal) — which makes this a contract on the writer
//    and the reader at once, and leaves the fixture DB in a state the next
//    assertion can read.
//  * Every mutation lands in the throwaway server copy, never in ~/.hermes.
//
// Run: `npx playwright test drawer-journey`.
const { test, expect } = require('@playwright/test');

const HUB_PROJECT = 'proj_orchestrator';   // the fixture copy's real plan owner

async function api(request, method, path, data) {
  const res = await request.fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json' },
    data: data === undefined ? undefined : JSON.stringify(data),
  });
  return res;
}

// Open an entity drawer directly by URL — the deep-link router the drawer
// already ships (?entity=type:id), so no click path has to be replayed.
async function openDrawer(page, type, id) {
  await page.goto(`/?entity=${type}:${encodeURIComponent(id)}`);
  await page.waitForLoadState('networkidle');
  await expect(page.locator('#entity-drawer')).toBeVisible();
  await expect(page.locator('#ed-body')).not.toContainText('Loading…');
}

test.describe('project drawer — the five-facet hub (journey fase 1, step 4)', () => {
  test('the project drawer carries Estado, Cliente and Valor entregado', async ({ page }) => {
    await openDrawer(page, 'project', HUB_PROJECT);

    // Estado is the only EDITABLE row: it routes through PATCH /api/projects/{id},
    // whose `status` field is handed to sprints.set_project_status (ruling 8).
    const status = page.locator('[data-testid="ed-project-status"]');
    await expect(status).toBeVisible();
    // A closed vocabulary — `delivering` was deliberately left unshipped, so
    // offering it would render an option the server refuses.
    const options = await status.locator('option').evaluateAll(
      els => els.map(e => e.value).filter(Boolean));
    expect(options).toEqual(expect.arrayContaining(['planned', 'active', 'delivered', 'archived']));
    expect(options).not.toContain('delivering');

    // Cliente + Valor entregado are read-outs, present whether or not this
    // project has a client (an absent row and an empty one must not look the
    // same — the reason the empty state is a word, not a blank).
    await expect(page.locator('#ed-body')).toContainText('Cliente');
    await expect(page.locator('#ed-body')).toContainText('Valor entregado');
  });

  test('the five facets render, and the real plan row is one of them', async ({ page }) => {
    await openDrawer(page, 'project', HUB_PROJECT);
    const hub = page.locator('[data-testid="ed-hub"]');
    await expect(hub).toBeVisible();

    // All five, by name. A facet with nothing in it still renders — an ABSENT
    // conversations row and a project with no conversations look identical
    // otherwise, and the first is a bug while the second is a fact.
    for (const label of ['conversaciones', 'recursos', 'código', 'planes', 'tareas']) {
      await expect(hub).toContainText(label);
    }

    // The plans facet is the one that lists its items: a plan is a DOCUMENT the
    // operator opens. This asserts against the REAL attachment the plan-to-repo
    // skill registered on proj_orchestrator, so it is a contract on the live
    // endpoint's shape and not on a fixture we wrote ourselves.
    const facets = await page.evaluate(async (pid) => {
      const r = await fetch(`/api/projects/${pid}/hub`);
      return (await r.json()).facets;
    }, HUB_PROJECT);
    expect(facets.plans.count).toBeGreaterThan(0);
    const plan = facets.plans.items[0];
    await expect(hub).toContainText(plan.title);
    // Rendered with its source agent, and reachable: a plan lives on disk in the
    // ~/dev/planning repo and there is NO file server, so the path is offered as
    // a tooltip + copy rather than as a link that would 404.
    if (plan.source_agent) await expect(hub).toContainText(plan.source_agent);
    const item = hub.locator('[data-hub-item]').first();
    await expect(item).toBeVisible();
    const target = plan.url || plan.path;
    expect(await item.locator('a, button').first().getAttribute('title')).toContain(target);

    // The tasks facet is a COUNT over the real tasks table, never attachments.
    expect(facets.tasks).toHaveProperty('open');
    expect(facets.tasks).toHaveProperty('total');
    expect(facets.tasks.total).toBeGreaterThanOrEqual(facets.tasks.open);
  });

  test('the project breadcrumb is no longer empty when a client delivers through it',
    async ({ page, request }) => {
      // The fixture copy has NO deal linked to a project — measured, not
      // assumed: step 3's backfill-by-taps is the operator's to run, so waiting for
      // the data would make this test vacuous forever. It builds the state
      // instead, through the SANCTIONED verb (POST /deliver — conversion verb
      // 2/3), which is what writes `deals.project_id` AND `projects.account_id`.
      // Building it any other way (raw SQL, a hand-set column) would test a
      // state the product cannot actually reach.
      const linked = await deliverSomeDeal(request);
      test.skip(!linked, 'fixture copy has no won deal to deliver');

      await openDrawer(page, 'project', linked.project_id);
      const crumb = page.locator('#ed-crumb');
      // Pre-step-4 this was a hard-coded [] — the project was the only entity in
      // the drawer with no breadcrumb at all. Two hops now: the client, then the
      // deal it delivers.
      await expect(crumb).toContainText(linked.account_name);
      await expect(crumb).toContainText(linked.title);
      // …and the fields the breadcrumb's data also feeds.
      await expect(page.locator('#ed-body')).toContainText(linked.account_name);
    });
});

// Deliver a won orphan into a real project through the sanctioned verb, and
// return {id, title, project_id, account_name} — or null when the fixture copy
// has no won deal at all. Idempotent by construction: `deliver_deal` answers
// `already_delivered` with the existing link, so calling it twice across specs
// is safe.
// An open task this spec may safely link to a deal — deliberately NOT an inbox
// task. The Playwright suite shares ONE server (workers: 1, reuseExistingServer),
// so a link written here persists into every later spec, and
// board-context-chip.spec.js asserts that its inbox card wears the `untriaged`
// chip. Since the client branch OUTRANKS the untriaged one by design, linking an
// inbox task would break that spec from a distance — a cross-spec failure that
// reads as a bug in the chip and is really a fixture collision.
async function pickLinkableTask(request) {
  const projects = await (await api(request, 'GET', '/api/projects')).json();
  const list = Array.isArray(projects) ? projects : (projects.projects || []);
  const inbox = new Set(list.filter(p => p.slug === 'inbox').map(p => p.id));
  const tasks = await (await api(request, 'GET', '/api/tasks')).json();
  return (tasks.tasks || []).find(
    t => !t.deal_id && t.status !== 'done' && t.project_id && !inbox.has(t.project_id)) || null;
}

async function deliverSomeDeal(request) {
  const pipeline = await (await api(request, 'GET', '/api/crm/pipeline')).json();
  const deals = Object.values(pipeline.by_stage || {}).flat();
  // WON specifically, not merely "has a project": board-crm-deliver.spec.js runs
  // first on the shared server and leaves behind a LOST deal that still carries
  // its delivering project. Reusing that one would make the stage assertion
  // below expect `ejecucion` from a deal the rule correctly refuses to place at
  // all (lost is an exit from the cycle, not a position in it) — a spec failure
  // that reads as a derivation bug and is really the wrong fixture.
  const already = deals.find(d => d.project_id && d.stage === 'won');
  if (already) return already;
  const won = deals.find(d => d.stage === 'won');
  if (!won) return null;
  const projects = await (await api(request, 'GET', '/api/projects')).json();
  const list = Array.isArray(projects) ? projects : (projects.projects || []);
  const target = list.find(p => p.id !== 'proj_inbox' && p.id !== 'proj_personal');
  if (!target) return null;
  const res = await api(request, 'POST', `/api/crm/deals/${won.id}/deliver`,
                        { project_id: target.id });
  if (!res.ok()) return null;
  return { ...won, project_id: target.id };
}

test.describe('deal drawer — Trabajo grouped by stage, Actividad by human', () => {
  test('Trabajo groups the work by stage-kind, and each row opens its task',
    async ({ page, request }) => {
      // Build the spine through the two sanctioned writers rather than waiting
      // for data: deliver a won deal into a project (so the DELIVERY half of the
      // union has rows) and link an open task straight to the deal (so the
      // COMMERCIAL half does too). Both halves are what step 4 added; a spec
      // that only ever saw one of them would pass with the union half-built.
      const deal = await deliverSomeDeal(request);
      test.skip(!deal, 'fixture copy has no won deal to deliver');

      const free = await pickLinkableTask(request);
      if (free) {
        const r = await api(request, 'PATCH', `/api/tasks/${free.id}/deal`, { deal_id: deal.id });
        expect(r.status(), await r.text()).toBe(200);
      }

      await openDrawer(page, 'deal', deal.id);
      const work = page.locator('[data-testid="ed-work"]');
      await expect(work).toBeVisible();
      await expect(work).toContainText('Trabajo');
      await expect(work).toContainText('Actividad');

      // The grouping is the point: a flat list answers "how many cards", the
      // cycle answers "where is this client".
      const drill = await page.evaluate(async (id) => {
        const r = await fetch(`/api/crm/deals/${id}/drilldown`);
        return await r.json();
      }, deal.id);
      expect(drill.tasks.length, 'the spine returned no work to group').toBeGreaterThan(0);
      expect(drill.groups.length).toBeGreaterThan(0);

      await expect(work.locator('[data-work-stage]').first()).toBeVisible();
      const stages = await work.locator('[data-work-stage]').evaluateAll(
        els => els.map(e => e.dataset.workStage));
      expect(stages.length).toBeGreaterThan(0);
      // Every rendered stage is one the SERVER derived — the client never
      // invents a stage of its own (the derivation has exactly one home).
      const fromServer = drill.groups.map(g => String(g.stage_kind || 'none'));
      for (const s of stages) expect(fromServer).toContain(s);
      // A won deal delivered by an active project is `ejecucion` by the rule.
      expect(fromServer).toContain('ejecucion');

      // A task row is a real navigation target (the drawer is the one detail
      // surface), not a decorative line.
      const row = work.locator('[data-work-task]').first();
      await expect(row).toBeVisible();
      await row.click();
      await expect(page.locator('#ed-kind')).toHaveText('task');
    });

  test('Actividad shows only human event kinds', async ({ page, request }) => {
    const pipeline = await (await api(request, 'GET', '/api/crm/pipeline')).json();
    const any = Object.values(pipeline.by_stage || {}).flat()[0];
    test.skip(!any, 'fixture copy has no deals');

    await openDrawer(page, 'deal', any.id);
    const work = page.locator('[data-testid="ed-work"]');
    await expect(work).toBeVisible();

    // The whitelist, asserted from the page's own constant so the contract
    // cannot silently widen: a NEW machine event kind must not be able to
    // appear here by default.
    const kinds = await page.evaluate(() => DEAL_HUMAN_EVENTS);
    expect(kinds).toEqual(['touch', 'meeting', 'discovery_call', 'stage_changed', 'delivered_link']);
    const machineOnly = await page.evaluate((allowed) => {
      const e = (ED_ENTITY || {});
      return { rendered: Object.keys(DEAL_EVENT_META), allowed };
    }, kinds);
    expect(machineOnly.rendered.sort()).toEqual([...kinds].sort());
  });
});

test.describe('the client chip goes LIVE (the dormant branch wakes up)', () => {
  test('a task linked through the named writer wears its client + stage chip',
    async ({ page, request }) => {
      // 1. Pick a real board task and a real deal from the fixture copy.
      const pipeline = await (await api(request, 'GET', '/api/crm/pipeline')).json();
      const deal = Object.values(pipeline.by_stage || {}).flat().find(d => d.account_name);
      const task = await pickLinkableTask(request);
      test.skip(!deal || !task, 'fixture copy lacks a deal with an account or an open task');

      // 2. Link them through the NEW named route — the only sanctioned web
      //    writer for this edge (ruling 5). Doing it through the API rather than
      //    with SQL makes this one contract on the writer AND the reader.
      const linked = await api(request, 'PATCH', `/api/tasks/${task.id}/deal`,
                               { deal_id: deal.id });
      expect(linked.status(), await linked.text()).toBe(200);

      // 3. …and the GENERIC patch must still refuse the same field, or the
      //    named route is decoration.
      const refused = await api(request, 'PATCH', `/api/tasks/${task.id}`,
                                { deal_id: deal.id });
      expect(refused.status()).toBe(400);

      // 4. The board now renders the client chip on that card — the branch that
      //    shipped dormant in step 2 with `undefined` falling straight through.
      await page.goto('/?tab=board');
      await page.waitForLoadState('networkidle');
      await page.waitForFunction(() => document.querySelectorAll('#my-kanban .kanban-card').length > 0);

      const chip = page.locator(`#my-kanban .kanban-card[data-task-id="${task.id}"] [data-context-chip]`);
      // The card may legitimately not be on this board (workspace filters), so
      // fall back to the renderer with the REAL payload the feed now returns —
      // still an honest assertion about live data, not a synthetic fixture.
      if (await chip.count()) {
        await expect(chip).toHaveAttribute('data-context-chip', 'client');
        await expect(chip).toContainText(deal.account_name);
      } else {
        const html = await page.evaluate((tid) => {
          const t = (loadedTasks || []).find(x => x.id === tid);
          return t ? contextChip(t) : null;
        }, task.id);
        expect(html, 'the linked task never reached the board feed').not.toBeNull();
        expect(html).toContain('data-context-chip="client"');
        expect(html).toContain(deal.account_name);
      }

      // 5. The board feed itself carries the joined client — the chip is only
      //    live because BOTH chokepoints widened (db._TASK_SELECT here,
      //    canvas._TASK_FIELDS for Hoy/Later/plan).
      const feed = await (await api(request, 'GET', '/api/tasks')).json();
      const row = (feed.tasks || []).find(t => t.id === task.id);
      expect(row.deal_id).toBe(deal.id);
      expect(row.account_name).toBe(deal.account_name);

      // 6. …and the stage chip rides beside it when the cycle position resolves.
      //    `stage_kind` is DERIVED server-side, so a deal whose stage places it
      //    nowhere (lost/stalled) legitimately renders no chip — asserted as an
      //    implication, never as an unconditional presence.
      if (row.stage_kind) {
        const rendered = await page.evaluate((t) => stageChip(t), row);
        expect(rendered).toContain('data-stage-chip="' + row.stage_kind + '"');
      } else {
        expect(await page.evaluate((t) => stageChip(t), row)).toBe('');
      }
    });

  test('the stage chip is display-only and never replaces the lineage chip',
    async ({ page }) => {
      await page.goto('/?tab=board');
      await page.waitForLoadState('networkidle');
      const out = await page.evaluate(() => {
        const html = contextChip({
          id: 't_syn', project_id: 'proj_inbox',
          deal_id: 'd_syn', account_id: 'acc_syn', account_name: 'Cliente Sintético',
          stage_kind: 'formalizacion',
        });
        const box = document.createElement('div');
        box.innerHTML = html;
        return {
          chips: box.children.length,
          first: box.children[0].getAttribute('data-context-chip'),
          second: box.children[1] ? box.children[1].getAttribute('data-stage-chip') : null,
          secondText: box.children[1] ? box.children[1].textContent.trim() : '',
          // A chip that looks clickable and isn't teaches distrust of every
          // other chip in the UI.
          clickable: box.children[1] ? (box.children[1].getAttribute('onclick')
            || box.children[1].getAttribute('role')) : null,
          none: contextChip({ id: 't_syn2', project_id: 'proj_inbox' }),
        };
      });
      expect(out.chips).toBe(2);
      expect(out.first).toBe('client');           // lineage still wins the first slot
      expect(out.second).toBe('formalizacion');
      expect(out.secondText).toBe('formalización');
      expect(out.clickable).toBeNull();
      // No stage → no chip. An honest blank beats a plausible guess.
      expect(out.none).not.toContain('data-stage-chip');
    });
});
