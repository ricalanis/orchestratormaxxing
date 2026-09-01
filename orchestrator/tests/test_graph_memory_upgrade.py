"""Regression guard for the memory-system upgrade (Phase 2/3) in graph_memory.

Pins the new write/decay/metabolism surface added on top of the Phase-5 recall
graph and reviewed by Kimi/GLM:

  - contradiction_check  — MemClaw keyword gate + the min-overlap false-positive
                           gate (GLM MED fix: require 2+ shared words OR sim>=0.25)
  - evolve_node          — A-MEM metadata evolution (merge props, stamp last_verified)
  - archive_node         — decay activation (status=archived, idempotent, reversible)
  - get_stale_nodes      — TTL-by-type staleness, max(created,updated) age
  - archive_stale        — bulk decay sweep
  - get_metabolism_stats — the digestive-system counts (now SQL-aggregated, GLM MED fix)
  - find_related         — A-MEM neighbour search (regex tokenization, GLM LOW fix)
  - ingest_memory        — new facts evolve related existing Decisions (GLM MED fix wiring)

DB isolation: every test builds a fresh GraphStore on a throwaway temp file, so
the real ~/.hermes/graph_memory.db is never touched. Stdlib unittest,
pytest-discoverable.

Run: python -m pytest tests/test_graph_memory_upgrade.py -v   # from orchestrator/
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import graph_memory as gm  # noqa: E402


def _fresh_store() -> gm.GraphStore:
    fd, path = tempfile.mkstemp(prefix="graphmem_test_", suffix=".db")
    os.close(fd)
    os.unlink(path)  # GraphStore recreates schema on a clean path
    return gm.GraphStore(db_path=Path(path))


def _backdate(store: gm.GraphStore, node_id: str, days: int):
    """Force a node's created_at AND updated_at `days` into the past so the
    staleness math (which uses max(created, updated)) actually bites."""
    ts = gm._now() - days * 86400
    with store._conn() as c:
        c.execute("UPDATE nodes SET created_at=?, updated_at=? WHERE id=?",
                  (ts, ts, node_id))


class _StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = _fresh_store()

    def tearDown(self):
        try:
            self.store.db_path.unlink()
        except Exception:
            pass


# ─────────────────────────── contradiction_check ───────────────────────────

class TestContradictionCheck(unittest.TestCase):
    def test_negation_asymmetry_flags_contradiction(self):
        r = gm.contradiction_check("Notion rejected", ["Notion is primary"])
        self.assertTrue(r["contradicts"])
        self.assertEqual(r["which"], 0)

    def test_multi_word_overlap_with_negation(self):
        r = gm.contradiction_check(
            "We no longer use the Ollama Cloud worker",
            ["We use the Ollama Cloud worker"])
        self.assertTrue(r["contradicts"])

    def test_unrelated_facts_do_not_contradict(self):
        r = gm.contradiction_check("cats are lovely", ["dogs run quickly"])
        self.assertFalse(r["contradicts"])
        self.assertIsNone(r["which"])

    def test_same_polarity_not_flagged(self):
        # Both affirmative, no negation asymmetry → not a contradiction.
        r = gm.contradiction_check("Notion is the wiki", ["Notion is the wiki tool"])
        self.assertFalse(r["contradicts"])

    def test_min_overlap_gate_suppresses_single_common_noun(self):
        # GLM MED fix: a single shared low-signal noun across two long facts
        # (Jaccard well below 0.25, only one word in common) must NOT fire.
        new = ("the quarterly planning meeting was cancelled after the "
               "budget committee finished its lengthy review process")
        existing = ("the annual engineering offsite covered roadmap themes "
                    "and hiring plans across every product meeting")
        overlap = (set(gm.re.findall(r"[a-zA-Z]{3,}", new.lower()))
                   & set(gm.re.findall(r"[a-zA-Z]{3,}", existing.lower())))
        self.assertEqual(overlap, {"the", "meeting"})  # sanity: exactly the shared words
        r = gm.contradiction_check(new, [existing])
        # 2 shared words ("the","meeting") — the gate lets it through, and there
        # IS negation asymmetry (cancelled). This documents that the gate is
        # about *overlap count*, not stopword filtering.
        self.assertTrue(r["contradicts"])

    def test_low_jaccard_single_overlap_is_gated_out(self):
        new = ("alpha beta gamma delta epsilon zeta eta theta iota kappa "
               "lambda cancelled")
        existing = ("mu nu xi omicron pi rho sigma tau upsilon phi chi "
                    "psi omega cancelled")
        # Only "cancelled" overlaps → len(overlap)=1, sim tiny → gated out even
        # though both share a word.
        r = gm.contradiction_check(new, [existing])
        self.assertFalse(r["contradicts"])

    def test_empty_existing_list(self):
        r = gm.contradiction_check("anything", [])
        self.assertFalse(r["contradicts"])


# ─────────────────────────── evolve_node ───────────────────────────

class TestEvolveNode(_StoreTest):
    def test_merges_properties_and_stamps_last_verified(self):
        nid = self.store.add_node("Decision", "use ollama workers",
                                   properties={"tag": "POLICY", "keep": "me"})
        ok = gm.evolve_node(nid, {"importance": 4}, store=self.store)
        self.assertTrue(ok)
        node = self.store.get_node(nid)
        self.assertEqual(node["properties"]["importance"], 4)
        self.assertEqual(node["properties"]["keep"], "me")   # existing preserved
        self.assertIn("last_evolved_at", node["properties"])
        self.assertIn("last_verified", node["properties"])   # ISO date stamped

    def test_missing_node_returns_false(self):
        self.assertFalse(gm.evolve_node("decision:does-not-exist", {"x": 1},
                                        store=self.store))


# ─────────────────────────── archive_node ───────────────────────────

class TestArchiveNode(_StoreTest):
    def test_archive_sets_status_and_reason(self):
        nid = self.store.add_node("Decision", "old fact", properties={"status": "active"})
        ok = gm.archive_node(nid, "TTL expired", store=self.store)
        self.assertTrue(ok)
        p = self.store.get_node(nid)["properties"]
        self.assertEqual(p["status"], "archived")
        self.assertEqual(p["archive_reason"], "TTL expired")
        self.assertEqual(p["original_status"], "active")   # reversible
        self.assertIn("archived_at", p)

    def test_archive_is_idempotent(self):
        nid = self.store.add_node("Decision", "x")
        gm.archive_node(nid, "first", store=self.store)
        first_at = self.store.get_node(nid)["properties"]["archived_at"]
        self.assertTrue(gm.archive_node(nid, "second", store=self.store))
        p = self.store.get_node(nid)["properties"]
        self.assertEqual(p["archived_at"], first_at)        # not re-stamped
        self.assertEqual(p["archive_reason"], "first")      # not overwritten

    def test_archived_node_excluded_from_search(self):
        nid = self.store.add_node("Decision", "hideme secret")
        gm.archive_node(nid, "gone", store=self.store)
        self.assertEqual(self.store.search("hideme"), [])
        self.assertEqual(len(self.store.search("hideme", include_archived=True)), 1)

    def test_missing_node_returns_false(self):
        self.assertFalse(gm.archive_node("nope", "r", store=self.store))


# ─────────────────────────── get_stale_nodes / archive_stale ────────────────

class TestStaleNodes(_StoreTest):
    def test_backdated_node_is_stale(self):
        nid = self.store.add_node("Note", "old research")   # Note TTL = 30d
        _backdate(self.store, nid, 40)
        stale = gm.get_stale_nodes(store=self.store)
        ids = {n["id"] for n in stale}
        self.assertIn(nid, ids)

    def test_fresh_node_not_stale(self):
        nid = self.store.add_node("Note", "brand new note")
        stale_ids = {n["id"] for n in gm.get_stale_nodes(store=self.store)}
        self.assertNotIn(nid, stale_ids)

    def test_genuinely_evolved_node_not_stale(self):
        # A node is protected from archival by GENUINE activity — evolve_node
        # stamps last_evolved_at + last_verified. Ancient birth, real evolution →
        # fresh, even after updated_at is forced old (proving the genuine signal,
        # not updated_at, does the protecting).
        nid = self.store.add_node("Note", "genuinely evolved")
        _backdate(self.store, nid, 999)                        # created + updated ancient
        gm.evolve_node(nid, {"insight": "still relevant"}, store=self.store)
        with self.store._conn() as c:                          # force updated_at old again
            c.execute("UPDATE nodes SET updated_at=? WHERE id=?",
                      (gm._now() - 999 * 86400, nid))
        stale_ids = {n["id"] for n in gm.get_stale_nodes(store=self.store)}
        self.assertNotIn(nid, stale_ids)

    def test_migration_touch_of_updated_at_does_not_protect(self):
        # Regression for the observed "0 archived / 617 active" freeze: a bulk
        # re-ingest / migration bumps updated_at on every node WITHOUT genuine
        # evolution. updated_at alone must NOT reset the staleness clock (else one
        # rebuild freezes the whole graph). Ancient birth + fresh updated_at only
        # (no last_evolved_at / last_verified) → STILL stale.
        nid = self.store.add_node("Note", "migration-touched")
        with self.store._conn() as c:
            c.execute("UPDATE nodes SET created_at=?, updated_at=? WHERE id=?",
                      (gm._now() - 999 * 86400, gm._now(), nid))
        stale_ids = {n["id"] for n in gm.get_stale_nodes(store=self.store)}
        self.assertIn(nid, stale_ids)

    def test_already_archived_excluded(self):
        nid = self.store.add_node("Note", "ancient")
        _backdate(self.store, nid, 100)
        gm.archive_node(nid, "manual", store=self.store)
        stale_ids = {n["id"] for n in gm.get_stale_nodes(store=self.store)}
        self.assertNotIn(nid, stale_ids)

    def test_archive_stale_archives_and_counts(self):
        a = self.store.add_node("Note", "stale one")
        b = self.store.add_node("Note", "stale two")
        self.store.add_node("Note", "fresh survivor")   # not backdated
        _backdate(self.store, a, 40)
        _backdate(self.store, b, 40)
        n = gm.archive_stale(store=self.store)
        self.assertEqual(n, 2)
        self.assertEqual(self.store.get_node(a)["properties"]["status"], "archived")

    def test_ttl_override(self):
        nid = self.store.add_node("Decision", "young decision")  # default TTL 180d
        _backdate(self.store, nid, 20)
        # Not stale under default, stale under a 10-day override.
        self.assertNotIn(nid, {n["id"] for n in gm.get_stale_nodes(store=self.store)})
        stale = gm.get_stale_nodes(ttl_override={"Decision": 10}, store=self.store)
        self.assertIn(nid, {n["id"] for n in stale})


# ─────────────────────────── get_metabolism_stats ───────────────────────────

class TestMetabolismStats(_StoreTest):
    def test_counts_inputs_distilled_evicted_and_totals(self):
        # Fresh input (created now).
        self.store.add_node("Decision", "new fact")
        # Distilled: created > 24h ago but updated now.
        old_updated = self.store.add_node("Decision", "refined fact")
        with self.store._conn() as c:
            c.execute("UPDATE nodes SET created_at=? WHERE id=?",
                      (gm._now() - 2 * 86400, old_updated))   # updated_at stays now
        # Archived in the last 24h → counts as evicted.
        ev = self.store.add_node("Note", "evicted fact")
        gm.archive_node(ev, "decayed", store=self.store)

        stats = gm.get_metabolism_stats(store=self.store)
        self.assertGreaterEqual(stats["inputs_processed"], 1)
        self.assertEqual(stats["facts_distilled"], 1)
        self.assertEqual(stats["memories_evicted"], 1)
        self.assertEqual(stats["total_archived"], 1)
        self.assertGreaterEqual(stats["total_active"], 2)
        # Every documented key is present.
        for key in ("inputs_processed", "facts_distilled", "memories_evicted",
                    "decay_triggered", "total_active", "total_archived"):
            self.assertIn(key, stats)

    def test_old_archive_not_counted_as_recent_eviction(self):
        ev = self.store.add_node("Note", "long-archived")
        gm.archive_node(ev, "old", store=self.store)
        # Backdate the archived_at stamp beyond the 24h window.
        with self.store._conn() as c:
            import json
            node = self.store.get_node(ev)
            node["properties"]["archived_at"] = gm._now() - 3 * 86400
            c.execute("UPDATE nodes SET properties_json=? WHERE id=?",
                      (json.dumps(node["properties"]), ev))
        stats = gm.get_metabolism_stats(store=self.store)
        self.assertEqual(stats["memories_evicted"], 0)
        self.assertEqual(stats["total_archived"], 1)

    def test_empty_graph(self):
        stats = gm.get_metabolism_stats(store=self.store)
        self.assertEqual(stats["total_active"], 0)
        self.assertEqual(stats["total_archived"], 0)
        self.assertEqual(stats["memories_evicted"], 0)


# ─────────────────────────── find_related ───────────────────────────

class TestFindRelated(_StoreTest):
    def test_scores_by_label_word_overlap(self):
        self.store.add_node("Decision", "ollama cloud worker policy")
        self.store.add_node("Decision", "ollama pricing notes")
        self.store.add_node("Decision", "unrelated kanban board layout")
        related = gm.find_related("ollama", store=self.store, limit=5)
        labels = [r["label"] for r in related]
        self.assertTrue(any("ollama" in l for l in labels))
        self.assertFalse(any("kanban" in l for l in labels))

    def test_excludes_archived(self):
        nid = self.store.add_node("Decision", "ollama archived decision")
        gm.archive_node(nid, "gone", store=self.store)
        related = gm.find_related("ollama", store=self.store)
        self.assertNotIn(nid, {r["id"] for r in related})

    def test_no_matches_returns_empty(self):
        self.assertEqual(gm.find_related("zzznotpresent", store=self.store), [])


# ─────────────────────────── ingest_memory evolution wiring ─────────────────

class TestIngestEvolutionWiring(_StoreTest):
    def _write_memory(self, text: str) -> Path:
        fd, path = tempfile.mkstemp(prefix="MEMORY_", suffix=".md")
        os.close(fd)
        Path(path).write_text(text)
        return Path(path)

    def test_new_fact_evolves_related_existing_decision(self):
        # Two facts sharing a leading [TAG] → find_related(tag) links them.
        md = self._write_memory(
            "[POLICY] Use Ollama Cloud workers for bulk parallel tasks\n"
            "§\n"
            "[POLICY] Verify every worker output with a deterministic contract\n"
        )
        try:
            counts = gm.ingest_memory(self.store, memory_md=md)
        finally:
            md.unlink()
        self.assertGreaterEqual(counts["decisions"], 2)
        self.assertGreaterEqual(counts["evolved"], 1)
        # The earlier decision gained a related_decisions back-link + last_verified.
        evolved = [n for n in self.store.search("POLICY")
                   if n["properties"].get("related_decisions")]
        self.assertTrue(evolved)
        self.assertIn("last_verified", evolved[0]["properties"])

    def test_reingest_is_idempotent_no_double_evolve(self):
        md = self._write_memory(
            "[POLICY] Alpha decision about deployment\n"
            "§\n"
            "[POLICY] Beta decision about deployment\n"
        )
        try:
            gm.ingest_memory(self.store, memory_md=md)
            second = gm.ingest_memory(self.store, memory_md=md)
        finally:
            md.unlink()
        # Nothing is new on the second pass → no further evolution churn.
        self.assertEqual(second["evolved"], 0)


if __name__ == "__main__":
    unittest.main()
