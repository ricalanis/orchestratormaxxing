"""Contract for the versioned commercial-proposal ledger."""
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from dashboard import commercial_proposals as proposals
from dashboard import attachments, context, db, sprints
from dashboard.migrations import runner
from dashboard.api import app


class CommercialProposalContract(unittest.TestCase):
    def setUp(self):
        source = Path(os.environ["HERMES_KANBAN_DB"])
        fd, name = tempfile.mkstemp(prefix="proposal-ledger-", suffix=".db")
        os.close(fd)
        shutil.copy(source, name)
        self.path = Path(name)
        self.old = db.KANBAN_DB, sprints.KANBAN_DB, runner.run_backup
        db.KANBAN_DB = sprints.KANBAN_DB = self.path
        runner.run_backup = lambda: None
        runner.run()
        conn = sqlite3.connect(self.path)
        now = int(time.time())
        conn.execute("INSERT OR IGNORE INTO accounts(id,name,created_at) VALUES('acct_prop','Proposal client',?)", (now,))
        conn.execute("INSERT OR REPLACE INTO deals(id,account_id,title,stage,value,currency,created_at,updated_at) VALUES('deal_prop','acct_prop','Proposal deal','proposal',75000,'MXN',?,?)", (now, now))
        conn.commit()
        conn.close()

    def tearDown(self):
        db.KANBAN_DB, sprints.KANBAN_DB, runner.run_backup = self.old
        self.path.unlink(missing_ok=True)

    def test_register_is_idempotent_but_revision_identity_cannot_change(self):
        first = proposals.register_packet("deal_prop", 1, "/tmp/work", "proposal-workspace.json", "send/proposal.pdf")
        self.assertEqual((first["status"], first["created"]), ("ok", True))
        again = proposals.register_packet("deal_prop", 1, "/tmp/work", "proposal-workspace.json", "send/proposal.pdf")
        self.assertEqual((again["status"], again["created"]), ("exists", False))
        conflict = proposals.register_packet("deal_prop", 1, "/tmp/other", "proposal-workspace.json", "send/proposal.pdf")
        self.assertEqual(conflict["code"], "revision_conflict")

    def test_registration_refusals_are_typed_and_numeric_text_is_normalized(self):
        self.assertEqual(proposals.register_packet("deal_prop", None, "/w", "m", "p")["code"],
                         "invalid_revision")
        self.assertEqual(proposals.register_packet("deal_prop", 0, "/w", "m", "p")["code"],
                         "invalid_revision")
        self.assertEqual(proposals.register_packet("deal_prop", 1, "", "m", "p")["code"],
                         "artifact_required")
        self.assertEqual(proposals.register_packet("deal_missing", 1, "/w", "m", "p")["code"],
                         "not_found")
        out = proposals.register_packet("deal_prop", "2", "/w", "m", "p")
        self.assertEqual((out["status"], out["proposal"]["revision"]), ("ok", 2))

    def test_only_verified_packet_can_be_sent_and_verified_revision_freezes(self):
        packet = proposals.register_packet("deal_prop", 1, "/tmp/work", "proposal-workspace.json", "send/proposal.pdf")["proposal"]
        refused = proposals.record_send(packet["id"], "email", "gmail:123", "deal_prop:r1:gmail:123")
        self.assertEqual(refused["code"], "not_verified")
        verified = proposals.verify_packet(packet["id"], "dist/send.zip", "a" * 64, "b" * 64, "evidence/receipt.json")
        self.assertEqual((verified["verified"], verified["proposal"]["proposal_state"]),
                         (True, "verified"))
        frozen = proposals.verify_packet(packet["id"], "dist/send.zip", "a" * 64, "c" * 64, "evidence/receipt.json")
        self.assertEqual(frozen["code"], "revision_frozen")
        replay = proposals.verify_packet(packet["id"], "dist/send.zip", "a" * 64, "b" * 64, "evidence/receipt.json")
        self.assertEqual((replay["status"], replay["verified"]), ("exists", True))
        self.assertEqual(proposals.verify_packet("missing", "p", "a", "b", "r")["code"], "not_found")
        self.assertEqual(proposals.verify_packet(packet["id"], "", "a", "b", "r")["code"],
                         "verification_required")

    def test_v2_verification_freezes_quality_evidence_and_receipt_identity(self):
        refused = proposals.register_packet(
            "deal_prop", 1, "/tmp/work", "proposal-workspace.json", "send/proposal.pdf",
            workspace_schema_version=2)
        self.assertEqual(refused["code"], "v2_artifact_required")
        packet = proposals.register_packet(
            "deal_prop", 1, "/tmp/work", "proposal-workspace.json", "send/proposal.pdf",
            workspace_schema_version=2,
            evidence_manifest_path="evidence/evidence-manifest.json",
            checker_report_path="evidence/checker-report.json",
            quality_report_path="evidence/proposal-quality-report.json")["proposal"]
        missing = proposals.verify_packet(
            packet["id"], "dist/send.zip", "a" * 64, "b" * 64, "evidence/receipt.json")
        self.assertEqual(missing["code"], "v2_verification_required")
        bad_quality = proposals.verify_packet(
            packet["id"], "dist/send.zip", "a" * 64, "b" * 64, "evidence/receipt.json",
            receipt_sha256="c" * 64, evidence_manifest_sha256="d" * 64,
            checker_report_sha256="e" * 64, quality_report_sha256="f" * 64,
            quality_status="fail")
        self.assertEqual(bad_quality["code"], "quality_not_passed")
        verified = proposals.verify_packet(
            packet["id"], "dist/send.zip", "a" * 64, "b" * 64, "evidence/receipt.json",
            receipt_sha256="c" * 64, evidence_manifest_sha256="d" * 64,
            checker_report_sha256="e" * 64, quality_report_sha256="f" * 64,
            quality_status="pass")
        self.assertEqual(verified["proposal"]["quality_status"], "pass")
        conn = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE commercial_proposal_packets SET quality_status='fail' WHERE id=?",
                         (packet["id"],))
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM commercial_proposal_packets WHERE id=?", (packet["id"],))
        conn.close()

    def test_send_is_append_only_idempotent_and_emits_deal_event(self):
        packet = proposals.register_packet("deal_prop", 1, "/tmp/work", "proposal-workspace.json", "send/proposal.pdf")["proposal"]
        proposals.verify_packet(packet["id"], "dist/send.zip", "a" * 64, "b" * 64, "evidence/receipt.json")
        first = proposals.record_send(packet["id"], "email", "gmail:123", "send-1", "buyer@example.com")
        again = proposals.record_send(packet["id"], "email", "gmail:123", "send-1", "buyer@example.com")
        self.assertEqual((first["status"], first["created"], again["status"], again["created"]),
                         ("ok", True, "exists", False))
        conflict = proposals.record_send(packet["id"], "whatsapp", "wa:7", "send-1")
        self.assertEqual(conflict["code"], "idempotency_conflict")
        listing = proposals.list_for_deal("deal_prop")["proposals"]
        self.assertEqual(listing[0]["proposal_state"], "sent")
        deliver = context.build_context("deal", "deal_prop")["entity"]["deliver"]
        self.assertEqual(deliver["proposal_workspace_path"], "/tmp/work")
        conn = sqlite3.connect(self.path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM deal_events WHERE deal_id='deal_prop' AND kind='proposal_sent'").fetchone()[0], 1)
        project = sprints.create_project("Proposal Project", "proposal-project-contract",
                                         repo_path="/tmp/work")
        conn.execute("UPDATE deals SET project_id=? WHERE id='deal_prop'", (project["id"],))
        conn.commit()
        conn.close()
        facet = attachments.list_project_hub(project["id"])["facets"]["proposals"]
        self.assertEqual((facet["count"], facet["items"][0]["id"]), (1, packet["id"]))

    def test_send_refusals_and_batch_state_are_explicit(self):
        self.assertEqual(proposals.record_send("missing", "email", "x", "key")["code"],
                         "not_found")
        self.assertEqual(proposals.record_send("missing", "", "x", "key")["code"],
                         "send_evidence_required")
        draft = proposals.register_packet("deal_prop", 1, "/w", "m", "p")["proposal"]
        self.assertEqual(proposals.latest_for_deals([]), {})
        self.assertEqual(proposals.latest_for_deals(["deal_prop"])["deal_prop"]["proposal_state"],
                         "draft")
        proposals.verify_packet(draft["id"], "zip", "a", "b", "receipt")
        self.assertEqual(proposals.latest_for_deals(["deal_prop"])["deal_prop"]["proposal_state"],
                         "verified")
        sent = proposals.record_send(draft["id"], "email", "gmail:explicit", "key-2",
                                     sent_at=123456789)
        self.assertEqual(sent["send"]["sent_at"], 123456789)
        self.assertEqual(proposals.latest_for_deals(["deal_prop"])["deal_prop"]["proposal_state"],
                         "sent")

    def test_agent_surface_prepares_but_cannot_assert_send(self):
        paths = {getattr(route, "path", None) for route in app.routes}
        self.assertIn("/api/crm/deals/{deal_id}/commercial-proposals", paths)
        self.assertIn("/api/crm/commercial-proposals/{packet_id}/verify", paths)
        self.assertIn("/api/crm/commercial-proposals/{packet_id}/send", paths)
        source = (Path(__file__).resolve().parents[1] / "mcp_server.py").read_text()
        self.assertIn('"register_commercial_proposal"', source)
        self.assertIn('"verify_commercial_proposal"', source)
        self.assertNotIn('"send_commercial_proposal"', source)

    def test_dashboard_contains_revision_ledger_and_explicit_project_path(self):
        source = (Path(__file__).resolve().parents[1] / "dashboard/templates/index.html").read_text()
        self.assertIn("edLoadCommercialProposals", source)
        self.assertIn("Registrar envío", source)
        self.assertIn('id="deliver-repo-path"', source)
        self.assertIn("proposal_workspace_path", source)


if __name__ == "__main__":
    unittest.main()
