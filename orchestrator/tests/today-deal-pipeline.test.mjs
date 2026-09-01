import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const dir = path.dirname(fileURLToPath(import.meta.url));
const pipeline = require(path.join(dir, '..', 'dashboard', 'static', 'today-deal-pipeline.js'));
const STAGES = ['lead', 'engaged', 'qualified', 'demo', 'proposal', 'won'];

function env() {
  const dom = new JSDOM('<!doctype html><body><div id="mount"></div></body>');
  return { dom, doc: dom.window.document, mount: dom.window.document.getElementById('mount') };
}

function deal(id, stage, value = 1000, extra = {}) {
  return { id, stage, title: `Deal ${id}`, value, currency: 'MXN', ...extra };
}

function payload(entries = {}) {
  return { by_stage: entries };
}

const opts = (extra = {}) => ({
  formatMoney: (value, currency) => `${currency} ${value}`,
  ...extra,
});

test('buildModel and render keep the six fixed live lanes in order, including zero lanes', () => {
  const model = pipeline.buildModel(payload({ proposal: [deal('p', 'proposal')] }));
  assert.deepEqual(model.lanes.map(l => l.stage), STAGES);
  assert.deepEqual(model.lanes.map(l => l.count), [0, 0, 0, 0, 1, 0]);

  const e = env();
  pipeline.render(e.mount, payload({ proposal: [deal('p', 'proposal')] }), opts());
  const lanes = [...e.mount.querySelectorAll('.today-deal-pipeline-lane')];
  assert.deepEqual(lanes.map(l => l.dataset.pipelineStage), STAGES);
  assert.equal(lanes.length, 6);
  assert.equal(e.mount.querySelector('[data-pipeline-stage="lead"]').textContent.includes('No deals'), true);
  assert.equal(e.mount.querySelector('[data-pipeline-stage="won"]').textContent.includes('No deals'), true);
});

test('each lane caps at two deal buttons and +N opens that stage in the full pipeline', () => {
  const calls = [];
  const e = env();
  pipeline.render(e.mount, payload({
    lead: [deal('a', 'lead'), deal('b', 'lead'), deal('c', 'lead'), deal('d', 'lead')],
  }), opts({ onFullPipeline: stage => calls.push(stage) }));
  const lead = e.mount.querySelector('[data-pipeline-stage="lead"]');
  assert.equal(lead.querySelectorAll('.today-deal-pipeline-deal').length, 2);
  assert.equal(lead.textContent.includes('Deal c'), false);
  assert.equal(lead.textContent.includes('Deal d'), false);
  const more = [...lead.querySelectorAll('button')].find(b => b.textContent === '+2 more');
  assert.ok(more);
  more.click();
  assert.deepEqual(calls, ['lead']);
});

test('selling total covers lead through proposal while Won / Active is separate', () => {
  const model = pipeline.buildModel(payload({
    lead: [deal('l', 'lead', 100)],
    engaged: [deal('e', 'engaged', 200)],
    qualified: [deal('q', 'qualified', 300)],
    demo: [deal('d', 'demo', 400)],
    proposal: [deal('p', 'proposal', 500)],
    won: [deal('w', 'won', 900)],
    stalled: [deal('s', 'stalled', 800)],
  }));
  assert.equal(model.sellingValue, 1500);
  assert.equal(model.sellingCount, 5);
  assert.equal(model.wonValue, 900);
  assert.equal(model.wonCount, 1);

  const e = env();
  pipeline.render(e.mount, payload({
    lead: [deal('l', 'lead', 100)], proposal: [deal('p', 'proposal', 500)],
    won: [deal('w', 'won', 900)],
  }), opts());
  assert.equal(e.mount.querySelector('.today-deal-pipeline-selling').textContent, 'Selling · 2 · MXN 600');
  assert.equal(e.mount.querySelector('.today-deal-pipeline-won').textContent, 'Won / Active · 1 · MXN 900');
});

test('delivered and lost never render; stalled is one compact footer, never a lane or card', () => {
  const e = env();
  pipeline.render(e.mount, payload({
    delivered: [deal('secret-delivered', 'delivered', 700, { title: 'Delivered secret' })],
    lost: [deal('secret-lost', 'lost', 600, { title: 'Lost secret' })],
    stalled: [deal('ice-1', 'stalled', 500, { title: 'Stalled private title' })],
  }), opts());
  assert.equal(e.mount.querySelector('[data-pipeline-stage="delivered"]'), null);
  assert.equal(e.mount.querySelector('[data-pipeline-stage="lost"]'), null);
  assert.equal(e.mount.querySelector('[data-pipeline-stage="stalled"]'), null);
  assert.equal(e.mount.textContent.includes('Delivered secret'), false);
  assert.equal(e.mount.textContent.includes('Lost secret'), false);
  assert.equal(e.mount.textContent.includes('Stalled private title'), false);
  const footer = e.mount.querySelector('.today-deal-pipeline-stalled');
  assert.ok(footer);
  assert.match(footer.textContent, /1 stalled · MXN 500/);
});

test('untrusted deal and stage text is escaped by construction', () => {
  const e = env();
  const hostile = '<img src=x onerror="globalThis.pwned=1">';
  pipeline.render(e.mount, payload({ lead: [deal('x', 'lead', 2, { title: hostile })] }), opts({
    stageMeta: { lead: { label: '<script>lane()</script>', color: '#fff' } },
  }));
  assert.equal(e.mount.querySelector('img'), null);
  assert.equal(e.mount.querySelector('script'), null);
  assert.match(e.mount.textContent, /<img src=x/);
  assert.match(e.mount.textContent, /<script>lane\(\)<\/script>/);
  assert.match(e.mount.innerHTML, /&lt;img src=x/);
});

test('deal, Full pipeline, +N, and stalled buttons call their navigation callbacks', () => {
  const dealCalls = [], fullCalls = [];
  const e = env();
  pipeline.render(e.mount, payload({
    lead: [deal('a', 'lead'), deal('b', 'lead'), deal('c', 'lead')],
    stalled: [deal('s', 'stalled')],
  }), opts({
    onDeal: (id, trigger) => dealCalls.push([id, trigger]),
    onFullPipeline: stage => fullCalls.push(stage),
  }));
  const first = e.mount.querySelector('[data-deal-id="a"]');
  assert.equal(first.tagName, 'BUTTON');
  assert.match(first.className, /focus-visible:/);
  assert.ok(first.getAttribute('aria-label'));
  first.click();
  e.mount.querySelector('[data-action="full-pipeline"]').click();
  e.mount.querySelector('[aria-label^="View 1 more Lead"]').click();
  e.mount.querySelector('.today-deal-pipeline-stalled').click();
  assert.equal(dealCalls[0][0], 'a');
  assert.equal(dealCalls[0][1], first, 'the activated deal button is passed to onDeal');
  assert.deepEqual(fullCalls, [null, 'lead', 'stalled']);
});

test('empty is a normal six-lane state, distinct from loading and error', () => {
  const e = env();
  pipeline.render(e.mount, payload(), opts());
  assert.equal(e.mount.firstElementChild.dataset.pipelineState, 'empty');
  assert.match(e.mount.textContent, /No active opportunities/);
  assert.equal(e.mount.querySelectorAll('.today-deal-pipeline-lane').length, 6);

  pipeline.renderLoading(e.mount);
  assert.equal(e.mount.firstElementChild.dataset.pipelineState, 'loading');
  assert.match(e.mount.textContent, /Loading deal pipeline/);
  assert.equal(e.mount.querySelector('.today-deal-pipeline-lane'), null);

  let retries = 0;
  pipeline.renderError(e.mount, () => { retries += 1; });
  assert.equal(e.mount.firstElementChild.dataset.pipelineState, 'error');
  assert.match(e.mount.textContent, /could not be loaded/);
  const buttons = e.mount.querySelectorAll('button');
  assert.equal(buttons.length, 1, 'Retry is the only error-state action');
  assert.equal(buttons[0].dataset.action, 'retry');
  assert.equal(buttons[0].textContent, 'Retry');
  buttons[0].click();
  assert.equal(retries, 1);
});

test('the fixed grid is confined to the component width for narrow Home layouts', () => {
  const e = env();
  pipeline.render(e.mount, payload(), opts());
  const root = e.mount.firstElementChild;
  const rail = e.mount.querySelector('.today-deal-pipeline-rail');
  assert.equal(root.style.maxWidth, '100%');
  assert.equal(root.style.overflow, 'hidden');
  assert.equal(rail.style.maxWidth, '100%');
  assert.equal(rail.style.display, 'grid');
  assert.match(rail.className, /grid-cols-2/);
  assert.match(rail.className, /sm:grid-cols-3/);
  assert.equal(rail.querySelector('[draggable]'), null, 'read-only view has no drag/drop surface');
});
