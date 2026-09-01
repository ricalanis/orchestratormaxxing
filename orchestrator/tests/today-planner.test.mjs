/*
 * jsdom/node unit tests for the Today planner's pure order math
 * (dashboard/static/today-planner.js) — the contract behind the interactive
 * daily plan: DOM order extraction (done cards INCLUDED), the top-3 partition,
 * ±1 / send-to-top reorders, shelf band grouping + already-planned filtering,
 * the 1-deep undo inverse, capacity math, and the debounce collapse.
 *
 * Stdlib node:test runner + jsdom (devDependency). Run:
 *   node --test tests/today-planner.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const dir = path.dirname(fileURLToPath(import.meta.url));
const TP = require(path.join(dir, '..', 'dashboard', 'static', 'today-planner.js'));

const T = (id, status = 'todo', extra = {}) => ({ id, status, title: id, ...extra });

// ---- order extraction from the rendered list ------------------------------
test('domOrder: reads direct children in DOM order, skipping dividers', () => {
  const dom = new JSDOM(`<div id="l">
      <div data-task-id="a"><button data-testid="today-move-up">up</button></div>
      <div class="divider">then</div>
      <div data-task-id="b"></div>
      <div class="divider">Done today · 1</div>
      <div data-task-id="c"></div>
    </div>`);
  const list = dom.window.document.getElementById('l');
  assert.deepEqual(TP.domOrder(list), ['a', 'b', 'c'], 'done card c is included at its sunk position');
});

test('domOrder: empty list and null are safe', () => {
  const dom = new JSDOM(`<div id="l"><div class="today-empty">Nothing planned.</div></div>`);
  assert.deepEqual(TP.domOrder(dom.window.document.getElementById('l')), []);
  assert.deepEqual(TP.domOrder(null), []);
});

// ---- done partition / front three -----------------------------------------
test('partitionDone: splits keeping relative order', () => {
  const { active, done } = TP.partitionDone([T('a'), T('b', 'done'), T('c'), T('d', 'done')]);
  assert.deepEqual(active.map(t => t.id), ['a', 'c']);
  assert.deepEqual(done.map(t => t.id), ['b', 'd']);
});

test('frontThreeIds: done cards never occupy an anchor slot', () => {
  const tasks = [T('a', 'done'), T('b'), T('c'), T('d'), T('e')];
  assert.deepEqual(TP.frontThreeIds(tasks), ['b', 'c', 'd']);
  assert.deepEqual(TP.frontThreeIds([T('x')]), ['x'], 'fewer than three is fine');
});

// ---- reorder math ----------------------------------------------------------
test('moveWithin: ±1 moves and clamping at both ends', () => {
  const ids = ['a', 'b', 'c'];
  assert.deepEqual(TP.moveWithin(ids, 'c', -1), ['a', 'c', 'b']);
  assert.deepEqual(TP.moveWithin(ids, 'a', +1), ['b', 'a', 'c']);
  assert.deepEqual(TP.moveWithin(ids, 'a', -1), ['a', 'b', 'c'], 'top card cannot wrap to the bottom');
  assert.deepEqual(TP.moveWithin(ids, 'c', +1), ['a', 'b', 'c'], 'bottom card cannot wrap to the top');
  assert.deepEqual(ids, ['a', 'b', 'c'], 'input array is never mutated');
});

test('moveWithin: unknown id is a no-op copy', () => {
  assert.deepEqual(TP.moveWithin(['a', 'b'], 'zz', -1), ['a', 'b']);
});

test('moveToTop: the MIT verb', () => {
  assert.deepEqual(TP.moveToTop(['a', 'b', 'c'], 'c'), ['c', 'a', 'b']);
  assert.deepEqual(TP.moveToTop(['a', 'b'], 'a'), ['a', 'b'], 'already first → unchanged');
});

// ---- priority bump (reorder-on-change) -------------------------------------
// The Do list keeps plan_order as its only sort key, so priority is honoured as a
// one-shot bump at the moment it's expressed. Everything below is a rule the
// product argued for explicitly — a symmetric "demote also moves it" version was
// rejected, and the Front Three are off-limits to anything but Urgent.
const PT = (id, p, status = 'todo', extra = {}) => T(id, status, { priority: p, ...extra });

test('bumpForPriority: raise to Urgent lands at position 0, anchors shift down', () => {
  const list = [PT('a', 0), PT('b', 0), PT('c', 0), PT('d', 0), PT('e', 0)];
  assert.equal(TP.bumpForPriority(list, 'e', 3), 0);
  // …but it stops UNDER an existing Urgent instead of outranking it.
  const withUrgent = [PT('a', 3), PT('b', 0), PT('c', 0), PT('d', 0), PT('e', 0)];
  assert.equal(TP.bumpForPriority(withUrgent, 'e', 3), 1);
});

test('bumpForPriority: raise to High lands at the top of the rest, never inside the Front Three', () => {
  const list = [PT('a', 0), PT('b', 0), PT('c', 0), PT('d', 0), PT('e', 0), PT('f', 0)];
  assert.equal(TP.bumpForPriority(list, 'f', 2), 3, 'below the three anchors, above the rest');
  // A custom front size is honoured (the anchor count is not hard-wired here).
  assert.equal(TP.bumpForPriority(list, 'f', 2, 1), 1);
});

test('bumpForPriority: the Front-Three clamp does not jump a raise over an equal peer', () => {
  // The clamp used to be applied AFTER a scan from 0, so it overrode the peer
  // scan: a new High landed at slot 3 — directly above `d`, an existing High
  // that already sits outside the anchors. Both readings of the spec ("first
  // slot whose peer it outranks" and "top of the rest") want it BELOW d.
  const list = [PT('a', 0), PT('b', 0), PT('c', 0), PT('d', 2), PT('e', 0), PT('f', 0)];
  assert.equal(TP.bumpForPriority(list, 'f', 2), 4, 'under the High already outside the anchors');
  // Urgent is the one level allowed inside the anchors — and it still stops
  // under an existing Urgent rather than outranking it.
  const u = [PT('a', 3), PT('b', 0), PT('c', 0), PT('d', 0), PT('e', 0), PT('f', 0)];
  assert.equal(TP.bumpForPriority(u, 'f', 3), 1);
  // Several equal peers below the clamp: it lands under ALL of them, not at the
  // first anchor-legal slot.
  const many = [PT('a', 0), PT('b', 0), PT('c', 0), PT('d', 2), PT('e', 2), PT('f', 0), PT('g', 0)];
  assert.equal(TP.bumpForPriority(many, 'g', 2), 5);
});

test('bumpForPriority: lowering never moves a deliberately-placed card', () => {
  const list = [PT('a', 0), PT('b', 0), PT('c', 0), PT('d', 3), PT('e', 0)];
  assert.equal(TP.bumpForPriority(list, 'd', 1), null, '→ Low: no move');
  assert.equal(TP.bumpForPriority(list, 'd', 0), null, '→ Normal: no move');
  assert.equal(TP.bumpForPriority(list, 'd', 3), null, 'same value: not a raise');
});

test('bumpForPriority: an already priority-correct card returns null (no write, no undo entry)', () => {
  assert.equal(TP.bumpForPriority([PT('a', 0), PT('b', 0), PT('c', 0)], 'a', 3), null, 'already first');
  assert.equal(TP.bumpForPriority([PT('a', 3), PT('b', 0), PT('c', 0)], 'b', 3), null, 'already under the urgent');
  // A raise must only ever move a card UP — the front-three clamp can never
  // turn a High raise into a demotion.
  assert.equal(TP.bumpForPriority([PT('a', 0), PT('b', 0), PT('c', 0)], 'b', 2), null);
});

test('bumpForPriority: done and waiting cards are excluded, and never eat a slot', () => {
  const list = [PT('a', 0, 'done'), PT('b', 0), PT('c', 0, 'blocked'), PT('d', 0, 'todo', { pinned_bottom: 1 }), PT('e', 0)];
  assert.equal(TP.bumpForPriority(list, 'a', 3), null, 'a done card is not reorderable');
  assert.equal(TP.bumpForPriority(list, 'c', 3), null, 'a blocked card lives in the waiting band');
  assert.equal(TP.bumpForPriority(list, 'd', 3), null, 'a parked card lives in the waiting band');
  assert.equal(TP.bumpForPriority(list, 'e', 3), 0, 'indices count workable cards only (b, e)');
});

// ---- shelf-band ordering ----------------------------------------------------
// The shelf's only production rule, extracted out of the renderer so it is
// falsifiable: it was previously an inline comparator with no test of any kind,
// and the live DB has every shelf card at priority 0, so a regression (an
// unstable sort, an in-place sort, undefined priorities compared as NaN) would
// have shipped invisible.
test('sortByPriority: descending, with missing/garbage priority reading as Normal', () => {
  const list = [PT('a', 0), PT('b', 3), PT('c', 1), PT('d', 2)];
  assert.deepEqual(TP.sortByPriority(list).map(t => t.id), ['b', 'd', 'c', 'a']);
  assert.deepEqual(TP.sortByPriority([T('a'), PT('b', 2), PT('c', null), PT('d', 'x')]).map(t => t.id),
    ['b', 'a', 'c', 'd'], 'no priority / null / NaN all sort as 0 and keep their arrival order');
});

test('sortByPriority: STABLE — equal priorities keep the server age order', () => {
  // The band arrives age-sorted from the server; priority is a re-rank WITHIN it,
  // not a reshuffle. Long enough to defeat any engine's small-array insertion path.
  const list = 'abcdefghijklmnopqrst'.split('').map((id, i) => PT(id, i === 7 ? 3 : 0));
  assert.deepEqual(TP.sortByPriority(list).map(t => t.id).join(''), 'habcdefgijklmnopqrst');
});

test('sortByPriority: does not mutate the caller (the band array is TODAY_DATA)', () => {
  const list = [PT('a', 0), PT('b', 3)];
  const out = TP.sortByPriority(list);
  assert.deepEqual(list.map(t => t.id), ['a', 'b'], 'the input array is untouched');
  assert.notEqual(out, list);
  assert.deepEqual(TP.sortByPriority(null), [], 'a missing band is empty, not a throw');
  assert.deepEqual(TP.sortByPriority([]), []);
});

test('bumpForPriority: unknown id and empty list are safe', () => {
  assert.equal(TP.bumpForPriority([PT('a', 0)], 'nope', 3), null);
  assert.equal(TP.bumpForPriority([], 'a', 3), null);
  assert.equal(TP.bumpForPriority(null, 'a', 3), null);
});

test('composeOrder: done ids ride along at the end (replace must not unplan them)', () => {
  assert.deepEqual(TP.composeOrder(['b', 'a'], ['z']), ['b', 'a', 'z']);
});

test('applyOrder: reorders tasks, keeps tasks the id list forgot', () => {
  const tasks = [T('a'), T('b'), T('c')];
  assert.deepEqual(TP.applyOrder(tasks, ['c', 'a']).map(t => t.id), ['c', 'a', 'b']);
  assert.deepEqual(TP.applyOrder(tasks, ['c', 'nope', 'c']).map(t => t.id), ['c', 'a', 'b']);
});

// ---- shelf bands -----------------------------------------------------------
test('shelfBands: fixed band order, source tags, planned filtered out', () => {
  const bands = TP.shelfBands({
    candidates: [
      T('o1', 'todo', { why: 'overdue' }),
      T('c1', 'todo', { why: 'carry_over' }),
      T('y1', 'todo', { why: 'cycle' }),
      T('client1', 'todo', { why: 'cliente' }),
      T('already', 'todo', { why: 'cycle' }),
    ],
    laterGroups: { this_week: [T('w1'), T('y1')] },
    plannedIds: ['already'],
  });
  assert.deepEqual(bands.map(b => b.key), ['overdue', 'carry_over', 'cycle', 'cliente', 'this_week']);
  assert.deepEqual(bands.map(b => b.tasks.map(t => t.id)), [['o1'], ['c1'], ['y1'], ['client1'], ['w1']]);
});

test('shelfBands: a task already planned for today never shows in any band', () => {
  const bands = TP.shelfBands({
    candidates: [T('x', 'todo', { why: 'overdue' })],
    laterGroups: { this_week: [T('x')] },
    plannedIds: ['x'],
  });
  assert.equal(bands.reduce((n, b) => n + b.tasks.length, 0), 0);
});

test('shelfBands: empty inputs still return the five fixed bands', () => {
  const bands = TP.shelfBands({});
  assert.equal(bands.length, 5);
  assert.ok(bands.every(b => b.tasks.length === 0));
});

// ---- undo ------------------------------------------------------------------
test('inverseOp: any plan mutation inverts to one replace of the prior order', () => {
  const inv = TP.inverseOp({ prevOrder: ['a', 'b'], label: 'pull-in' });
  assert.equal(inv.kind, 'replace');
  assert.deepEqual(inv.task_ids, ['a', 'b']);
  assert.match(inv.message, /pull-in/);
  assert.equal(TP.inverseOp(null), null);
  assert.equal(TP.inverseOp({}), null, 'no prior order → nothing to undo');
});

// The 1-deep stack must be able to say WHICH mutation it holds: a reorder that
// silently inherits an earlier pull-in's inverse makes `u` destructive (it
// unplans a task instead of restoring the rank) — the collapse rule in
// _todayPushReorderUndo keys off exactly this field.
test('inverseOp: carries the mutation label so only reorders collapse', () => {
  assert.equal(TP.inverseOp({ prevOrder: ['a'], label: 'reorder' }).label, 'reorder');
  assert.equal(TP.inverseOp({ prevOrder: ['a'], label: 'pull-in' }).label, 'pull-in');
  assert.notEqual(TP.inverseOp({ prevOrder: ['a'], label: 'pull-in' }).label, 'reorder',
    'a pull-in entry must never be mistaken for a collapsible reorder');
  assert.equal(TP.inverseOp({ prevOrder: ['a'] }).label, null);
});

// ---- capacity --------------------------------------------------------------
test('typicalPerDay: mean of recent completed cycle velocities over workdays', () => {
  const cycles = [
    { status: 'planning', velocity: 0 },
    { status: 'active', velocity: 0 },
    { status: 'completed', velocity: 23 },
    { status: 'completed', velocity: 30 },
    { status: 'completed', velocity: 22 },
    { status: 'completed', velocity: 99 },
  ];
  assert.equal(TP.typicalPerDay(cycles), 5, '(23+30+22)/3 / 5 workdays → 5');
  assert.equal(TP.typicalPerDay([]), null, 'no evidence → unknown, never a number');
  assert.equal(TP.typicalPerDay([{ status: 'active', velocity: 12 }]), null);
});

test('capacityState: advisory only — amber past typical, silent without evidence', () => {
  assert.deepEqual(TP.capacityState(6, 4), { level: 'over', label: '6 planned', hint: 'you typically land 4/day' });
  assert.equal(TP.capacityState(3, 4).level, 'ok');
  assert.equal(TP.capacityState(9, null).level, 'unknown');
  assert.equal(TP.capacityState(9, null).hint, '');
});

// ---- debounce collapse -----------------------------------------------------
test('makeDebouncer: a burst of N reorders collapses to ONE write', () => {
  let now = 0, seq = 0;
  const pending = new Map();
  const timers = {
    setTimeout: (fn, ms) => { const id = ++seq; pending.set(id, { fn, at: now + ms }); return id; },
    clearTimeout: (id) => pending.delete(id),
  };
  const tick = (ms) => {
    now += ms;
    [...pending.entries()].forEach(([id, t]) => { if (t.at <= now) { pending.delete(id); t.fn(); } });
  };
  let writes = 0;
  const debounced = TP.makeDebouncer(() => { writes++; }, 400, timers);
  debounced(); tick(100); debounced(); tick(100); debounced();
  assert.equal(writes, 0, 'nothing written mid-burst');
  tick(400);
  assert.equal(writes, 1, 'three key-moves → one POST');
  debounced(); tick(400);
  assert.equal(writes, 2, 'a later burst writes again');
});

// ---- blocked band ----------------------------------------------------------
test('partitionPlan: three bands — workable, blocked, done — order preserved', () => {
  const tasks = [
    { id: 'a', status: 'todo' }, { id: 'b', status: 'blocked' },
    { id: 'c', status: 'in_progress' }, { id: 'd', status: 'done' },
    { id: 'e', status: 'blocked' },
  ];
  const p = TP.partitionPlan(tasks);
  assert.deepEqual(p.active.map(t => t.id), ['a', 'c']);
  assert.deepEqual(p.blocked.map(t => t.id), ['b', 'e']);
  assert.deepEqual(p.done.map(t => t.id), ['d']);
});

test('frontThreeIds: blocked cards never occupy an anchor slot', () => {
  const tasks = [
    { id: 'a', status: 'blocked' }, { id: 'b', status: 'todo' },
    { id: 'c', status: 'todo' }, { id: 'd', status: 'todo' }, { id: 'e', status: 'todo' },
  ];
  assert.deepEqual(TP.frontThreeIds(tasks), ['b', 'c', 'd']);
});

test('composeOrder: blocked ids ride the payload between active and done — never dropped', () => {
  assert.deepEqual(TP.composeOrder(['a', 'c'], ['d'], ['b', 'e']), ['a', 'c', 'b', 'e', 'd']);
  // Legacy 2-arg call still composes active+done (no silent behavior change).
  assert.deepEqual(TP.composeOrder(['a'], ['d']), ['a', 'd']);
});

test('partitionDone: legacy two-band view keeps blocked cards in active', () => {
  const tasks = [{ id: 'a', status: 'todo' }, { id: 'b', status: 'blocked' }, { id: 'd', status: 'done' }];
  const p = TP.partitionDone(tasks);
  assert.deepEqual(p.active.map(t => t.id), ['a', 'b']);
  assert.deepEqual(p.done.map(t => t.id), ['d']);
});

test('partitionPlan: parked (pinned_bottom) joins the waiting band after blocked, keeps status', () => {
  const tasks = [
    { id: 'a', status: 'todo' },
    { id: 'p', status: 'in_progress', pinned_bottom: 1 },
    { id: 'b', status: 'blocked' },
    { id: 'd', status: 'done', pinned_bottom: 1 },
  ];
  const part = TP.partitionPlan(tasks);
  assert.deepEqual(part.active.map(t => t.id), ['a']);
  assert.deepEqual(part.blocked.map(t => t.id), ['b', 'p'], 'blocked first, then parked');
  assert.deepEqual(part.done.map(t => t.id), ['d'], 'done wins over parked');
});

test('frontThreeIds: parked cards never occupy an anchor slot', () => {
  const tasks = [
    { id: 'p', status: 'todo', pinned_bottom: 1 }, { id: 'b', status: 'todo' },
    { id: 'c', status: 'todo' }, { id: 'd', status: 'todo' }, { id: 'e', status: 'todo' },
  ];
  assert.deepEqual(TP.frontThreeIds(tasks), ['b', 'c', 'd']);
});
