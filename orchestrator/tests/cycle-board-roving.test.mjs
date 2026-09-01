/*
 * jsdom unit tests for the Cycle board's roving-tabindex keyboard navigation
 * (dashboard/static/cycle-keyboard.js). Guards the navigation contract:
 * Tab/Shift+Tab cycling, focus wrap-around, and disabled-card skip.
 *
 * Stdlib node:test runner + jsdom (devDependency). Run:
 *   node --test tests/cycle-board-roving.test.mjs      (or: npm test)
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const dir = path.dirname(fileURLToPath(import.meta.url));
const kb = require(path.join(dir, '..', 'dashboard', 'static', 'cycle-keyboard.js'));

// ---- the pure navigation math (the whole contract) ------------------------
test('rovingNextIndex: forward and backward step', () => {
  const en = () => true;
  assert.equal(kb.rovingNextIndex(4, 0, +1, en, true), 1);   // Tab
  assert.equal(kb.rovingNextIndex(4, 2, -1, en, true), 1);   // Shift+Tab
});

test('rovingNextIndex: wraps around the ends', () => {
  const en = () => true;
  assert.equal(kb.rovingNextIndex(3, 2, +1, en, true), 0, 'last → first (Tab wrap)');
  assert.equal(kb.rovingNextIndex(3, 0, -1, en, true), 2, 'first → last (Shift+Tab wrap)');
});

test('rovingNextIndex: no wrap clamps to current at the ends', () => {
  const en = () => true;
  assert.equal(kb.rovingNextIndex(3, 2, +1, en, false), 2);
  assert.equal(kb.rovingNextIndex(3, 0, -1, en, false), 0);
});

test('rovingNextIndex: skips disabled indices', () => {
  const disabled = new Set([1, 2]);
  const en = (i) => !disabled.has(i);
  assert.equal(kb.rovingNextIndex(4, 0, +1, en, true), 3, 'forward skips 1 and 2');
  assert.equal(kb.rovingNextIndex(4, 3, +1, en, true), 0, 'forward from 3 wraps past disabled to 0');
  assert.equal(kb.rovingNextIndex(4, 0, -1, en, true), 3, 'backward from 0 wraps to 3 (skips 1,2)');
});

test('rovingNextIndex: all-others-disabled stays put', () => {
  const en = (i) => i === 1;             // only current is enabled
  assert.equal(kb.rovingNextIndex(3, 1, +1, en, true), 1);
});

// ---- DOM behavior: Tab / Shift+Tab move focus via the handler --------------
function makeBoard(columns) {
  // columns: array of arrays of { id, disabled? }
  const dom = new JSDOM('<!doctype html><body><div id="cycle-kanban" role="grid"></div></body>');
  const doc = dom.window.document;
  const board = doc.getElementById('cycle-kanban');
  for (const col of columns) {
    const colEl = doc.createElement('div');
    colEl.className = 'kanban-column';
    for (const card of col) {
      const c = doc.createElement('div');
      c.className = 'kanban-card';
      c.id = card.id;
      c.setAttribute('role', 'button');
      c.setAttribute('tabindex', '-1');
      if (card.disabled) c.setAttribute('aria-disabled', 'true');
      colEl.appendChild(c);
    }
    board.appendChild(colEl);
  }
  const first = board.querySelector('.kanban-card');
  if (first) first.setAttribute('tabindex', '0');
  board.addEventListener('keydown', kb.cycleBoardKeydown);
  return { dom, doc, board };
}

function pressTab(env, shift = false) {
  const active = env.doc.activeElement;
  const from = active && active.classList && active.classList.contains('kanban-card') ? active : env.board;
  from.dispatchEvent(new env.dom.window.KeyboardEvent('keydown', { key: 'Tab', shiftKey: shift, bubbles: true }));
  return env.doc.activeElement;
}

const flatCards = (env) => [...env.board.querySelectorAll('.kanban-card')];

test('Tab cycles focus forward through cards', () => {
  const env = makeBoard([[{ id: 'a' }, { id: 'b' }], [{ id: 'c' }]]);
  env.doc.getElementById('a').focus();
  assert.equal(pressTab(env).id, 'b');
  assert.equal(pressTab(env).id, 'c');
});

test('Shift+Tab cycles focus backward through cards', () => {
  const env = makeBoard([[{ id: 'a' }, { id: 'b' }], [{ id: 'c' }]]);
  env.doc.getElementById('c').focus();
  assert.equal(pressTab(env, true).id, 'b');
  assert.equal(pressTab(env, true).id, 'a');
});

test('focus wraps around: Tab past the last card returns to the first', () => {
  const env = makeBoard([[{ id: 'a' }], [{ id: 'b' }, { id: 'c' }]]);
  env.doc.getElementById('c').focus();          // last card
  assert.equal(pressTab(env).id, 'a', 'Tab wraps last → first');
  env.doc.getElementById('a').focus();          // first card
  assert.equal(pressTab(env, true).id, 'c', 'Shift+Tab wraps first → last');
});

test('disabled cards are skipped during Tab cycling', () => {
  const env = makeBoard([[{ id: 'a' }, { id: 'b', disabled: true }], [{ id: 'c' }]]);
  env.doc.getElementById('a').focus();
  assert.equal(pressTab(env).id, 'c', 'skips the aria-disabled b');
  assert.equal(pressTab(env).id, 'a', 'wraps back to a, still skipping b');
});

test('roving invariant: exactly one card is the tab stop (tabindex 0)', () => {
  const env = makeBoard([[{ id: 'a' }, { id: 'b' }], [{ id: 'c' }]]);
  env.doc.getElementById('a').focus();
  pressTab(env);   // → b
  const stops = flatCards(env).filter((c) => c.getAttribute('tabindex') === '0');
  assert.equal(stops.length, 1);
  assert.equal(stops[0].id, 'b');
});

test('Enter opens the focused card (click)', () => {
  const env = makeBoard([[{ id: 'a' }]]);
  let clicked = false;
  env.doc.getElementById('a').addEventListener('click', () => { clicked = true; });
  env.doc.getElementById('a').focus();
  env.doc.getElementById('a').dispatchEvent(
    new env.dom.window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  assert.equal(clicked, true);
});
