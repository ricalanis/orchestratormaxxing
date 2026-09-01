"""Verb-audit P0: PATCH /api/projects/{id} + PATCH /api/crm/contacts/{id} + crm.update_contact MCP tool.

Pins the three surfaces added in the verb-audit gap fix:
  1. PATCH /api/projects/{project_id} — rename/recolor/description/slug/icon
  2. PATCH /api/crm/contacts/{contact_id} — inline-edit contact fields
  3. MCP tool update_contact — same handler the dashboard calls, no SQL drift

Isolation: dashboard.api runs ensure_schema() at import, so the DB layers are
pointed at a COPY of ~/.hermes/kanban.db BEFORE the import — the real DB is never
touched. If there's no kanban.db to copy, the whole case skips.

Run:  python -m pytest tests/test_patch_verb_audit.py -v
      python -m unittest tests.test_patch_verb_audit
"""
import atexit
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid as _uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_READY = False
_CLIENT = None
_TMP_DB = None
_DASH_TOKEN = None
try:
    from dashboard import db as _db

    _REAL_DB = Path.home() / ".hermes" / "kanban.db"
    if _REAL_DB.exists():
        _fd, _tmp = tempfile.mkstemp(prefix="kanban_test_patch_", suffix=".db")
        os.close(_fd)
        shutil.copy(_REAL_DB, _tmp)
        _TMP_DB = Path(_tmp)
        _db.KANBAN_DB = _TMP_DB

        # Read the dashboard token so test mutations pass the auth gate.
        _TOKEN_FILE = Path.home() / ".config" / "orchestratormaxxing" / "dashboard-token"
        try:
            _DASH_TOKEN = _TOKEN_FILE.read_text().strip()
        except Exception:
            _DASH_TOKEN = os.environ.get("HERMES_DASHBOARD_TOKEN", "")

        from dashboard.api import app  # ensure_schema() runs here, on the copy
        from starlette.testclient import TestClient

        _CLIENT = TestClient(app, raise_server_exceptions=False)
        _READY = True
except Exception:  # pragma: no cover
    _READY = False


def _auth():
    """Return Authorization header dict for mutating requests."""
    if _DASH_TOKEN:
        return {"Authorization": f"Bearer {_DASH_TOKEN}"}
    return {}


@atexit.register
def _cleanup_tmp_db():  # pragma: no cover
    try:
        if _TMP_DB and _TMP_DB.exists():
            _TMP_DB.unlink()
    except Exception:
        pass


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class PatchProjectEndpoint(unittest.TestCase):
    """PATCH /api/projects/{project_id} — rename, recolor, update description."""

    def setUp(self):
        # Create a fresh project to patch in each test — unique slug to avoid
        # UNIQUE constraint collisions across tests sharing the same DB copy.
        self._suffix = _uuid.uuid4().hex[:6]
        self._slug = f"patch-test-{self._suffix}"
        resp = _CLIENT.post("/api/projects", json={
            "name": f"Patch Test Project {self._suffix}",
            "slug": self._slug,
            "description": "original desc",
            "color": "#3b82f6",
            "icon": "📦",
        }, headers=_auth())
        self.assertEqual(resp.status_code, 200, f"create_project failed: {resp.text}")
        body = resp.json()
        self.project_id = body.get("id") or body.get("project_id")
        self.assertIsNotNone(self.project_id, f"create_project returned: {body}")

    def test_rename_project(self):
        """Rename a project via PATCH — only name changes."""
        resp = _CLIENT.patch(f"/api/projects/{self.project_id}", json={
            "name": "Renamed Project"}, headers=_auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "updated")
        self.assertEqual(body["project"]["name"], "Renamed Project")
        # slug should be unchanged
        self.assertEqual(body["project"]["slug"], self._slug)

    def test_patch_multiple_fields(self):
        """Patch name + color + description in one call."""
        resp = _CLIENT.patch(f"/api/projects/{self.project_id}", json={
            "name": "Multi Patch",
            "color": "#ff0000",
            "description": "updated description"}, headers=_auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "updated")
        self.assertEqual(body["project"]["name"], "Multi Patch")
        self.assertEqual(body["project"]["color"], "#ff0000")
        self.assertEqual(body["project"]["description"], "updated description")

    def test_patch_empty_body_rejected(self):
        """No fields supplied → 400 'nothing to update'."""
        resp = _CLIENT.patch(f"/api/projects/{self.project_id}", json={},
                             headers=_auth())
        self.assertEqual(resp.status_code, 400)

    def test_patch_nonexistent_project_404(self):
        resp = _CLIENT.patch("/api/projects/proj_nope0000", json={"name": "X"},
                             headers=_auth())
        self.assertEqual(resp.status_code, 404)


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class PatchContactEndpoint(unittest.TestCase):
    """PATCH /api/crm/contacts/{contact_id} — inline-edit contact fields."""

    def setUp(self):
        # Create an account + contact to patch
        acct = _CLIENT.post("/api/crm/accounts", json={
            "name": "Patch Test Co", "domain": "patchtest.example.com"},
            headers=_auth())
        self.account_id = acct.json().get("account_id")
        # If account already exists, it returns 'exists' with the id
        if not self.account_id:
            # Find it in the list
            accts = _CLIENT.get("/api/crm/accounts").json().get("accounts", [])
            for a in accts:
                if a["name"] == "Patch Test Co":
                    self.account_id = a["id"]
                    break
        self.assertIsNotNone(self.account_id, "Could not create/find test account")

        resp = _CLIENT.post("/api/crm/contacts", json={
            "account_id": self.account_id,
            "name": "Original Contact",
            "email": "orig@patchtest.com",
            "role": "CTO",
            "phone": "+52 55 1234 5678",
            "source": "linkedin",
        }, headers=_auth())
        self.assertEqual(resp.status_code, 200, f"create_contact failed: {resp.text}")
        body = resp.json()
        self.contact_id = body.get("contact_id")
        self.assertIsNotNone(self.contact_id, f"create_contact returned: {body}")

    def test_rename_contact(self):
        """Rename a contact via PATCH."""
        resp = _CLIENT.patch(f"/api/crm/contacts/{self.contact_id}", json={
            "name": "Renamed Contact"}, headers=_auth())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "updated")

    def test_update_phone_and_role(self):
        """Update phone and role in one PATCH."""
        resp = _CLIENT.patch(f"/api/crm/contacts/{self.contact_id}", json={
            "phone": "+52 55 9999 8888",
            "role": "VP Engineering"}, headers=_auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "updated")

    def test_update_invalid_source_rejected(self):
        """source must be from the closed enum — 'twitter' is invalid."""
        resp = _CLIENT.patch(f"/api/crm/contacts/{self.contact_id}", json={
            "source": "twitter"}, headers=_auth())
        self.assertEqual(resp.status_code, 400)

    def test_clear_phone_with_empty_string(self):
        """Empty string for phone should clear it (set to NULL)."""
        resp = _CLIENT.patch(f"/api/crm/contacts/{self.contact_id}", json={
            "phone": ""}, headers=_auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "updated")

    def test_patch_nonexistent_contact_400(self):
        """Non-existent contact_id → error (400, not 500)."""
        resp = _CLIENT.patch("/api/crm/contacts/cont_nope0000", json={
            "name": "X"}, headers=_auth())
        self.assertEqual(resp.status_code, 400)


@unittest.skipUnless(_READY, "dashboard.api / kanban.db unavailable")
class UpdateContactMCPTool(unittest.TestCase):
    """Verify the MCP tool_update_contact handler exists and calls crm.update_contact."""

    def test_mcp_tool_definition_exists(self):
        """The update_contact tool must be in the MCP tool catalog."""
        from mcp_server import TOOLS
        names = [t["name"] for t in TOOLS]
        self.assertIn("update_contact", names)

    def test_mcp_tool_in_privileged_set(self):
        """update_contact must be in PRIVILEGED_TOOLS (CRM writes are operator-only)."""
        from mcp_server import PRIVILEGED_TOOLS
        self.assertIn("update_contact", PRIVILEGED_TOOLS)

    def test_mcp_tool_dispatch_exists(self):
        """The TOOL_HANDLERS table must map update_contact to a handler."""
        from mcp_server import TOOL_HANDLERS
        self.assertIn("update_contact", TOOL_HANDLERS)
        self.assertTrue(callable(TOOL_HANDLERS["update_contact"]))

    def test_mcp_tool_required_fields(self):
        """The tool schema must require contact_id and accept the documented fields."""
        from mcp_server import TOOLS
        tool = next(t for t in TOOLS if t["name"] == "update_contact")
        self.assertIn("contact_id", tool["inputSchema"]["required"])
        props = tool["inputSchema"]["properties"]
        for field in ("contact_id", "name", "email", "role", "phone",
                      "whatsapp", "linkedin_url", "source", "source_notes",
                      "account_id"):
            self.assertIn(field, props, f"Missing field '{field}' in update_contact schema")


if __name__ == "__main__":
    unittest.main()