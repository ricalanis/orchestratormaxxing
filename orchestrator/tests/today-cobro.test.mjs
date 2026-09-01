import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { JSDOM } from 'jsdom';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const dir = path.dirname(fileURLToPath(import.meta.url));
const componentPath = path.join(dir, '..', 'dashboard', 'static', 'today-cobro.js');
const cobro = require(componentPath);

function env() {
  const dom = new JSDOM('<!doctype html><body><div id="mount"></div></body>');
  return { dom, doc: dom.window.document, mount: dom.window.document.getElementById('mount') };
}

function row(id, daysLate = 0, extra = {}) {
  return {
    deal_id: id,
    title: `Deal ${id}`,
    account_name: `Account ${id}`,
    value: 1000,
    currency: 'MXN',
    expected_payment_date: '2026-08-06',
    invoiced: true,
    paid: false,
    delivered: true,
    days_late: daysLate,
    ...extra,
  };
}

function payload(extra = {}) {
  return {
    status: 'ok',
    date: '2026-08-04',
    week: { start: '2026-08-03', end: '2026-08-09', total: 0, rows: [] },
    month: { label: 'AGO', collected: 6000, expected: 120000, target: null },
    overdue: [],
    no_expected: [],
    leaks: {
      uninvoiced_count: 0,
      uninvoiced_value: 0,
      no_expected_count: 0,
      no_project_count: 0,
    },
    slippage: null,
    narrative: { severity: 'ok', text: 'Todo en orden' },
    ...extra,
  };
}

const opts = (extra = {}) => ({
  formatMoney: value => `$${value}`,
  ...extra,
});

test('empty-healthy model renders the header and exactly the healthy and month lines', () => {
  const model = cobro.buildModel(payload());
  assert.equal(model.emptyHealthy, true);
  assert.equal(model.week.measured, false);

  const e = env();
  cobro.render(e.mount, payload(), opts());
  const root = e.mount.querySelector('.today-cobro');
  assert.equal(root.dataset.state, 'empty');
  assert.equal(root.getAttribute('aria-label'), 'Cobro y flujo de efectivo');
  assert.equal(root.children.length, 3, 'header plus exactly two content lines');
  assert.match(root.textContent, /✓ Nada por cobrar esta semana/);
  assert.match(root.querySelector('.today-cobro-month').textContent, /AGO.*cobrado \$6000 · esperado \$120000/);
  assert.equal(root.querySelector('.today-cobro-narrative'), null);
});

test('overdue strip filters days_late below 3 and caps qualifying rows at two', () => {
  const e = env();
  cobro.render(e.mount, payload({
    overdue: [row('one', 1), row('twelve', 12), row('five', 5), row('four', 4)],
  }), opts());
  const strip = e.mount.querySelector('.today-cobro-overdue');
  const rows = [...strip.querySelectorAll('.today-cobro-overdue-row')];
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map(item => item.dataset.dealId), ['twelve', 'five']);
  assert.equal(strip.textContent.includes('Account one'), false);
  assert.equal(strip.textContent.includes('Account four'), false);
  assert.ok([...strip.querySelectorAll('button')].find(item => item.textContent === '+1 →'));
});

test('one- and two-day late rows stay amber in the week list and out of the red strip', () => {
  const e = env();
  cobro.render(e.mount, payload({
    week: { total: 2000, rows: [row('one', 1), row('two', 2)] },
    overdue: [row('one', 1), row('two', 2)],
  }), opts());
  assert.equal(e.mount.querySelector('.today-cobro-overdue'), null);
  const weekRows = [...e.mount.querySelectorAll('.today-cobro-week-row')];
  assert.equal(weekRows.length, 2);
  weekRows.forEach(item => {
    assert.equal(item.dataset.rowState, 'late');
    assert.ok(item.querySelector('.text-amber-300'));
    assert.equal(item.querySelector('.text-red-200'), null);
  });
});

test('sin fecha is unmeasured zinc and never emerald or amber', () => {
  const e = env();
  cobro.render(e.mount, payload({
    no_expected: [row('missing-date', 0, { expected_payment_date: null })],
    leaks: {
      uninvoiced_count: 0,
      no_expected_count: 1,
      no_project_count: 0,
      first_no_expected_deal_id: 'missing-date',
    },
  }), opts());
  const noDate = e.mount.querySelector('.today-cobro-leak-no-expected');
  assert.ok(noDate);
  assert.match(noDate.textContent, /1 sin fecha/);
  assert.match(noDate.className, /text-zinc-500/);
  assert.doesNotMatch(noDate.className, /text-(emerald|amber)-/);
  assert.equal(e.mount.querySelector('[data-deal-id="missing-date"].today-cobro-week-row'), null);
});

test('month omits bar and percentage without target, and renders capped bar with target', () => {
  const e = env();
  cobro.render(e.mount, payload({
    week: { total: 1000, rows: [row('week')] },
    month: { label: 'AGO', collected: 6000, expected: 120000, target: null },
  }), opts());
  assert.equal(e.mount.querySelector('.today-cobro-month-bar'), null);
  assert.equal(e.mount.querySelector('.today-cobro-month').textContent.includes('%'), false);
  assert.match(e.mount.querySelector('.today-cobro-month').textContent, /cobrado \$6000 · esperado \$120000/);

  cobro.render(e.mount, payload({
    week: { total: 1000, rows: [row('week')] },
    month: { label: 'AGO', collected: 6000, expected: 120000, target: 300000 },
  }), opts());
  const bar = e.mount.querySelector('.today-cobro-month-bar');
  assert.ok(bar);
  assert.equal(bar.firstElementChild.style.width, '2%');
  assert.match(e.mount.querySelector('.today-cobro-month').textContent, /\$6000 \/ \$120000/);
  assert.equal(e.mount.querySelector('.today-cobro-month').textContent.includes('%'), false);
});

test('week row and every leak button route to their specified deal ids', () => {
  const calls = [];
  const e = env();
  cobro.render(e.mount, payload({
    week: { total: 1000, rows: [row('week', 0, { title: 'Acme — Sprint 2' })] },
    leaks: {
      uninvoiced_count: 4,
      uninvoiced_value: 200500,
      no_expected_count: 2,
      no_project_count: 4,
      first_uninvoiced_deal_id: 'deal-a',
      first_no_expected_deal_id: 'deal-b',
      first_no_project_deal_id: 'deal-c',
    },
  }), opts({ onDeal: (id, title) => calls.push([id, title]) }));

  e.mount.querySelector('.today-cobro-week-row').click();
  [...e.mount.querySelectorAll('.today-cobro-leak')].forEach(item => item.click());
  assert.deepEqual(calls, [
    ['week', 'Acme — Sprint 2'],
    ['deal-a', undefined],
    ['deal-b', undefined],
    ['deal-c', undefined],
  ]);
});

test('buildModel tolerates null and missing payload keys', () => {
  const model = cobro.buildModel(null);
  assert.equal(model.emptyHealthy, true);
  assert.equal(model.week.total, 0);
  assert.deepEqual(model.week.rows, []);
  assert.deepEqual(model.overdue.shown, []);
  assert.deepEqual(model.leaks, []);
  assert.equal(model.month.hasTarget, false);
});

test('component source performs no network reads', async () => {
  const source = await readFile(componentPath, 'utf8');
  assert.equal(source.includes('fetch('), false);
});

// --- m18/m19 + the two element-tag regressions (2026-08-04) -----------------
// Both bugs were the same shape: element(doc, CLASSES, TEXT) with the 'span'
// tag missing, so createElement threw InvalidCharacterError — but only on
// payload shapes no fixture exercised (an uninvoiced week row wearing its
// delivery badge; a non-null slippage). These renders pin them closed.

test('an uninvoiced week row renders its delivery badge (live-payload regression)', () => {
  const { doc, mount } = env();
  const payload = {
    status: 'ok',
    week: { start: '2026-08-03', end: '2026-08-09', total: 75000, rows: [
      { deal_id: 'd_r1', title: 'Sedes', value: 75000, currency: 'MXN',
        expected_payment_date: '2026-08-05', date: '2026-08-05',
        invoiced: false, paid: false, delivered: false, days_late: 0 },
    ] },
    month: { label: 'AGO', collected: 0, invoiced: 0, expected: 75000, target: null },
    overdue: [], no_expected: [], leaks: {}, slippage: null,
    narrative: { severity: 'blind', text: 'x' },
  };
  cobro.render(mount, payload, {});
  const badge = [...mount.querySelectorAll('span')].find(el => el.textContent === 'sin entregar');
  assert.ok(badge, 'the sin-entregar badge must render, not throw');
});

test('a non-null slippage renders its chip (element-tag regression)', () => {
  const { doc, mount } = env();
  const payload = {
    status: 'ok',
    week: { start: '2026-08-03', end: '2026-08-09', total: 0, rows: [] },
    month: { label: 'AGO', collected: 100, invoiced: 100, expected: 100, target: null },
    overdue: [], no_expected: [], leaks: {},
    slippage: { median_days: 4, count: 3 },
    narrative: { severity: 'healthy', text: 'x' },
  };
  cobro.render(mount, payload, {});
  const chip = [...mount.querySelectorAll('span')].find(el => /media \+4d/.test(el.textContent));
  assert.ok(chip, 'the slippage chip must render, not throw');
});

test('launch rows are their own kind: 🧾 icon, blue on time, amber late, never red', () => {
  const onTime = cobro.buildModel({
    week: { rows: [{ deal_id: 'd_l1', title: 'L', value: 100, kind: 'launch',
      date: '2026-08-06', days_late: 0, invoiced: false, paid: false }] },
  }).week.rows[0];
  assert.equal(onTime.state.key, 'launch');
  assert.match(onTime.state.className, /blue/);
  assert.match(onTime.dateLabel, /^🧾 /);
  const late = cobro.buildModel({
    week: { rows: [{ deal_id: 'd_l2', title: 'L', value: 100, kind: 'launch',
      date: '2026-08-01', days_late: 5, invoiced: false, paid: false }] },
  }).week.rows[0];
  assert.equal(late.state.key, 'launch-late');
  assert.match(late.state.className, /amber/);
  assert.doesNotMatch(late.state.className, /red/);
});

test('the hero subtitle splits cobros from lanzamientos', () => {
  const { mount } = env();
  cobro.render(mount, {
    week: { total: 500, rows: [
      { deal_id: 'a', title: 'P', value: 500, kind: 'payment', date: '2026-08-06',
        expected_payment_date: '2026-08-06', invoiced: true, paid: false, days_late: 0 },
      { deal_id: 'b', title: 'L', value: 100, kind: 'launch', date: '2026-08-06',
        invoiced: false, paid: false, days_late: 0 },
    ] },
    month: { label: 'AGO', collected: 0, invoiced: 0, expected: 0, target: null },
    overdue: [], leaks: {}, slippage: null, narrative: { text: '' },
  }, {});
  assert.match(mount.textContent, /1 cobros · 1 lanzamiento/);
});

test('a paid row shows the real cash, and the month line shows facturado', () => {
  const model = cobro.buildModel({
    week: { rows: [{ deal_id: 'p1', title: 'P', value: 1000, cash: 900,
      kind: 'payment', paid: true, paid_on: '2026-08-04', date: '2026-08-04', days_late: 0 }] },
    month: { label: 'AGO', collected: 900, invoiced: 1000, expected: 1000, target: null },
  });
  assert.equal(model.week.rows[0].value, 900);
  assert.equal(model.month.invoiced, 1000);
  const { mount } = env();
  cobro.render(mount, {
    week: { total: 0, rows: [] },
    month: { label: 'AGO', collected: 900, invoiced: 1000, expected: 1000, target: null },
    overdue: [], leaks: {}, slippage: null, narrative: { text: '' },
  }, {});
  assert.match(mount.textContent, /facturado \$1,000/);
});

test('overdue launches leak in amber with their own deep link', () => {
  const model = cobro.buildModel({
    week: { rows: [] }, month: {},
    leaks: { launch_overdue_count: 2, first_launch_overdue_deal_id: 'd_lo' },
  });
  const item = model.leaks.find(l => l.key === 'launch-overdue');
  assert.ok(item);
  assert.equal(item.dealId, 'd_lo');
  assert.match(item.className, /amber/);
});
