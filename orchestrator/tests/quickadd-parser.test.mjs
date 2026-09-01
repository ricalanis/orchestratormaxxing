/*
 * Unit tests for the Quick-Add token grammar (dashboard/static/quickadd-parser.js).
 * The grammar IS the contract the composer, the modal escalation and the submit
 * PATCH shapes are all built on, so it is table-driven and covers every rule in
 * the spec's §4 (and every case listed in §7).
 *
 * Pure logic — no DOM, no jsdom. Stdlib node:test runner. Run:
 *   node --test tests/quickadd-parser.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const require = createRequire(import.meta.url);
const dir = path.dirname(fileURLToPath(import.meta.url));
const qa = require(path.join(dir, '..', 'dashboard', 'static', 'quickadd-parser.js'));
const { parseQuickTitle, rankProjects, resolveProject, tokenOf } = qa;

// Stand-in for Object.values(PROJECTS_BY_ID). `hermes`/`hermes-docs` share a
// prefix on purpose so the ambiguity rule is exercised.
const PROJECTS = [
  { id: 'p_hermes', name: 'hermes' },
  { id: 'p_hdocs', name: 'hermes-docs' },
  { id: 'p_icalia', name: 'Icalia' },
  { id: 'p_inbox', name: 'Inbox' },
];
const parse = (s) => parseQuickTitle(s, PROJECTS);

// ---- spec §7: the five listed parser cases -------------------------------
test('§7.1 "Fix login #hermes !p1" → title stripped, project + priority tokens', () => {
  const r = parse('Fix login #hermes !p1');
  assert.equal(r.title, 'Fix login');
  assert.equal(r.tokens.length, 2);
  assert.deepEqual(tokenOf(r.tokens, 'project'), { type: 'project', value: 'p_hermes', name: 'hermes', raw: '#hermes' });
  assert.deepEqual(tokenOf(r.tokens, 'priority'), { type: 'priority', value: 1, raw: '!p1' });
});

test('§7.2 "fix p1 incident" → NO priority token (the ! sigil is required)', () => {
  const r = parse('fix p1 incident');
  assert.equal(r.title, 'fix p1 incident');
  assert.deepEqual(r.tokens, []);
});

test('§7.3 "@nextweek" → scheduled_week shape, "@2026-08-01" → due_date shape', () => {
  const a = parse('ship it @nextweek');
  assert.equal(a.title, 'ship it');
  assert.equal(tokenOf(a.tokens, 'when').field, 'scheduled_week');
  assert.equal(tokenOf(a.tokens, 'when').value, 'nextweek');
  assert.equal(tokenOf(a.tokens, 'when').assignActiveCycle, false);

  const b = parse('ship it @2026-08-01');
  assert.equal(b.title, 'ship it');
  assert.equal(tokenOf(b.tokens, 'when').field, 'due_date');
  assert.equal(tokenOf(b.tokens, 'when').date, '2026-08-01');
});

test('§7.4 "#nonexistent do thing" → the token stays literal in the title', () => {
  const r = parse('#nonexistent do thing');
  assert.equal(r.title, '#nonexistent do thing');
  assert.deepEqual(r.tokens, []);
});

test('§7.5 duplicate "!p1 … !p2" → priority 2; the earlier !p1 reverts to literal', () => {
  const r = parse('Fix !p1 the thing !p2');
  assert.equal(r.title, 'Fix !p1 the thing');
  assert.equal(r.tokens.length, 1);
  assert.equal(tokenOf(r.tokens, 'priority').value, 2);
});

// ---- @when: the closed vocabulary ----------------------------------------
test('@thisweek carries assignActiveCycle (the {scheduled_week, assign_active_cycle} PATCH)', () => {
  const t = tokenOf(parse('do it @thisweek').tokens, 'when');
  assert.equal(t.field, 'scheduled_week');
  assert.equal(t.assignActiveCycle, true);
});

test('@someday has no PATCH field (stays in the backlog)', () => {
  const t = tokenOf(parse('later @someday').tokens, 'when');
  assert.equal(t.field, null);
});

test('the @when vocabulary is CLOSED — weekdays / free text / recurrence stay literal', () => {
  for (const s of ['@tuesday', '@tomorrow', '@next-week', '@weekly', '@in3days', '@aug1']) {
    const r = parse('do it ' + s);
    assert.deepEqual(r.tokens, [], s + ' must not parse');
    assert.equal(r.title, 'do it ' + s);
  }
});

test('@date validation: shape and calendar validity both required', () => {
  assert.equal(tokenOf(parse('x @2026-08-01').tokens, 'when').value, '2026-08-01');
  assert.equal(tokenOf(parse('x @2028-02-29').tokens, 'when').value, '2028-02-29');   // leap year
  for (const bad of ['@2026-13-01', '@2026-02-31', '@2026-02-29', '@2026-8-1', '@26-08-01', '@2026-08-01x']) {
    assert.deepEqual(parse('x ' + bad).tokens, [], bad + ' must not parse');
  }
});

// ---- word boundaries + token position ------------------------------------
test('tokens match only at word boundaries — glued sigils stay literal', () => {
  for (const s of ['fix foo#hermes now', 'fix a!p1 now', 'mail me@thisweek now']) {
    assert.deepEqual(parse(s).tokens, [], s);
    assert.equal(parse(s).title, s);
  }
});

test('a token parses at the start, middle and end of the string', () => {
  assert.equal(parse('#hermes fix login').title, 'fix login');
  assert.equal(parse('fix #hermes login').title, 'fix login');
  assert.equal(parse('fix login #hermes').title, 'fix login');
  for (const s of ['#hermes fix login', 'fix #hermes login', 'fix login #hermes']) {
    assert.equal(tokenOf(parse(s).tokens, 'project').value, 'p_hermes');
  }
});

test('a stripped token leaves no double space and no leading/trailing space', () => {
  const r = parse('  Fix   #hermes   login  !p3  ');
  assert.equal(r.title, 'Fix   login');            // interior runs are preserved, the token gap is not
  assert.equal(r.tokens.length, 2);
});

// ---- project resolution: never guess -------------------------------------
test('# resolves an unambiguous prefix (autocomplete-style partial)', () => {
  assert.equal(tokenOf(parse('note #ica').tokens, 'project').value, 'p_icalia');
  assert.equal(tokenOf(parse('note #hermes-d').tokens, 'project').value, 'p_hdocs');
});

test('# resolution is case-insensitive and matches ids as well as names', () => {
  assert.equal(tokenOf(parse('note #ICALIA').tokens, 'project').value, 'p_icalia');
  assert.equal(tokenOf(parse('note #p_hdocs').tokens, 'project').value, 'p_hdocs');
});

test('an EXACT name beats an ambiguous prefix ("#hermes" with hermes-docs present)', () => {
  assert.equal(tokenOf(parse('note #hermes').tokens, 'project').value, 'p_hermes');
});

test('an AMBIGUOUS prefix never guesses — the token stays literal', () => {
  const r = parse('note #herm');                   // hermes + hermes-docs
  assert.deepEqual(r.tokens, []);
  assert.equal(r.title, 'note #herm');
});

test('bare sigils are literal, not tokens', () => {
  const r = parse('a # b ! c @ d');
  assert.deepEqual(r.tokens, []);
  assert.equal(r.title, 'a # b ! c @ d');
});

test('an empty project list resolves nothing (never guess with no data)', () => {
  const r = parseQuickTitle('fix #hermes', []);
  assert.deepEqual(r.tokens, []);
  assert.equal(r.title, 'fix #hermes');
});

// ---- priority ------------------------------------------------------------
test('!p1/!p2/!p3 only — !p0, !p4, !pX and bare p1 do not parse', () => {
  assert.equal(tokenOf(parse('a !p1').tokens, 'priority').value, 1);
  assert.equal(tokenOf(parse('a !p2').tokens, 'priority').value, 2);
  assert.equal(tokenOf(parse('a !p3').tokens, 'priority').value, 3);
  for (const bad of ['!p0', '!p4', '!p', '!pX', '!priority1', 'p1']) {
    assert.deepEqual(parse('a ' + bad).tokens, [], bad + ' must not parse');
  }
});

// ---- last-token-wins, per type -------------------------------------------
test('duplicate project: the last resolved one wins, the earlier reverts to literal', () => {
  const r = parse('note #hermes and #Icalia');
  assert.equal(tokenOf(r.tokens, 'project').value, 'p_icalia');
  assert.equal(r.title, 'note #hermes and');
});

test('duplicate when: the last one wins', () => {
  const r = parse('x @thisweek y @someday');
  assert.equal(r.tokens.length, 1);
  assert.equal(tokenOf(r.tokens, 'when').value, 'someday');
  assert.equal(r.title, 'x @thisweek y');
});

test('last-token-wins is PER TYPE — one of each survives together', () => {
  const r = parse('Fix login #hermes !p3 @nextweek');
  assert.equal(r.title, 'Fix login');
  assert.equal(r.tokens.length, 3);
  assert.equal(tokenOf(r.tokens, 'project').value, 'p_hermes');
  assert.equal(tokenOf(r.tokens, 'priority').value, 3);
  assert.equal(tokenOf(r.tokens, 'when').value, 'nextweek');
});

test('an UNRESOLVED duplicate does not displace a resolved earlier one', () => {
  const r = parse('note #hermes and #nope');
  assert.equal(tokenOf(r.tokens, 'project').value, 'p_hermes');
  assert.equal(r.title, 'note and #nope');
});

// ---- degenerate input ----------------------------------------------------
test('title is empty once tokens are stripped (the composer must refuse to submit)', () => {
  const r = parse('#hermes !p1 @thisweek');
  assert.equal(r.title, '');
  assert.equal(r.tokens.length, 3);
});

test('empty / whitespace / null input is safe', () => {
  for (const s of ['', '   ', null, undefined]) {
    const r = parseQuickTitle(s, PROJECTS);
    assert.equal(r.title, '');
    assert.deepEqual(r.tokens, []);
  }
});

test('raw is retained on every token (un-parse restores the exact text)', () => {
  const r = parse('Fix #ica !p2 @2026-08-01');
  assert.deepEqual(r.tokens.map(t => t.raw), ['#ica', '!p2', '@2026-08-01']);
});

// ---- popover ranking (autocomplete) --------------------------------------
test('rankProjects: prefix matches rank before substring matches', () => {
  const ranked = rankProjects('herm', PROJECTS).map(p => p.id);
  assert.deepEqual(ranked, ['p_hermes', 'p_hdocs']);
  const sub = rankProjects('docs', PROJECTS).map(p => p.id);
  assert.deepEqual(sub, ['p_hdocs']);
});

test('rankProjects: empty query lists everything; unknown query lists nothing', () => {
  assert.equal(rankProjects('', PROJECTS).length, PROJECTS.length);
  assert.deepEqual(rankProjects('zzz', PROJECTS), []);
});

test('resolveProject is stricter than rankProjects — substring never auto-resolves', () => {
  assert.equal(rankProjects('docs', PROJECTS).length, 1);
  assert.equal(resolveProject('docs', PROJECTS), null);
});
