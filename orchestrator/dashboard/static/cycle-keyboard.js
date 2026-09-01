/*
 * Keyboard navigation for the Cycle tab — roving tabindex (WAI-ARIA).
 *
 * Extracted from the inline dashboard script so the navigation MATH is unit
 * testable (jsdom): see tests/cycle-board-roving.test.mjs. Loaded in the browser
 * via <script src="/static/cycle-keyboard.js"> (attaches the handlers to window,
 * which the inline onkeydown="..." attributes resolve); also CommonJS-exportable
 * for Node.
 *
 * The whole navigation contract lives in rovingNextIndex(): from a position,
 * step ±1 to the next ENABLED item, skipping disabled ones and (with wrap)
 * cycling past the ends. The board's Tab/Shift+Tab (flat ring, wrap-around,
 * skip disabled) and arrow keys, and the calendar's arrows, are all built on it.
 */
;(function (root) {
  'use strict';

  // Pure: next ENABLED index from `current` moving by `step` (+1 fwd / -1 back).
  // Skips items where isEnabled(i) is false; wraps past the ends when `wrap`.
  // Returns `current` when no other enabled item exists (or would need to wrap
  // with wrap=false → clamps to `current`).
  function rovingNextIndex(count, current, step, isEnabled, wrap) {
    if (count <= 0) return -1;
    if (typeof isEnabled !== 'function') isEnabled = function () { return true; };
    if (wrap === undefined) wrap = true;
    var i = current;
    for (var n = 0; n < count; n++) {
      i += step;
      if (i < 0) { if (!wrap) return current; i = count - 1; }
      else if (i >= count) { if (!wrap) return current; i = 0; }
      if (i === current) return current;          // came all the way around
      if (isEnabled(i)) return i;
    }
    return current;
  }

  function _cardEnabled(card) {
    return !!card && card.getAttribute('aria-disabled') !== 'true';
  }

  // Roving tabindex: exactly one item is the tab stop (tabindex 0); focus it.
  function _moveTabstop(items, target) {
    if (!target) return;
    for (var i = 0; i < items.length; i++) items[i].setAttribute('tabindex', '-1');
    target.setAttribute('tabindex', '0');
    if (typeof target.focus === 'function') target.focus();
  }

  // Cycle board (2D grid of task cards across columns). Tab/Shift+Tab cycle the
  // flat card ring with wrap-around, skipping disabled (aria-disabled) cards;
  // arrows do column/row moves; Enter/Space opens the focused task.
  function cycleBoardKeydown(e) {
    var board = e.currentTarget;
    var cards = Array.prototype.slice.call(board.querySelectorAll('.kanban-card'));
    if (!cards.length) return;
    var isEnabled = function (i) { return _cardEnabled(cards[i]); };
    var cur = cards.indexOf(board.ownerDocument.activeElement);

    if (e.key === 'Tab') {
      e.preventDefault();
      // Entering with nothing focused: forward starts before the first card,
      // backward starts after the last, so the first step lands on an end.
      var from = cur < 0 ? (e.shiftKey ? cards.length : -1) : cur;
      var next = rovingNextIndex(cards.length, from, e.shiftKey ? -1 : 1, isEnabled, true);
      _moveTabstop(cards, cards[next]);
      return;
    }
    if (e.key === 'Enter' || e.key === ' ') {
      if (cur >= 0) { e.preventDefault(); cards[cur].click(); }
      return;
    }
    if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End'].indexOf(e.key) < 0) return;
    var cols = Array.prototype.slice.call(board.querySelectorAll('.kanban-column'))
      .map(function (col) { return Array.prototype.slice.call(col.querySelectorAll('.kanban-card')); });
    var ci = -1, ri = -1;
    cols.forEach(function (col, x) { var y = col.indexOf(board.ownerDocument.activeElement); if (y >= 0) { ci = x; ri = y; } });
    if (ci < 0) { e.preventDefault(); _moveTabstop(cards, cards[0]); return; }
    e.preventDefault();
    var nci = ci, nri = ri;
    if (e.key === 'ArrowDown') nri = Math.min(ri + 1, cols[ci].length - 1);
    else if (e.key === 'ArrowUp') nri = Math.max(ri - 1, 0);
    else if (e.key === 'Home') nri = 0;
    else if (e.key === 'End') nri = cols[ci].length - 1;
    else {                                          // Left/Right: nearest card in the adjacent non-empty column
      var dir = e.key === 'ArrowRight' ? 1 : -1, x = ci + dir;
      while (x >= 0 && x < cols.length && cols[x].length === 0) x += dir;
      if (x < 0 || x >= cols.length) return;
      nci = x; nri = Math.min(ri, cols[x].length - 1);
    }
    var target = cols[nci] && cols[nci][nri];
    if (target) _moveTabstop(cards, target);
  }

  // Calendar strip (1D listbox of week cells). Arrows/Home/End move with
  // wrap-around; Enter/Space opens/plans the focused cycle.
  function cycleCalKeydown(e) {
    var cells = Array.prototype.slice.call(e.currentTarget.querySelectorAll('.cycle-cell[tabindex]'));
    if (!cells.length) return;
    var i = cells.indexOf(e.currentTarget.ownerDocument.activeElement);
    if (i < 0) { i = 0; for (var k = 0; k < cells.length; k++) if (cells[k].getAttribute('tabindex') === '0') { i = k; break; } }
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (cells[i]) cells[i].click(); return; }
    if (e.key === 'Home') { e.preventDefault(); _moveTabstop(cells, cells[0]); return; }
    if (e.key === 'End') { e.preventDefault(); _moveTabstop(cells, cells[cells.length - 1]); return; }
    var step = (e.key === 'ArrowRight' || e.key === 'ArrowDown') ? 1
      : (e.key === 'ArrowLeft' || e.key === 'ArrowUp') ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    _moveTabstop(cells, cells[rovingNextIndex(cells.length, i, step, null, true)]);
  }

  function focusBoardCard(flat, target) { _moveTabstop(flat, target); }

  root.rovingNextIndex = rovingNextIndex;
  root.cycleBoardKeydown = cycleBoardKeydown;
  root.cycleCalKeydown = cycleCalKeydown;
  root.focusBoardCard = focusBoardCard;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = {
      rovingNextIndex: rovingNextIndex,
      cycleBoardKeydown: cycleBoardKeydown,
      cycleCalKeydown: cycleCalKeydown,
      focusBoardCard: focusBoardCard,
    };
  }
})(typeof window !== 'undefined' ? window : globalThis);
