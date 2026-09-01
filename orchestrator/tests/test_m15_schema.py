"""m15 schema contracts — the constraints that carry the design's invariants.

These assert the properties a later refactor could quietly drop, each of which
was a real finding during design review:

  * a nullable enum written `IN (NULL, ...)` accepts ANY value in SQLite
    (unmatched → NULL, and CHECK passes on NULL), so every nullable enum here
    must be spelled `IS NULL OR IN (...)`;
  * `UNIQUE(objective_id, kind)` is what makes suggestion dedup structural
    rather than a content-hash heuristic;
  * `digest_status` must be able to express `failed`/`dead_letter`, or a
    malformed digestion has nowhere to go but `digested` — losing the event.
"""
import sqlite3
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dashboard.migrations import m15_differential_capture as m15  # noqa: E402

NOW = 1785800000


def fresh():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    m15.apply(conn)
    return conn


def seed_event(conn, event_id="ev_1", **kw):
    row = dict(source_kind="fireflies", source_ref="tr_1", captured_at=NOW)
    row.update(kw)
    cols = ", ".join(["event_id"] + list(row))
    ph = ", ".join("?" * (len(row) + 1))
    conn.execute(f"INSERT INTO capture_events ({cols}) VALUES ({ph})",
                 [event_id] + list(row.values()))
    return event_id


def seed_objective(conn, oid="obj_1", **kw):
    row = dict(title="Enviar cotización", opened_at=NOW, updated_at=NOW)
    row.update(kw)
    cols = ", ".join(["id"] + list(row))
    ph = ", ".join("?" * (len(row) + 1))
    conn.execute(f"INSERT INTO objectives ({cols}) VALUES ({ph})", [oid] + list(row.values()))
    return oid


class Idempotency(unittest.TestCase):
    def test_apply_is_reentrant(self):
        conn = fresh()
        m15.apply(conn)  # must not raise
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("capture_events", "capture_watermarks", "objectives", "objective_evidence",
                  "entity_state", "state_ops", "suggestions"):
            self.assertIn(t, tables)

    def test_event_id_is_the_dedup_key(self):
        conn = fresh()
        seed_event(conn)
        with self.assertRaises(sqlite3.IntegrityError):
            seed_event(conn)
        conn.execute("INSERT OR IGNORE INTO capture_events (event_id, source_kind, source_ref, "
                     "captured_at) VALUES ('ev_1','fireflies','tr_1',?)", (NOW,))
        self.assertEqual(conn.execute("SELECT count(*) FROM capture_events").fetchone()[0], 1)


class NullableEnumsRejectGarbage(unittest.TestCase):
    """The `IS NULL OR IN (...)` spelling — the whole point of Enmienda C.1."""

    def test_entity_kind_rejects_unknown_but_allows_null(self):
        conn = fresh()
        seed_event(conn, "ev_null", entity_kind=None)  # NULL is legal
        with self.assertRaises(sqlite3.IntegrityError):
            seed_event(conn, "ev_bad", entity_kind="contact")

    def test_digest_status_rejects_unknown(self):
        conn = fresh()
        with self.assertRaises(sqlite3.IntegrityError):
            seed_event(conn, "ev_bad", digest_status="done")

    def test_dead_letter_and_failed_are_expressible(self):
        # Without these an undigestible event can only be marked 'digested'.
        conn = fresh()
        for i, st in enumerate(("failed", "dead_letter")):
            seed_event(conn, f"ev_{st}", digest_status=st, attempts=i + 1)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM capture_events WHERE digest_status IN ('failed','dead_letter')"
        ).fetchone()[0], 2)

    def test_gist_is_length_bounded(self):
        conn = fresh()
        conn.execute("INSERT INTO entity_state (entity_kind, entity_id, gist, updated_at) "
                     "VALUES ('deal','d1',?,?)", ("x" * 700, NOW))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO entity_state (entity_kind, entity_id, gist, updated_at) "
                         "VALUES ('deal','d2',?,?)", ("x" * 701, NOW))

    def test_objective_status_and_op_verdict_vocabularies(self):
        conn = fresh()
        with self.assertRaises(sqlite3.IntegrityError):
            seed_objective(conn, "obj_bad", status="closed")
        seed_event(conn)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO state_ops (event_id, op_index, op, verdict, created_at) "
                         "VALUES ('ev_1',0,'objective.add','ok',?)", (NOW,))


class StructuralDedup(unittest.TestCase):
    def test_one_suggestion_per_objective_and_kind(self):
        conn = fresh()
        seed_objective(conn)
        args = ("obj_1", "create_task", "Enviar cotización", NOW, NOW)
        conn.execute("INSERT INTO suggestions (id, objective_id, kind, title, created_at, "
                     "updated_at) VALUES ('sug_a',?,?,?,?,?)", args)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO suggestions (id, objective_id, kind, title, created_at, "
                         "updated_at) VALUES ('sug_b',?,?,?,?,?)", args)
        # A different transition on the same objective is legitimately distinct.
        conn.execute("INSERT INTO suggestions (id, objective_id, kind, title, created_at, "
                     "updated_at) VALUES ('sug_c','obj_1','close_task','Cerrar',?,?)", (NOW, NOW))
        self.assertEqual(conn.execute("SELECT count(*) FROM suggestions").fetchone()[0], 2)

    def test_op_ledger_is_replay_safe(self):
        conn = fresh()
        seed_event(conn)
        conn.execute("INSERT INTO state_ops (event_id, op_index, op, verdict, created_at) "
                     "VALUES ('ev_1',0,'objective.add','applied',?)", (NOW,))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO state_ops (event_id, op_index, op, verdict, created_at) "
                         "VALUES ('ev_1',0,'objective.add','applied',?)", (NOW,))


class ProvenanceSurvivesPurge(unittest.TestCase):
    def test_quote_is_copied_not_referenced(self):
        """E0 purge nulls capture_events.payload; the citation must still resolve."""
        conn = fresh()
        seed_event(conn, payload='{"sentences":[{"text":"te mando la cotización"}]}')
        seed_objective(conn)
        conn.execute("INSERT INTO objective_evidence (objective_id, event_id, anchor, quote, "
                     "created_at) VALUES ('obj_1','ev_1','12','te mando la cotización',?)", (NOW,))
        conn.execute("UPDATE capture_events SET payload = NULL, payload_purged_at = ? WHERE event_id='ev_1'",
                     (NOW,))
        quote = conn.execute("SELECT quote FROM objective_evidence WHERE objective_id='obj_1'"
                             ).fetchone()[0]
        self.assertEqual(quote, "te mando la cotización")

    def test_evidence_is_unique_per_anchor(self):
        conn = fresh()
        seed_event(conn)
        seed_objective(conn)
        args = ("obj_1", "ev_1", "12", "cita", NOW)
        conn.execute("INSERT INTO objective_evidence (objective_id, event_id, anchor, quote, "
                     "created_at) VALUES (?,?,?,?,?)", args)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO objective_evidence (objective_id, event_id, anchor, quote, "
                         "created_at) VALUES (?,?,?,?,?)", args)


class ReferentialIntegrity(unittest.TestCase):
    def test_suggestion_requires_a_real_objective(self):
        conn = fresh()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO suggestions (id, objective_id, kind, title, created_at, "
                         "updated_at) VALUES ('sug_x','obj_ghost','create_task','t',?,?)",
                         (NOW, NOW))

    def test_supersede_chain_is_a_real_link(self):
        conn = fresh()
        seed_objective(conn, "obj_new")
        seed_objective(conn, "obj_old", status="superseded", superseded_by="obj_new")
        self.assertEqual(conn.execute(
            "SELECT superseded_by FROM objectives WHERE id='obj_old'").fetchone()[0], "obj_new")
        with self.assertRaises(sqlite3.IntegrityError):
            seed_objective(conn, "obj_x", superseded_by="obj_ghost")


class ConcurrencyFields(unittest.TestCase):
    def test_objective_carries_a_version_for_compare_and_swap(self):
        conn = fresh()
        seed_objective(conn)
        v = conn.execute("SELECT version FROM objectives WHERE id='obj_1'").fetchone()[0]
        self.assertEqual(v, 1)
        n = conn.execute("UPDATE objectives SET title='x', version = version + 1 "
                         "WHERE id='obj_1' AND version = 1").rowcount
        self.assertEqual(n, 1)
        stale = conn.execute("UPDATE objectives SET title='y', version = version + 1 "
                             "WHERE id='obj_1' AND version = 1").rowcount
        self.assertEqual(stale, 0, "a stale-version write must not land")

    def test_watermark_is_composite(self):
        cols = {r[1] for r in fresh().execute("PRAGMA table_info(capture_watermarks)")}
        self.assertIn("last_seen_ts", cols)
        self.assertIn("last_seen_id", cols)


if __name__ == "__main__":
    unittest.main()
