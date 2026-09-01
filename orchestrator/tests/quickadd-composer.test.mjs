/*
 * jsdom tests for the Quick-Add COMPOSER (the mountQuickAdd component inlined in
 * dashboard/templates/index.html). The pure grammar is covered separately in
 * quickadd-parser.test.mjs; this file guards the half that only exists in the
 * DOM — key semantics, the sticky batch, the request shapes, and above all the
 * re-render survival guard (a create triggers loadTasks(), which replaces the
 * column's innerHTML; without QA_ACTIVE + qaRemount, batch entry loses the draft
 * and the focus).
 *
 * The component lives inside a 14k-line no-build template, so the block is
 * extracted by its section BANNERS (never line numbers) and evaluated against
 * stubbed dashboard globals. The extraction asserts its own anchors, so a moved
 * or renamed section fails loudly instead of silently testing the wrong code.
 *
 * Stdlib node:test runner + jsdom. Run:
 *   node --test tests/quickadd-composer.test.mjs
 */
import { test, before, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const dir = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(dir, '..');
const START = '// ==================== QUICK-ADD COMPOSER ====================';
const END = '// ==================== TOAST ====================';

function composerSource() {
  const html = fs.readFileSync(path.join(ROOT, 'dashboard', 'templates', 'index.html'), 'utf8');
  const a = html.indexOf(START), b = html.indexOf(END, a);
  assert.ok(a > 0, 'QUICK-ADD COMPOSER banner not found in index.html');
  assert.ok(b > a, 'TOAST banner not found after the composer section');
  return html.slice(a, b);
}

// Everything the composer reads from the surrounding dashboard script. Declared
// INSIDE the evaluated scope: jsdom does not surface properties assigned onto the
// window from Node as bare globals for evaluated code.
const STUBS = `
  var PROJECTS_BY_ID = { p_h: {id:'p_h', name:'hermes'}, p_i: {id:'p_i', name:'Icalia'} };
  var currentProjectFilter = '';
  var currentTab = 'board';
  var __sprint = { id: 'sp_w30', name: 'Sprint W30' };
  var __toasts = [], __renders = 0, __modal = null;
  var __fetchImpl = function(){ return Promise.resolve({ok:true, json:function(){return Promise.resolve({});}}); };
  function fetch(u,o){ return __fetchImpl(u,o); }
  function boardSprintSlot() { return __sprint; }
  function sprintShort(n) { var m = /W(\\d+)/.exec(n||''); return m ? 'W'+m[1] : (n||'Cycle'); }
  function currentIsoWeek(){ return '2026-W30'; }
  function nextIsoWeek(){ return '2026-W31'; }
  function escapeHtml(s){ return String(s).replace(/[&<>"']/g, function(c){
      return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
  function createTaskCard(t){ return '<div class="kanban-card">'+escapeHtml(t.title)+'</div>'; }
  function showTaskDetail(){}
  function toast(m,k,a){ __toasts.push({m:m,k:k,a:a}); }
  function loadTasks(){ __renders++; }
  function openNewTaskModal(sid, pre){ __modal = {sid:sid, pre:pre}; }
`;
const EXPORTS = `
  window.mountQuickAdd = mountQuickAdd;
  window.qaRemount = qaRemount;
  window.startQuickAdd = startQuickAdd;
  window.qaOpenFromKey = qaOpenFromKey;
  window.qaAnyModalOpen = qaAnyModalOpen;
  window.qaGhostHtml = qaGhostHtml;
  window.__active = function(){ return QA_ACTIVE; };
  window.__setFetch = function(f){ __fetchImpl = f; };
  Object.defineProperty(window, '__toasts',  {get:function(){return __toasts;}});
  Object.defineProperty(window, '__renders', {get:function(){return __renders;}});
  Object.defineProperty(window, '__modal',   {get:function(){return __modal;}});
`;
const MARKUP = `<body>
  <div id="toast-stack"></div>
  <div id="my-kanban"><div class="kanban-column" data-column="inbox">
    <div class="space-y-2 kanban-list" data-status="ready" data-col="inbox"></div>
    <button type="button" data-testid="qa-ghost" data-qa-col="list:mine:inbox" data-qa-kind="list">+ Add task</button>
  </div></div>
  <div id="board-kanban"><div class="kanban-column" data-column="pool_inbox">
    <div class="space-y-2 kanban-list" data-status="ready" data-col="pool_inbox"></div>
    <button type="button" data-testid="qa-ghost" data-qa-col="board:pool_inbox" data-qa-kind="board">+ Add task</button>
  </div><div class="kanban-column" data-column="in_progress">
    <div class="space-y-2 kanban-list" data-status="in_progress" data-col="in_progress"></div>
    <button type="button" data-testid="qa-ghost" data-qa-col="board:in_progress" data-qa-kind="board">+ Add task</button>
  </div></div>
  <textarea id="elsewhere"></textarea>
</body>`;

let SRC, w, fetches;

before(() => { SRC = composerSource(); });

beforeEach(() => {
  // runScripts is required: without it window.eval falls back to the Node realm
  // and the evaluated component can't see `window`/`document`.
  const dom = new JSDOM(MARKUP, { url: 'http://localhost', runScripts: 'dangerously' });
  w = dom.window;
  w.eval('(function(){' + STUBS + '\n' + SRC + '\n' + EXPORTS + '})()');
  w.eval(fs.readFileSync(path.join(ROOT, 'dashboard', 'static', 'quickadd-parser.js'), 'utf8'));
  // jsdom has no layout engine, so getClientRects() is always empty; the `n`
  // shortcut filters ghosts on it. Stub it so visibility can be exercised.
  w.Element.prototype.getClientRects = function () { return [{ width: 100, height: 20 }]; };
  fetches = [];
  w.__setFetch(async (url, opts) => {
    fetches.push({ url, opts, body: opts && opts.body ? JSON.parse(opts.body) : null });
    return { ok: true, json: async () => ({ task_id: 't_new1' }) };
  });
});

const q = (s) => w.document.querySelector(s);
const ghost = (col) => q('[data-testid="qa-ghost"][data-qa-col="' + col + '"]');
const input = () => q('[data-testid="qa-input"]');
const key = (el, k, mods = {}) =>
  el.dispatchEvent(new w.KeyboardEvent('keydown', Object.assign({ key: k, bubbles: true, cancelable: true }, mods)));
const type = (el, v) => { el.value = v; el.dispatchEvent(new w.Event('input', { bubbles: true })); };
const settle = () => new Promise(r => setTimeout(r, 20));
const mountBoard = () => { w.mountQuickAdd(ghost('board:pool_inbox')); return input(); };

// ---- activation -----------------------------------------------------------
test('ghost → composer: input mounted, focused, tagged with its column', () => {
  const el = mountBoard();
  assert.ok(el, 'composer mounted');
  assert.equal(w.document.activeElement, el, 'input autofocused');
  assert.equal(q('.qa-composer').dataset.qaCol, 'board:pool_inbox');
  assert.equal(el.getAttribute('aria-label'), 'Quick add task — #project !priority @when');
  assert.equal(el.getAttribute('role'), 'combobox');
});

// ---- autocomplete ---------------------------------------------------------
test('typing # opens the listbox and Enter accepts the highlighted project', () => {
  const el = mountBoard();
  type(el, 'Fix login #her');
  assert.ok(!q('[data-qa-pop]').classList.contains('hidden'), 'popover open');
  assert.equal(el.getAttribute('aria-expanded'), 'true');
  assert.ok(el.getAttribute('aria-activedescendant'), 'driven by aria-activedescendant');
  assert.equal(q('[data-qa-pop]').getAttribute('role'), 'listbox');
  key(el, 'Enter');                                  // accept, NOT create
  assert.equal(el.value, 'Fix login #hermes ');
  assert.ok(q('[data-testid="qa-chip-project"]'), 'project chip rendered');
  assert.equal(fetches.length, 0, 'accepting must not create');
});

// ---- create ---------------------------------------------------------------
test('Enter creates and STAYS: exact POST body, cleared input, sticky chips', async () => {
  const el = mountBoard();
  type(el, 'Fix login #hermes !p2');
  assert.ok(q('[data-testid="qa-chip-priority"]'), 'priority chip');
  key(el, 'Enter');
  await settle();
  assert.equal(fetches.length, 1);
  assert.deepEqual(fetches[0].body, {
    title: 'Fix login', project_id: 'p_h', assignee: 'ricardo', priority: 2, sprint_id: 'sp_w30',
  });
  assert.equal(input().value, '', 'input cleared');
  assert.ok(input(), 'composer still mounted');
  assert.ok(q('[data-testid="qa-chip-project"]'), 'sticky chips persist for the batch');
  assert.equal(w.__renders, 1, 'a create triggers the loadTasks() re-render');
});

test('sticky chips carry to the next create in the batch', async () => {
  const el = mountBoard();
  type(el, 'First #hermes !p2');
  key(el, 'Enter'); await settle();
  type(input(), 'Second task');
  key(input(), 'Enter'); await settle();
  assert.equal(fetches.length, 2);
  assert.equal(fetches[1].body.title, 'Second task');
  assert.equal(fetches[1].body.priority, 2, 'sticky priority applied');
  assert.equal(fetches[1].body.project_id, 'p_h', 'sticky project applied');
});

test('Cmd/Ctrl+Enter creates and CLOSES back to the ghost', async () => {
  const el = mountBoard();
  type(el, 'close me');
  key(el, 'Enter', { metaKey: true });
  await settle();
  assert.equal(fetches[0].body.title, 'close me');
  assert.ok(!input(), 'composer unmounted');
  assert.ok(ghost('board:pool_inbox'), 'ghost restored');
});

test('an empty title never POSTs', async () => {
  const el = mountBoard();
  type(el, '   ');
  key(el, 'Enter'); await settle();
  assert.equal(fetches.length, 0);
});

// ---- @when → the existing PATCH shapes ------------------------------------
test('@when beats the column slot and issues the modal PATCH shape', async () => {
  const el = mountBoard();
  type(el, 'ship it @nextweek');
  key(el, 'Enter'); await settle();
  assert.equal(fetches[0].body.sprint_id, null, 'explicit token > column context');
  assert.equal(fetches[1].url, '/api/tasks/t_new1');
  assert.deepEqual(fetches[1].body, { scheduled_week: '2026-W31' });
});

test('@thisweek also commits to the active cycle; @date sets due_date', async () => {
  let el = mountBoard();
  type(el, 'a @thisweek'); key(el, 'Enter'); await settle();
  assert.deepEqual(fetches[1].body, { scheduled_week: '2026-W30', assign_active_cycle: true });
  fetches.length = 0;
  type(input(), 'b @2026-08-01'); key(input(), 'Enter'); await settle();
  assert.deepEqual(fetches[1].body, { due_date: '2026-08-01' });
});

test('no @when on a board column → the column sprint rides on the create', async () => {
  const el = mountBoard();
  type(el, 'plain'); key(el, 'Enter'); await settle();
  assert.equal(fetches[0].body.sprint_id, 'sp_w30');
  assert.equal(fetches.length, 1, 'no scheduling PATCH needed');
});

// ---- Esc / Backspace ------------------------------------------------------
test('Esc is two-stage: clear text, then unmount — it never does both at once', () => {
  const el = mountBoard();
  type(el, 'draft text');
  key(el, 'Escape');
  assert.equal(input().value, '', 'stage: text cleared');
  assert.ok(input(), 'composer still open');
  key(input(), 'Escape');
  assert.ok(!input(), 'stage: unmounted');
  assert.ok(ghost('board:pool_inbox'), 'ghost restored');
  assert.equal(w.__active(), null, 'QA_ACTIVE cleared');
});

test('Esc with the popover open closes ONLY the popover', () => {
  const el = mountBoard();
  type(el, 'x #her');
  key(el, 'Escape');
  assert.ok(q('[data-qa-pop]').classList.contains('hidden'), 'popover closed');
  assert.equal(input().value, 'x #her', 'text untouched');
});

test('Backspace on an empty input dissolves the newest chip back into text', async () => {
  const el = mountBoard();
  type(el, 'task #hermes !p2');
  key(el, 'Enter'); await settle();               // → empty input, sticky chips
  assert.equal(input().value, '');
  key(input(), 'Backspace');
  assert.equal(input().value, '!p2', 'raw token text back in the input');
  assert.ok(!q('[data-testid="qa-chip-priority"]'), 'chip dissolved, stays literal');
  assert.ok(q('[data-testid="qa-chip-project"]'), 'the other chip is untouched');
});

test('Backspace dissolves the chip typed LAST, not a fixed type order', async () => {
  const el = mountBoard();
  // With the display order ['project','priority','when'] this always dissolved the
  // @when chip, whatever the user typed last (spec §3 says "most recent").
  type(el, 'task @nextweek #hermes ');   // trailing space: caret is out of the # chunk,
  key(el, 'Enter'); await settle();      // so Enter creates instead of accepting the popover
  key(input(), 'Backspace');
  assert.equal(input().value, '#hermes', 'the last thing typed comes back');
  assert.ok(q('[data-testid="qa-chip-when"]'), '@nextweek is untouched');
});

// ---- the guard that matters ----------------------------------------------
test('re-render survival: a clobbering render restores the draft, chips and focus', () => {
  const el = mountBoard();
  const draft = 'half typed #Icalia';
  type(el, draft);
  // Exactly what renderBoardTab() does: replace the column's innerHTML.
  q('#board-kanban').innerHTML =
    '<div class="kanban-column"><div class="space-y-2 kanban-list" data-status="ready"></div>' +
    w.qaGhostHtml('board:pool_inbox', 'board') + '</div>';
  assert.ok(!input(), 'the render clobbered the composer');
  w.qaRemount();                                   // the hook at the end of the render
  assert.ok(input(), 're-mounted');
  assert.equal(input().value, draft, 'draft preserved');
  assert.equal(w.document.activeElement, input(), 'refocused');
  assert.ok(q('[data-testid="qa-chip-project"]'), 'chips re-rendered from state');
});

test('qaRemount is idempotent — a live composer is never rebuilt', () => {
  const el = mountBoard();
  type(el, 'keep me');
  w.qaRemount(); w.qaRemount();
  assert.equal(w.document.querySelectorAll('[data-testid="qa-input"]').length, 1);
  assert.equal(input().value, 'keep me');
});

// ---- escalation -----------------------------------------------------------
test('Cmd/Ctrl+Shift+Enter escalates losslessly into the modal prefill', () => {
  const el = mountBoard();
  type(el, 'Fix login #hermes !p1 @2026-08-01');
  key(el, 'Enter', { metaKey: true, shiftKey: true });
  // Spread into this realm: jsdom objects fail strict deepEqual on prototype identity.
  // priority 3, not 1: !p1 is the operator's "most urgent" and Hermes's scale is
  // 0=Normal,1=Low,2=High,3=Urgent — the modal must highlight Urgent, so the
  // round-trip agrees with what was typed instead of inverting it.
  assert.deepEqual({ ...w.__modal.pre }, {
    title: 'Fix login', project_id: 'p_h', priority: 3, when: '2026-08-01', sprintId: null,
  });
  assert.ok(!input(), 'composer unmounted — the modal takes over');
  assert.equal(fetches.length, 0, 'escalation never creates');
});

test('escalation with no @when hands the column sprint to the modal', () => {
  const el = mountBoard();
  type(el, 'Fix login');
  key(el, 'Enter', { metaKey: true, shiftKey: true });
  assert.equal(w.__modal.pre.sprintId, 'sp_w30');
  assert.equal(w.__modal.sid, 'sp_w30');
});

// ---- the column's status --------------------------------------------------
test('a non-pool column PATCHes the status it advertises', async () => {
  w.mountQuickAdd(ghost('board:in_progress'));
  type(input(), 'Ship auth');
  key(input(), 'Enter'); await settle();
  assert.equal(fetches[0].url, '/api/tasks');
  assert.equal(fetches[1].url, '/api/tasks/t_new1', 'create has no status field — a PATCH follows');
  assert.deepEqual(fetches[1].body, { status: 'in_progress' },
    'the ghost row must not lie about which column the card lands in');
});

test('a Pool/Inbox column issues no status PATCH (that is already the create status)', async () => {
  const el = mountBoard();
  type(el, 'triage me'); key(el, 'Enter'); await settle();
  assert.equal(fetches.length, 1);
});

test('status and @when ride in ONE follow-up PATCH', async () => {
  w.mountQuickAdd(ghost('board:in_progress'));
  type(input(), 'both @nextweek');
  key(input(), 'Enter'); await settle();
  assert.deepEqual(fetches[1].body, { status: 'in_progress', scheduled_week: '2026-W31' });
});

// ---- priority scale -------------------------------------------------------
test('!p1 files as URGENT (3), not Low — the grammar is operator urgency', async () => {
  const el = mountBoard();
  type(el, 'Prod is down !p1'); key(el, 'Enter'); await settle();
  assert.equal(fetches[0].body.priority, 3);
  assert.ok(q('[data-testid="qa-chip-priority"]'), 'the chip still reads what was typed');
});

test('!p3 files as Low (1) and !p2 as High (2)', async () => {
  let el = mountBoard();
  type(el, 'someday thing !p3'); key(el, 'Enter'); await settle();
  assert.equal(fetches[0].body.priority, 1);
  fetches.length = 0;
  type(input(), 'other !p2'); key(input(), 'Enter'); await settle();
  assert.equal(fetches[0].body.priority, 2);
});

// ---- errors ---------------------------------------------------------------
test('a failed POST puts the exact submitted text back into an IDLE composer', async () => {
  const el = mountBoard();
  w.__setFetch(async () => ({ ok: false, json: async () => ({ detail: 'boom' }) }));
  type(el, 'will fail #hermes !p3');
  key(el, 'Enter');
  await settle();
  assert.equal(input().value, 'will fail #hermes !p3', 'nothing is eaten');
  assert.equal(w.__toasts.at(-1).k, 'err');
});

test('a LATE failure never overwrites the next task being typed (the batch flow)', async () => {
  const el = mountBoard();
  let reject;
  w.__setFetch(() => new Promise((_, rj) => { reject = rj; }));
  type(el, 'task one');
  key(el, 'Enter');                       // input clears, POST is in flight
  type(input(), 'task two');              // the user keeps typing the NEXT task
  reject(new Error('boom'));
  await settle();
  assert.equal(input().value, 'task two', 'the live draft is untouched');
  assert.equal(w.document.activeElement, input(), 'and still has the caret');
  const failed = q('[data-qa-failed]');
  assert.ok(failed, 'the failed capture is parked on its own card');
  assert.match(failed.textContent, /task one/);
});

test('Retry re-issues the POST — it does not rewrite the composer', async () => {
  const el = mountBoard();
  let reject;
  w.__setFetch(() => new Promise((_, rj) => { reject = rj; }));
  type(el, 'task one'); key(el, 'Enter');
  type(input(), 'task two');
  reject(new Error('boom')); await settle();
  fetches.length = 0;
  w.__setFetch(async (url, opts) => {
    fetches.push({ url, opts, body: opts && opts.body ? JSON.parse(opts.body) : null });
    return { ok: true, json: async () => ({ task_id: 't_retry' }) };
  });
  q('[data-qa-retry]').dispatchEvent(new w.MouseEvent('click', { bubbles: true }));
  await settle();
  assert.equal(fetches.length, 1, 'Retry actually retries');
  assert.equal(fetches[0].url, '/api/tasks');
  assert.equal(fetches[0].body.title, 'task one');
  assert.equal(input().value, 'task two', 'and leaves the live draft alone');
  assert.ok(!q('[data-qa-failed]'), 'the error card clears on success');
});

test('a failed capture survives the next board render', async () => {
  const el = mountBoard();
  let reject;
  w.__setFetch(() => new Promise((_, rj) => { reject = rj; }));
  type(el, 'task one'); key(el, 'Enter');
  type(input(), 'task two');
  reject(new Error('boom')); await settle();
  // Close the composer: the failed capture belongs to the COLUMN, so it must not
  // depend on a composer being re-mounted to come back.
  type(input(), ''); key(input(), 'Escape');
  assert.ok(!input(), 'composer unmounted');
  // The 45s poll: renderBoardTab() replaces the column's innerHTML, then remounts.
  q('#board-kanban').innerHTML =
    '<div class="kanban-column"><div class="space-y-2 kanban-list" data-status="ready"></div>' +
    w.qaGhostHtml('board:pool_inbox', 'board') + '</div>';
  w.qaRemount();
  const failed = q('[data-qa-failed]');
  assert.ok(failed, 'the only remaining copy of the text is not silently deleted');
  assert.match(failed.textContent, /task one/);
});

// ---- focus ownership ------------------------------------------------------
test('a poll-driven re-mount does NOT steal focus from elsewhere', () => {
  const el = mountBoard();
  type(el, 'draft that stays');
  const other = q('#elsewhere');
  other.focus();
  assert.equal(w.document.activeElement, other);
  q('#board-kanban').innerHTML =
    '<div class="kanban-column"><div class="space-y-2 kanban-list" data-status="ready"></div>' +
    w.qaGhostHtml('board:pool_inbox', 'board') + '</div>';
  w.qaRemount();
  assert.equal(input().value, 'draft that stays', 'the draft is still re-mounted');
  assert.equal(w.document.activeElement, other, 'but the keyboard stays where the user put it');
});

// ---- one composer ---------------------------------------------------------
test('only ONE composer can be live; the draft moves with it', () => {
  const el = mountBoard();
  type(el, 'draft A');
  w.mountQuickAdd(ghost('board:in_progress'));
  assert.equal(w.document.querySelectorAll('[data-testid="qa-input"]').length, 1,
    'the first composer is unmounted, not orphaned');
  assert.ok(ghost('board:pool_inbox'), 'its ghost is restored');
  assert.equal(w.__active().colKey, 'board:in_progress');
  assert.equal(input().value, 'draft A', 'and the draft is not eaten');
});

test('n focuses the live composer instead of opening a second one', () => {
  const el = mountBoard();
  type(el, 'keep me here');
  q('#elsewhere').focus();
  assert.equal(w.qaOpenFromKey(), true);
  assert.equal(w.document.querySelectorAll('[data-testid="qa-input"]').length, 1);
  assert.equal(w.__active().colKey, 'board:pool_inbox', 'no relocation');
  assert.equal(w.document.activeElement, input());
});

// ---- optimistic card ------------------------------------------------------
test('the pending card is inert — no .kanban-card drag surface, no id-bound buttons', () => {
  const el = mountBoard();
  w.__setFetch(() => new Promise(() => {}));      // never settles: the card stays pending
  type(el, 'pending');
  key(el, 'Enter');
  const holder = q('[data-qa-optimistic]');
  assert.ok(holder, 'the optimistic card rendered');
  assert.ok(!holder.querySelector('.kanban-card'), 'Sortable draggable selector is absent');
  assert.equal(holder.querySelectorAll('button').length, 0, 'no button can address qa_pending_N');
});

// ---- toast ----------------------------------------------------------------
test('a successful create offers both Edit and Undo', async () => {
  const el = mountBoard();
  type(el, 'done'); key(el, 'Enter'); await settle();
  const t = w.__toasts.at(-1);
  assert.equal(t.k, 'ok');
  assert.deepEqual(Array.from(t.a, a => a.label), ['Edit', 'Undo']);
});

// ---- the two mounts + the n key -------------------------------------------
test('the list mount is the same composer and inherits NO sprint', async () => {
  w.startQuickAdd(ghost('list:mine:inbox'), 'ready');
  const el = q('.qa-composer[data-qa-col="list:mine:inbox"] [data-testid="qa-input"]');
  assert.ok(el, 'list mount uses the composer');
  type(el, 'from the list');
  key(el, 'Enter'); await settle();
  assert.equal(fetches[0].body.sprint_id, null, 'the list has no WHEN column to inherit');
});

test('n opens the composer in the board column; a visible modal blocks it', () => {
  assert.equal(w.qaAnyModalOpen(), false);
  assert.equal(w.qaOpenFromKey(), true);
  assert.ok(q('#board-kanban [data-testid="qa-input"]'), 'opened in the board column');
  // Any visible full-screen overlay must suppress the shortcut.
  const modal = w.document.createElement('div');
  modal.className = 'fixed inset-0 z-50';
  w.document.body.appendChild(modal);
  assert.equal(w.qaAnyModalOpen(), true);
  modal.classList.add('hidden');
  assert.equal(w.qaAnyModalOpen(), false, 'a hidden modal does not count');
});
