# MEMORY.md governance upgrade — spec (2026-06-06)

Upgrades this project's shared Claude/Codex memory from **append-only** (which the research showed
*degrades*: stale notes outrank current ones) to a **governed, self-pruning** layer — with
zero new infra. It's frontmatter + four rules Claude follows on write, enforced by a
deterministic audit script.

Memory lives at `<git-root>/.agents/memory/` (`MEMORY.md` index + one file per fact). Claude's
machine-specific auto-memory directory is a reversible symlink to it; Codex receives the same
bounded index through plugin hooks and uses `memoryctl` for writes. Codex's private SQLite is
never scraped or rewritten. The format remains compatible with Claude auto-memory.

## Why (papers → mechanism)
| Source | Lesson | What we adopt |
|---|---|---|
| Zep (2501.13956) | Facts carry validity; new facts **supersede**, don't stack | `status` + `superseded_by`; update-don't-append |
| SSGM (2603.11768) | Memory needs TTL/decay + contradiction gate + review | per-type TTL; critic-as-write-gate on conflicts |
| ByteRover (2604.01599) | Hierarchical, just-in-time retrieval | keep index 1-line/fact; load full files only when relevant |
| GraphRAG (2404.16130) | Summaries for the human layer only | periodic consolidation pass; never reason over the summary |
| AST-graph (2601.08773) | Ground truth is **computed, not LLM-guessed** | `mem-audit` computes staleness/drift from dates+FS, not vibes |
| TOKI (2606.06240) | Contradiction = typed **bitemporal** resolution (valid-time vs transaction-time; 4 operators; loser kept as audit row) | `valid_from`/`valid_to` clocks + named `resolution_rule`; `mem-audit` decays off whichever clock bites first |
| RBI-Eval (2606.06055) | Agents over-surface **sensitive** memory (+51–83%) even when unneeded; agent-judgment is the failure point | **secrets → never store** (`mem-audit` errors; removal, not a label); **PII → mark `sensitivity: sensitive`** + least-disclosure (soft generation-time control, not a guarantee) |

## Frontmatter schema (additions in **bold**)
```yaml
---
name: <kebab-slug>
description: <one-line, used for recall>
metadata:
  node_type: memory
  type: user | feedback | project | reference
  originSessionId: <id>
  created: 2026-06-06            # bold: date first written (YYYY-MM-DD) — transaction time
  last_verified: 2026-06-06      # bold: last time re-checked true; bump on re-verify — transaction time
  valid_from: 2026-06-06         # bitemporal (TOKI): when the fact became true in the world
  valid_to:                      # bitemporal (TOKI): when it stops being true (optional; set on known expiry)
  status: active                 # bold: active | superseded | stale
  superseded_by: <name>          # bold: required when status=superseded
  resolution_rule:               # TOKI operator used on supersede: last-writer-wins | evidence-merge | await-confirm | per-rule
  confidence: high | medium | low # bold
  sensitivity: normal | sensitive # least-disclosure (2606.06055): sensitive → stay silent at recall unless task needs it
  source: <where it came from>   # bold: provenance for re-verification
---
```
Old files without the new fields still work; `mem-audit` flags them as "missing dates" (warn,
not error) so they get backfilled opportunistically.

## The four rules (what Claude does on every memory write)
1. **Supersede, don't append (Zep).** Before writing, check for an existing fact on the same
   subject. If the new fact *changes* the old one: set the old file's `status: superseded` +
   `superseded_by: <new-name>`, remove its `MEMORY.md` pointer, write the new file. Never leave
   two contradictory active facts. (This is the core anti-drift rule.)
2. **Stamp + decay (SSGM/ByteRover).** Every write sets `created`/`last_verified`. TTL by type:
   `reference` 30d · `project` 14d · `feedback` 180d · `user` 365d. Past TTL → **stale, not
   deleted**: re-verify (re-run the source) before trusting; on confirm, bump `last_verified`.
   This rides the harness's existing "this memory is N days old" reminder rather than fighting it.
3. **Critic-as-write-gate, but only when it earns its cost (SSGM + our iron rule).** Routine new
   facts: just write. **Gate only the high-stakes / belief-changing writes** — i.e. a supersession
   candidate or a `project`/`reference` fact that contradicts an active one. Then spawn one
   `ollama-worker` as critic ("does this contradict [existing fact]? cite the conflict"). Agree →
   write; conflict → escalate to Opus. Cost scales with stakes, not with every trivial note.
4. **Index stays thin; consolidate at the cap (ByteRover/GraphRAG).** `MEMORY.md` = one line per
   *active* fact, never fact bodies. At >25 active memories, run a consolidation pass: merge
   near-duplicates, supersede dead ones. If the pass finds no safe semantic merge, record that
   exact reviewed set with `memoryctl review-consolidation --decision no-safe-merge`. The private
   receipt contains only a count, timestamp, and semantic-set hash; any active claim or membership
   change invalidates it and reopens the advisory. Reverification and index ordering do not.
   Summaries are navigational only — reasoning always uses the underlying fact files.

## Mutation + enforcement — `bin/memoryctl` and `bin/mem-audit`

`memoryctl` owns locked, atomic writes and index rebuilds. It rejects credential patterns and
unmarked PII, makes import/bridging dry-run by default, preserves the original Claude directory
as `memory.pre-shared.<timestamp>`, and keeps superseded facts as linked audit rows.

`mem-audit` is deterministic and read-only:
Computes from dates + filesystem (never asks an LLM what's stale):
- STALE active facts — bitemporal: past `valid_to` (EXPIRED) **or** past their type TTL, whichever bites first
- superseded facts still living in the index (error)
- superseded facts missing / with an invalid `resolution_rule` (TOKI operator)
- broken supersede links: `superseded_by`→unknown target (error), or missing `supersedes` back-link (warn)
- index pointers to missing files / active files missing a pointer
- missing `created`/`last_verified` dates
- stored credentials: known formats incl. JWT (**error** — remove, no `sensitivity` bypass); high-entropy blobs (**warn** — possible unpatterned secret). Best-effort backstop; the authoritative control is the write-time "never store credentials" rule, not the regex.
- unmarked PII bodies (**warn** — mark `sensitivity: sensitive`, least-disclosure). Nothing echoes the match.
- index over the consolidation cap unless its exact active semantic set has a current
  `no-safe-merge` review receipt; the receipt never suppresses stale, malformed, or index findings

`bin/mem-audit` (exit 1 on errors) → human reads the flags and fixes. Run it at session start,
or wire a SessionStart/Stop hook. It is the "deterministic ground truth" arm of rule-set above;
Claude acts on its output, Claude does not re-derive staleness by reading every file.

## Rollout
1. ✅ `bin/memoryctl` + shared resolver added; ✅ existing facts imported without findings.
2. ✅ Claude legacy directory backed up and bridged; ✅ Codex/Claude SessionStart load the same bounded brief.
3. ✅ `mem-audit` resolves the canonical store; ✅ harness black-box tests cover migration, locking, supersession, and dirty-pull refusal.
4. Cross-machine transport is the private Git remote. No memory HTTP service or second scheduler.

## Operational block for CLAUDE.md (the part Claude actually follows)
> ### Memory governance (see knowledge/memory-protocol-upgrade.md)
> When writing project memory: **supersede, don't append** — if a new fact changes an existing
> one, mark the old `status: superseded` + `superseded_by`, drop its index line, write the new.
> Stamp `created`/`last_verified`; treat facts past TTL (reference 30d/project 14d) as **stale →
> re-verify before trusting**. For belief-changing or high-stakes writes only, run one
> `ollama-worker` as a contradiction critic before committing. Keep `MEMORY.md` one line per
> active fact. Run `bin/mem-audit` at session start; act on its flags (don't re-derive staleness).

## What this deliberately does NOT do
- No vector DB / embeddings (zero new infra — the research said files win on readability/git/latency).
- No LLM-generated knowledge graph of the notes (that's the *degrading* tier).
- No auto-delete (decay flags for review; deletion stays a human/Opus decision).
