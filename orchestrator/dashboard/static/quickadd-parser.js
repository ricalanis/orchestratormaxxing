/*
 * Quick-Add token grammar — the pure parser behind the inline composer.
 *
 * Extracted from the inline dashboard script so the GRAMMAR is unit testable
 * (no DOM): see tests/quickadd-parser.test.mjs. Loaded in the browser via
 * <script src="/static/quickadd-parser.js"> (attaches window.QuickAddParser +
 * window.__parseQuickTitle, which mountQuickAdd resolves); also
 * CommonJS-exportable for Node. Same pattern as static/cycle-keyboard.js.
 *
 * Three closed token types, all matched only at WORD BOUNDARIES (a token is a
 * whole whitespace-delimited chunk — `foo#bar` is never a project token):
 *   #<text>        project   — resolved against the caller's project list
 *   !p1 | !p2 | !p3 priority — the `!` sigil is REQUIRED ("fix p1 incident"
 *                              must not become a priority; see spec §4)
 *   @thisweek | @nextweek | @someday | @YYYY-MM-DD   scheduling
 *
 * Deliberately closed vocabulary: no weekday words, no free-text date NLP, no
 * recurrence — ever. A token that does not resolve stays LITERAL in the title
 * (never guess). Duplicate type: the last resolved one wins, earlier ones
 * revert to literal text. `raw` is retained on every token so the composer can
 * un-parse a chip back into the input.
 */
;(function (root) {
  'use strict';

  var WHEN_WORDS = {
    thisweek: { field: 'scheduled_week', assignActiveCycle: true },
    nextweek: { field: 'scheduled_week', assignActiveCycle: false },
    someday:  { field: null,             assignActiveCycle: false },  // backlog: no PATCH
  };
  var ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

  // A real calendar date, not just the shape (rejects 2026-02-31 / month 13).
  function _validIsoDate(s) {
    if (!ISO_DATE.test(s)) return false;
    var p = s.split('-'), y = +p[0], m = +p[1], d = +p[2];
    if (m < 1 || m > 12 || d < 1) return false;
    var dt = new Date(Date.UTC(y, m - 1, d));
    return dt.getUTCFullYear() === y && dt.getUTCMonth() === m - 1 && dt.getUTCDate() === d;
  }

  function _norm(s) { return String(s == null ? '' : s).toLowerCase(); }

  // Autocomplete ranking for the `#` popover: case-insensitive over name AND id,
  // prefix matches before substring matches, each group alphabetical by name.
  // An empty query returns every project (the popover opens on a bare `#`).
  function rankProjects(query, projects) {
    var list = Array.isArray(projects) ? projects : [];
    var q = _norm(query);
    if (!q) return list.slice();
    var pre = [], sub = [];
    list.forEach(function (p) {
      var n = _norm(p && p.name), i = _norm(p && p.id);
      if (n.indexOf(q) === 0 || i.indexOf(q) === 0) pre.push(p);
      else if (n.indexOf(q) > 0 || i.indexOf(q) > 0) sub.push(p);
    });
    var byName = function (a, b) { return _norm(a.name) < _norm(b.name) ? -1 : _norm(a.name) > _norm(b.name) ? 1 : 0; };
    return pre.sort(byName).concat(sub.sort(byName));
  }

  // Submit-time project resolution — STRICTER than the popover ranking on
  // purpose: exact name/id wins, else a prefix match that is UNAMBIGUOUS (one
  // candidate). Ambiguous or unmatched → null → the token stays literal.
  // Substring matches never auto-resolve (that would be guessing).
  function resolveProject(text, projects) {
    var list = Array.isArray(projects) ? projects : [];
    var q = _norm(text);
    if (!q) return null;
    var exact = null;
    for (var i = 0; i < list.length; i++) {
      if (_norm(list[i].name) === q || _norm(list[i].id) === q) { exact = list[i]; break; }
    }
    if (exact) return exact;
    var hits = list.filter(function (p) {
      return _norm(p.name).indexOf(q) === 0 || _norm(p.id).indexOf(q) === 0;
    });
    return hits.length === 1 ? hits[0] : null;
  }

  // Classify one whitespace-delimited chunk. Returns a token or null (literal).
  function _classify(chunk, projects) {
    var sigil = chunk.charAt(0), rest = chunk.slice(1);
    if (!rest) return null;                                   // bare "#", "!", "@" → literal
    if (sigil === '#') {
      var proj = resolveProject(rest, projects);
      if (!proj) return null;                                 // never guess
      return { type: 'project', value: proj.id, name: proj.name, raw: chunk };
    }
    if (sigil === '!') {
      var m = /^p([123])$/i.exec(rest);
      if (!m) return null;
      return { type: 'priority', value: Number(m[1]), raw: chunk };
    }
    if (sigil === '@') {
      var w = _norm(rest);
      if (Object.prototype.hasOwnProperty.call(WHEN_WORDS, w)) {
        return { type: 'when', value: w, raw: chunk, field: WHEN_WORDS[w].field,
                 assignActiveCycle: WHEN_WORDS[w].assignActiveCycle };
      }
      if (_validIsoDate(rest)) {
        return { type: 'when', value: rest, raw: chunk, field: 'due_date',
                 assignActiveCycle: false, date: rest };
      }
      return null;                                            // invalid date / unknown word → literal
    }
    return null;
  }

  /*
   * parseQuickTitle(str, projects) → { title, tokens:[{type,value,raw,…}] }
   *
   * `projects` is [{id,name}, …] (the caller passes Object.values(PROJECTS_BY_ID)).
   * `title` is the input with every RESOLVED token stripped and whitespace
   * collapsed at the removal sites; unresolved tokens are left verbatim.
   */
  function parseQuickTitle(str, projects) {
    var s = String(str == null ? '' : str);
    // Chunk the string into [leading whitespace, non-space run] pairs so the
    // title can be rebuilt without a token's own separator surviving it.
    var parts = [], re = /(\s*)(\S+)/g, m;
    while ((m = re.exec(s)) !== null) parts.push({ ws: m[1], text: m[2], token: null });

    parts.forEach(function (p) { p.token = _classify(p.text, projects); });

    // Duplicate type → the LAST resolved token wins; earlier ones revert to
    // literal text (spec §4 precedence).
    var lastOf = {};
    parts.forEach(function (p, i) { if (p.token) lastOf[p.token.type] = i; });
    parts.forEach(function (p, i) { if (p.token && lastOf[p.token.type] !== i) p.token = null; });

    var tokens = [], title = '';
    parts.forEach(function (p) {
      if (p.token) { tokens.push(p.token); return; }           // drop chunk AND its separator
      title += p.ws + p.text;
    });
    return { title: title.trim(), tokens: tokens };
  }

  // Convenience: the token of a given type, or null. The composer's chip row and
  // submit path both read tokens this way (a type appears at most once).
  function tokenOf(tokens, type) {
    var found = null;
    (tokens || []).forEach(function (t) { if (t.type === type) found = t; });
    return found;
  }

  var api = {
    parseQuickTitle: parseQuickTitle,
    rankProjects: rankProjects,
    resolveProject: resolveProject,
    tokenOf: tokenOf,
    WHEN_WORDS: WHEN_WORDS,
  };
  root.QuickAddParser = api;
  root.__parseQuickTitle = parseQuickTitle;      // spec §4/§7 test hook
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);
