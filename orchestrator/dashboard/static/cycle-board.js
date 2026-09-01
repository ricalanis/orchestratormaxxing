/*
 * Pure rendering helpers for the Cycle board's status columns.
 *
 * Extracted from the inline dashboard script so the EMPTY-STATE rendering is
 * unit-testable (jsdom): see tests/cycle-board-empty.test.mjs. Loaded in the
 * browser via <script src="/static/cycle-board.js"> (attaches to window); also
 * CommonJS-exportable for Node.
 *
 * A status column with zero cards renders a dashed placeholder ("Nothing in
 * flight", …) instead of an empty box, so a column never visually collapses —
 * on first render, when filtered to nothing, or after a drag empties it.
 */
;(function (root) {
  'use strict';

  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // The placeholder for a column with zero cards. `column.empty` is the
  // per-column copy; a generic message is the fallback. Marked .cycle-col-empty
  // (so it can be reconciled after a drag) and role="note".
  function emptyColumnHtml(column) {
    var msg = (column && column.empty) || 'No sprints in this state';
    return '<div class="cycle-col-empty text-[11px] text-zinc-400 border border-dashed ' +
      'border-zinc-800 rounded-xl py-6 text-center" role="note">' + escapeHtml(msg) + '</div>';
  }

  // The inner HTML for a column's card list: the cards when there are any, else
  // the empty placeholder. `count` is the number of cards; `cardsHtml` their markup.
  function columnBodyHtml(column, count, cardsHtml) {
    return count > 0 ? (cardsHtml || '') : emptyColumnHtml(column);
  }

  // Look up a column's meta by key from `columns`, which may be an array (like
  // CYCLE_COLUMNS) or a {key: meta} map. Returns a bare {key} when unknown.
  function metaFor(columns, key) {
    if (columns) {
      if (typeof columns.find === 'function') {
        var m = columns.find(function (c) { return c && c.key === key; });
        if (m) return m;
      } else if (columns[key]) {
        return columns[key];
      }
    }
    return { key: key };
  }

  // Reconcile every column's count badge + empty placeholder against the LIVE
  // DOM — the single source of truth after a drag (cross-column) or reorder.
  // `root` is the board container; `columns` supplies each column's placeholder
  // copy. For each .kanban-column: set its [data-count] badge to the number of
  // .kanban-card in that column's list, drop the .cycle-col-empty placeholder
  // when cards are present, and (re)insert it when the column is empty so it
  // never collapses. Returns a {key: count} map (used by callers/tests).
  function syncColumnCounts(root, columns) {
    var counts = {};
    if (!root) return counts;
    var cols = root.querySelectorAll('.kanban-column');
    for (var i = 0; i < cols.length; i++) {
      var col = cols[i];
      var list = col.querySelector('.kanban-list');
      var n = list ? list.querySelectorAll('.kanban-card').length : 0;
      var badge = col.querySelector('[data-count]');
      if (badge) badge.textContent = String(n);
      if (!list) continue;
      counts[list.getAttribute('data-col')] = n;
      var placeholder = list.querySelector('.cycle-col-empty');
      if (n > 0) {
        if (placeholder) placeholder.remove();
      } else if (!placeholder) {
        list.insertAdjacentHTML('beforeend', emptyColumnHtml(metaFor(columns, list.getAttribute('data-col'))));
      }
    }
    return counts;
  }

  var api = {
    emptyColumnHtml: emptyColumnHtml,
    columnBodyHtml: columnBodyHtml,
    syncColumnCounts: syncColumnCounts,
  };
  root.CycleBoard = api;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  }
})(typeof window !== 'undefined' ? window : globalThis);
