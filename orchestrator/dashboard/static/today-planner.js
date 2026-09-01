/*
 * Today planner — the pure ORDER MATH + composition rules behind the Today
 * tab's interactive daily plan (index.html: renderToday / persistTodayOrder /
 * todayMove / the shelf drawer).
 *
 * Extracted from the inline dashboard script so the CONTRACT is unit testable
 * (jsdom): see tests/today-planner.test.mjs. Loaded in the browser via
 * <script src="/static/today-planner.js"> (attaches window.TodayPlanner); also
 * CommonJS-exportable for Node.
 *
 * Everything here is a pure function over plain data — no DOM writes, no fetch.
 * `plan_order` is the SERVER's order: this module only ever computes an order
 * the client PROPOSES through the sanctioned endpoints (POST /api/day-plan
 * {replace:true} · PATCH /api/tasks/{id}/plan), after which the server is truth
 * again (the Today tab's server-side composition doctrine, api.py ~1034).
 */
;(function (root) {
  'use strict';

  // --- order extraction ----------------------------------------------------
  // The DOM order of a plan list, DIRECT CHILDREN only: dividers ("then",
  // "Done today · N") and empty-state hints carry no data-task-id, so they are
  // skipped, and done cards ARE included at their sunk positions (the batch
  // replace must list them or they'd be unplanned).
  function domOrder(listEl) {
    var out = [];
    if (!listEl) return out;
    var kids = listEl.children || [];
    for (var i = 0; i < kids.length; i++) {
      var id = kids[i].getAttribute && kids[i].getAttribute('data-task-id');
      if (id) out.push(id);
    }
    return out;
  }

  // Three bands: workable, waiting, done. The waiting band unifies the two
  // panel semantics — status 'blocked' (can't proceed) and parked/pinned_bottom
  // (waiting on someone else, status unchanged — the board's 📌). Blocked cards
  // list before parked; neither is reorderable nor occupies an anchor slot.
  function partitionPlan(tasks) {
    var active = [], blockedOnly = [], parked = [], done = [];
    (tasks || []).forEach(function (t) {
      if (!t) return;
      if (t.status === 'done') done.push(t);
      else if (t.status === 'blocked') blockedOnly.push(t);
      else if (t.pinned_bottom) parked.push(t);
      else active.push(t);
    });
    return { active: active, blocked: blockedOnly.concat(parked), done: done };
  }

  // Done cards sink below the divider but keep their relative order.
  // (Blocked cards count as active here — this is the two-band legacy view;
  // band-aware callers use partitionPlan.)
  function partitionDone(tasks) {
    var p = partitionPlan(tasks);
    return { active: p.active.concat(p.blocked), done: p.done };
  }

  // The Front Three are POSITIONAL (no backend field): the first `n` workable
  // cards of the plan. Done and blocked cards never occupy an anchor slot.
  function frontThreeIds(tasks, n) {
    if (n === undefined) n = 3;
    return partitionPlan(tasks).active.slice(0, n).map(function (t) { return t.id; });
  }

  // Move one id by `delta` slots inside `ids`. Clamped at the ends (no wrap —
  // an accidental extra ↑ on the MIT must not send it to the bottom). Returns
  // a NEW array; returns a copy unchanged when the move is a no-op.
  function moveWithin(ids, id, delta) {
    var arr = (ids || []).slice();
    var from = arr.indexOf(id);
    if (from < 0 || !delta) return arr;
    var to = from + delta;
    if (to < 0) to = 0;
    if (to > arr.length - 1) to = arr.length - 1;
    if (to === from) return arr;
    arr.splice(to, 0, arr.splice(from, 1)[0]);
    return arr;
  }

  // The MIT verb: send to position 1.
  function moveToTop(ids, id) {
    var arr = (ids || []).slice();
    var from = arr.indexOf(id);
    if (from <= 0) return arr;
    arr.splice(0, 0, arr.splice(from, 1)[0]);
    return arr;
  }

  // --- priority bump (reorder-on-change) -----------------------------------
  // `plan_order` stays the ONLY sort key for the Do list (the server never
  // re-sorts a planned day), so priority is honoured as an IMPULSE: raising a
  // card's priority moves it once, at the moment the user expresses it, and the
  // list is manual again afterwards. Returns the target index INSIDE the
  // workable band, or null for "no move". Every rule here is deliberate:
  //   · lower/equal priority NEVER moves a card — a demotion must not yank a
  //     deliberately-placed card out from under the user (drag it down instead).
  //   · done / blocked / parked cards are excluded: they live in their own bands
  //     and are not reorderable, so a bump on one is a no-op.
  //   · Urgent may enter the Front Three (the anchors shift down one slot — the
  //     one user-initiated interaction with the anchors); High lands at the top
  //     of the "rest" BELOW the anchors, never inside them.
  //   · a raise only ever moves a card UP. A computed slot at or below where the
  //     card already sits means "already priority-correct" → null (no write, no
  //     undo entry), and it makes an accidental demotion-by-bump impossible.
  var FRONT_N = 3;
  function bumpForPriority(list, id, newPriority, frontN) {
    if (frontN === undefined) frontN = FRONT_N;
    var active = partitionPlan(list).active;
    var from = -1;
    for (var i = 0; i < active.length; i++) { if (active[i].id === id) { from = i; break; } }
    if (from < 0) return null;                       // not a workable planned card
    var newP = prio({ priority: newPriority });
    var oldP = prio(active[from]);
    if (newP <= oldP) return null;                   // raises only
    var peers = active.slice(0, from).concat(active.slice(from + 1));
    // A non-Urgent raise may not enter the Front Three, so the scan starts BELOW
    // the anchors. Scanning from the clamp (rather than clamping after a scan
    // from 0) is what keeps the two rules from contradicting each other: with
    // [a0,b0,c0,d2,e0,f0], raising f to High used to land at 3 — i.e. ABOVE d,
    // an existing High already outside the anchors — because the clamp overrode
    // the peer scan. "First slot whose peer it outranks" and "top of the rest"
    // agree once the scan simply begins at the first legal slot.
    var start = newP < 3 ? Math.min(frontN, peers.length) : 0;
    var to = peers.length;                           // nothing it outranks → the tail
    for (var j = start; j < peers.length; j++) {
      if (prio(peers[j]) < newP) { to = j; break; }
    }
    if (to >= from) return null;                     // already correct / would demote
    return to;
  }

  // Shelf bands: the BAND stays the primary grouping (why a card is on the shelf
  // beats how loud it is); priority orders within one band. Lives here, not
  // inlined in the renderer, because it is order computation — the one thing this
  // module exists to make Node-testable. Three properties are contractual:
  //   · descending priority, missing/garbage priority reading as 0 (Normal);
  //   · STABLE — the server's age order is the tiebreak, so equal-priority cards
  //     keep the order they arrived in (Array#sort is spec-stable since ES2019);
  //   · non-mutating — the caller's band array is shared with the cached payload,
  //     so sorting in place would silently re-rank TODAY_DATA.
  function sortByPriority(tasks) {
    return (tasks || []).slice().sort(function (a, b) {
      return prio(b) - prio(a);
    });
  }
  function prio(t) {
    var n = Number(t && t.priority);
    return isFinite(n) ? n : 0;
  }

  // The payload order: active cards in rank, then blocked, then the done stack
  // at its sunk position. Blocked and done ids are ALWAYS included —
  // replace:true unplans anything absent, and plan_day never touches status
  // (verified: canvas.plan_day). blockedIds is optional (legacy 2-arg calls).
  function composeOrder(activeIds, doneIds, blockedIds) {
    return (activeIds || []).concat(blockedIds || []).concat(doneIds || []);
  }

  // Reorder a task array to match a proposed id order. Unknown ids are ignored;
  // tasks missing from `ids` keep their relative order at the end (so a stale
  // DOM read can never silently drop a task from the model).
  function applyOrder(tasks, ids) {
    var byId = {};
    (tasks || []).forEach(function (t) { if (t && t.id) byId[t.id] = t; });
    var out = [], seen = {};
    (ids || []).forEach(function (id) {
      if (byId[id] && !seen[id]) { out.push(byId[id]); seen[id] = 1; }
    });
    (tasks || []).forEach(function (t) { if (t && t.id && !seen[t.id]) out.push(t); });
    return out;
  }

  // --- the shelf -----------------------------------------------------------
  // Fixed bands, in fixed order. Sources: the standup's candidates (already
  // tagged server-side with `why` ∈ overdue|carry_over|cycle) plus the Later
  // drawer's this-week bucket. First band wins on duplicates, and anything
  // already planned for today is filtered out (client-side view filter — the
  // server stays the composer).
  // 'cliente' is the FIFTH band (journey fase 1, step 5): tasks that name a
  // deal — the cadence materializer's cards and any task the operator linked to
  // a client. It sits after the active cycle and before the generic this-week
  // drawer because a commercial card carries a due date the server computed
  // from a real cadence, while `this_week` is everything else that is merely
  // unplanned. Server-side, `canvas.plan_candidates` dedups it LAST, so a
  // client card that is also overdue arrives labelled 'overdue' and renders in
  // band 1 — urgency is the more actionable of the two labels.
  var BANDS = [
    ['overdue', 'Overdue'],
    ['carry_over', 'Carried over'],
    ['cycle', 'Active cycle'],
    ['cliente', 'Cliente / venta'],
    ['this_week', 'This week'],
  ];

  function shelfBands(opts) {
    opts = opts || {};
    var planned = {};
    (opts.plannedIds || []).forEach(function (id) { planned[id] = 1; });
    // Must list every BANDS key: `take` routes on `buckets[t.why]`, so a band
    // declared above but missing here silently falls through to 'cycle' — the
    // fifth band would render empty while its cards hid in the third.
    var buckets = { overdue: [], carry_over: [], cycle: [], cliente: [], this_week: [] };
    var seen = {};
    function take(key, t) {
      if (!t || !t.id || seen[t.id] || planned[t.id]) return;
      seen[t.id] = 1;
      buckets[key].push(t);
    }
    (opts.candidates || []).forEach(function (t) {
      var key = t && buckets[t.why] ? t.why : 'cycle';
      take(key, t);
    });
    var lg = opts.laterGroups || {};
    (lg.this_week || []).forEach(function (t) { take('this_week', t); });
    return BANDS.map(function (b) {
      return { key: b[0], label: b[1], tasks: buckets[b[0]] };
    });
  }

  // --- undo (1-deep) -------------------------------------------------------
  // Every plan mutation (pull-in, kick-out, reorder) is invertible by ONE call:
  // re-committing the previous id order with replace:true. That single inverse
  // restores rank, re-plans a kicked-out task at its old slot, and unplans a
  // pulled-in one — no snapshot surgery, and it reuses the sanctioned endpoint.
  function inverseOp(op) {
    if (!op || !op.prevOrder) return null;
    return {
      kind: 'replace',
      task_ids: op.prevOrder.slice(),
      // `label` is kept on the op so the caller can tell WHICH mutation is on the
      // 1-deep stack — consecutive reorders collapse, but a reorder must never
      // reuse an earlier pull-in's inverse (that undoes the wrong, destructive op).
      label: op.label || null,
      message: op.label ? ('Undid ' + op.label) : 'Undone',
    };
  }

  // --- capacity (advisory, count-based — never blocks) ---------------------
  // Typical daily throughput from the cycle velocity feed: mean velocity of the
  // most recent completed cycles ÷ workdays. null when there's no evidence yet
  // (an unknown baseline must not paint the chip amber).
  function typicalPerDay(cycles, workdays, sample) {
    if (workdays === undefined) workdays = 5;
    if (sample === undefined) sample = 3;
    var vals = (cycles || [])
      .filter(function (c) { return c && c.status === 'completed' && c.velocity > 0; })
      .slice(0, sample)
      .map(function (c) { return c.velocity; });
    if (!vals.length) return null;
    var mean = vals.reduce(function (a, b) { return a + b; }, 0) / vals.length;
    return Math.max(1, Math.round(mean / workdays));
  }

  function capacityState(planned, typical) {
    var n = planned || 0;
    var label = n + ' planned';
    if (!typical) return { level: 'unknown', label: label, hint: '' };
    return {
      level: n > typical ? 'over' : 'ok',
      label: label,
      hint: 'you typically land ' + typical + '/day',
    };
  }

  // --- debounce ------------------------------------------------------------
  // A burst of keyboard reorders is ONE write: the DOM moves immediately, the
  // POST collapses. `timers` is injectable so the collapse is testable.
  function makeDebouncer(fn, wait, timers) {
    var T = timers || root;
    var handle = null;
    return function () {
      var args = arguments, self = this;
      if (handle !== null) T.clearTimeout(handle);
      handle = T.setTimeout(function () { handle = null; fn.apply(self, args); }, wait);
    };
  }

  var API = {
    domOrder: domOrder,
    partitionDone: partitionDone,
    partitionPlan: partitionPlan,
    frontThreeIds: frontThreeIds,
    moveWithin: moveWithin,
    moveToTop: moveToTop,
    bumpForPriority: bumpForPriority,
    sortByPriority: sortByPriority,
    composeOrder: composeOrder,
    applyOrder: applyOrder,
    shelfBands: shelfBands,
    BANDS: BANDS,
    inverseOp: inverseOp,
    typicalPerDay: typicalPerDay,
    capacityState: capacityState,
    makeDebouncer: makeDebouncer,
  };

  if (typeof module !== 'undefined' && module.exports) module.exports = API;
  root.TodayPlanner = API;
})(typeof window !== 'undefined' ? window : globalThis);
