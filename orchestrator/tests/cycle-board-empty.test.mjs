/*
 * jsdom unit tests for the Cycle board's EMPTY-STATE rendering
 * (dashboard/static/cycle-board.js): a status column with zero cards renders a
 * dashed placeholder so the column never visually collapses; a column with cards
 * renders the cards and no placeholder.
 *
 * Stdlib node:test runner + jsdom (devDependency). Run:
 *   node --test tests/cycle-board-empty.test.mjs      (or: npm test)
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { JSDOM } from 'jsdom';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const dir = path.dirname(fileURLToPath(import.meta.url));
const cb = require(path.join(dir, '..', 'dashboard', 'static', 'cycle-board.js'));

const COL = { key: 'in_progress', label: 'In Progress', empty: 'Nothing in flight' };

// Parse an HTML string into a container element we can query.
function frag(html) {
  return new JSDOM(`<div id="root">${html}</div>`).window.document.getElementById('root');
}

test('emptyColumnHtml: renders the column-specific placeholder', () => {
  const root = frag(cb.emptyColumnHtml(COL));
  const ph = root.querySelector('.cycle-col-empty');
  assert.ok(ph, 'placeholder element present');
  assert.equal(ph.getAttribute('role'), 'note');
  assert.match(ph.textContent, /Nothing in flight/);
});

test('emptyColumnHtml: falls back to a generic message when the column has no copy', () => {
  const root = frag(cb.emptyColumnHtml({ key: 'x' }));
  assert.match(root.querySelector('.cycle-col-empty').textContent, /No sprints in this state/);
});

test('emptyColumnHtml: escapes the message (no HTML injection)', () => {
  const root = frag(cb.emptyColumnHtml({ empty: '<script>x</script>' }));
  assert.equal(root.querySelector('script'), null, 'no live <script> injected');
  assert.match(root.querySelector('.cycle-col-empty').innerHTML, /&lt;script&gt;/);
});

test('columnBodyHtml: zero cards → placeholder (column does not collapse)', () => {
  const root = frag(cb.columnBodyHtml(COL, 0, ''));
  assert.ok(root.querySelector('.cycle-col-empty'), 'placeholder shown for empty column');
  assert.equal(root.querySelector('.kanban-card'), null, 'no cards');
});

test('columnBodyHtml: ignores stray cardsHtml when count is 0', () => {
  // Defensive: a zero count wins even if cardsHtml is non-empty.
  const root = frag(cb.columnBodyHtml(COL, 0, '<div class="kanban-card">ghost</div>'));
  assert.ok(root.querySelector('.cycle-col-empty'));
  assert.equal(root.querySelector('.kanban-card'), null);
});

test('columnBodyHtml: with cards → cards, no placeholder', () => {
  const cards = '<div class="kanban-card">a</div><div class="kanban-card">b</div>';
  const root = frag(cb.columnBodyHtml(COL, 2, cards));
  assert.equal(root.querySelectorAll('.kanban-card').length, 2);
  assert.equal(root.querySelector('.cycle-col-empty'), null, 'no placeholder when cards present');
});

// ── Column count-badge reconcile (syncColumnCounts) ──────────────────────────
// Simulates the DOM mutation SortableJS performs on a cross-column drag —
// moving a .kanban-card node from one column's list to another — then asserts
// the per-column count badges (and empty placeholders) update to match.

const COLUMNS = [
  { key: 'backlog',     label: 'Backlog',     empty: 'Nothing parked' },
  { key: 'in_progress', label: 'In Progress', empty: 'Nothing in flight' },
  { key: 'review',      label: 'Review',      empty: 'Nothing to review' },
  { key: 'done',        label: 'Done',        empty: 'Nothing shipped yet' },
];

// Build a live board document with the given per-column card counts; each column
// has a header count badge (stale on purpose) and a card list.
function makeBoard(countsByKey) {
  const dom = new JSDOM('<div id="cycle-kanban"></div>');
  const doc = dom.window.document;
  const board = doc.getElementById('cycle-kanban');
  board.innerHTML = COLUMNS.map(c => {
    const n = countsByKey[c.key] || 0;
    const cards = Array.from({ length: n }, (_, i) =>
      `<div class="kanban-card" data-task-id="${c.key}-${i}"></div>`).join('');
    // Header count badge is seeded to 0 (stale) so a passing test proves the
    // reconcile actually wrote it, not that it happened to match.
    return `<div class="kanban-column">
      <div class="flex"><span>${c.label}</span><div data-count>0</div></div>
      <div class="kanban-list" data-status="${c.key}" data-col="${c.key}">${cb.columnBodyHtml(c, n, cards)}</div>
    </div>`;
  }).join('');
  return { doc, board };
}

const badgeText = (board, key) =>
  board.querySelector(`.kanban-list[data-col="${key}"]`).closest('.kanban-column')
    .querySelector('[data-count]').textContent;

test('syncColumnCounts: writes each column badge from the live DOM', () => {
  const { board } = makeBoard({ backlog: 1, in_progress: 3, review: 0, done: 2 });
  const counts = cb.syncColumnCounts(board, COLUMNS);
  assert.deepEqual(counts, { backlog: 1, in_progress: 3, review: 0, done: 2 });
  assert.equal(badgeText(board, 'in_progress'), '3');
  assert.equal(badgeText(board, 'backlog'), '1');
  assert.equal(badgeText(board, 'done'), '2');
});

test('syncColumnCounts: badges update after a simulated cross-column drag', () => {
  const { doc, board } = makeBoard({ backlog: 0, in_progress: 3, review: 0, done: 0 });
  cb.syncColumnCounts(board, COLUMNS);   // establish baseline badges
  assert.equal(badgeText(board, 'in_progress'), '3');
  assert.equal(badgeText(board, 'done'), '0');

  // SortableJS-style DOM move: pull one card out of In Progress, drop it in Done.
  const from = board.querySelector('.kanban-list[data-col="in_progress"]');
  const to = board.querySelector('.kanban-list[data-col="done"]');
  const donePlaceholder = to.querySelector('.cycle-col-empty');
  assert.ok(donePlaceholder, 'Done starts with an empty placeholder');
  to.appendChild(from.querySelector('.kanban-card'));

  cb.syncColumnCounts(board, COLUMNS);

  assert.equal(badgeText(board, 'in_progress'), '2', 'source badge decremented');
  assert.equal(badgeText(board, 'done'), '1', 'target badge incremented');
  assert.equal(to.querySelector('.cycle-col-empty'), null, 'Done placeholder removed once it has a card');
});

test('syncColumnCounts: emptying a column restores its placeholder + zero badge', () => {
  const { board } = makeBoard({ backlog: 0, in_progress: 1, review: 0, done: 0 });
  cb.syncColumnCounts(board, COLUMNS);
  const from = board.querySelector('.kanban-list[data-col="in_progress"]');
  const to = board.querySelector('.kanban-list[data-col="done"]');
  assert.equal(from.querySelector('.cycle-col-empty'), null, 'non-empty column has no placeholder');

  to.appendChild(from.querySelector('.kanban-card'));   // In Progress → empty
  cb.syncColumnCounts(board, COLUMNS);

  assert.equal(badgeText(board, 'in_progress'), '0');
  const ph = from.querySelector('.cycle-col-empty');
  assert.ok(ph, 'emptied column regains its placeholder (no collapse)');
  assert.match(ph.textContent, /Nothing in flight/, 'placeholder uses the column-specific copy');
});
