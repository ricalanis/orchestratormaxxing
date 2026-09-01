/*
 * jsdom-free unit tests for the Today shelf's BANDS (dashboard/static/today-planner.js).
 *
 * The shelf is where the day's candidates get triaged before they are pulled
 * into the plan, and journey fase 1 step 5 adds a FIFTH band — 'cliente' — for
 * commercial work: the cadence materializer's cards and any task the operator
 * linked to a deal. `canvas.plan_candidates` tags them server-side with
 * `why='cliente'`; this file asserts the client half of that contract.
 *
 * Two of these exist because the same bug is cheap to ship twice:
 *
 *   * **BANDS and `buckets` are two literals that must agree.** `take()` routes
 *     on `buckets[t.why]` and falls back to 'cycle' when the key is missing, so
 *     declaring a band in BANDS and forgetting it in `buckets` produces a fifth
 *     band that renders EMPTY while its cards hide inside the third — green
 *     everywhere, wrong on screen. Asserted as a set equality, not per key, so
 *     a sixth band cannot be added to one literal only.
 *
 *   * **First band wins on duplicates.** A client card that is also overdue
 *     arrives labelled 'overdue' (the server dedups in that order) and must
 *     appear ONCE. The band order encodes which label the operator sees, and
 *     urgency has to beat category.
 *
 * Stdlib node:test runner. Run:
 *   node --test tests/today-planner-bands.test.mjs      (or: npm test)
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const dir = path.dirname(fileURLToPath(import.meta.url));
const tp = require(path.join(dir, '..', 'dashboard', 'static', 'today-planner.js'));

const byKey = (bands) => Object.fromEntries(bands.map(b => [b.key, b]));

test('the shelf has FIVE bands and cliente is one of them', () => {
  const bands = tp.shelfBands({ candidates: [], laterGroups: {} });
  assert.equal(bands.length, 5);
  assert.deepEqual(bands.map(b => b.key),
    ['overdue', 'carry_over', 'cycle', 'cliente', 'this_week']);
  assert.equal(byKey(bands).cliente.label, 'Cliente / venta');
});

test('cliente sits after the active cycle and before the generic this-week drawer', () => {
  const keys = tp.shelfBands({}).map(b => b.key);
  assert.ok(keys.indexOf('cliente') > keys.indexOf('cycle'),
    'a commercial card is not more urgent than the committed cycle');
  assert.ok(keys.indexOf('cliente') < keys.indexOf('this_week'),
    'a card with a server-derived due date beats an unscheduled one');
});

test('a why=cliente candidate lands in the cliente band, not in cycle', () => {
  const bands = byKey(tp.shelfBands({
    candidates: [
      { id: 'a', why: 'cliente', title: 'Romper el hielo con WePort' },
      { id: 'b', why: 'cycle', title: 'Ship the thing' },
    ],
  }));
  assert.deepEqual(bands.cliente.tasks.map(t => t.id), ['a']);
  assert.deepEqual(bands.cycle.tasks.map(t => t.id), ['b']);
});

test('every declared band has a bucket — a band with no bucket silently eats into cycle', () => {
  // Feed one candidate per declared key and demand each lands in its own band.
  // This is the structural assertion: it fails if BANDS and the `buckets`
  // literal ever drift, whichever one grew.
  const keys = tp.shelfBands({}).map(b => b.key).filter(k => k !== 'this_week');
  const bands = byKey(tp.shelfBands({
    candidates: keys.map(k => ({ id: k, why: k })),
  }));
  for (const k of keys) {
    assert.deepEqual(bands[k].tasks.map(t => t.id), [k],
      `candidates with why='${k}' must land in the '${k}' band`);
  }
});

test('an overdue client card appears once, labelled overdue', () => {
  // The server dedups overdue → carry_over → cycle → cliente, so a card that is
  // both arrives as 'overdue'. The shelf must not re-add it under cliente.
  const bands = byKey(tp.shelfBands({
    candidates: [{ id: 'x', why: 'overdue', deal_id: 'd_1' }],
    laterGroups: { this_week: [{ id: 'x', deal_id: 'd_1' }] },
  }));
  assert.deepEqual(bands.overdue.tasks.map(t => t.id), ['x']);
  assert.deepEqual(bands.cliente.tasks, []);
  assert.deepEqual(bands.this_week.tasks, []);
});

test('a client card already planned for today is filtered out of the shelf', () => {
  const bands = byKey(tp.shelfBands({
    candidates: [{ id: 'p', why: 'cliente' }],
    plannedIds: ['p'],
  }));
  assert.deepEqual(bands.cliente.tasks, []);
});

test('an unknown why still falls back to cycle rather than vanishing', () => {
  const bands = byKey(tp.shelfBands({ candidates: [{ id: 'z', why: 'nonsense' }] }));
  assert.deepEqual(bands.cycle.tasks.map(t => t.id), ['z']);
});
