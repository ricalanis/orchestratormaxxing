"""MCP ↔ dashboard-API parity regression guard.

The MCP server and the dashboard API are parallel frontends over one backend
(docs/mcp-dashboard-coverage-audit.md). This pins the parity fills so a future
edit can't silently drop one side:

  Gap set A (MCP-only → gained an API endpoint):
    - GET  /api/graph/related   (twin of find_related)
    - POST /api/graph/evolve    (twin of evolve_node)

  Gap set B (API-only → gained an MCP tool):
    - search, get_memory, update_memory, delete_memory, get_stale_deals, crm_decay
      are registered AND handler-wired in mcp_server.

Stdlib unittest, pytest-discoverable. Run: python -m unittest tests.test_mcp_api_parity
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class McpToolWiring(unittest.TestCase):
    """Gap set B: the six parity tools must be both registered and dispatchable."""

    NEW_TOOLS = ["search", "get_memory", "update_memory", "delete_memory",
                 "get_stale_deals", "crm_decay"]

    def test_registered_and_handler_wired(self):
        import mcp_server
        registered = {t["name"] for t in mcp_server.TOOLS}
        for name in self.NEW_TOOLS:
            self.assertIn(name, registered, f"{name} missing from TOOLS schema")
            self.assertIn(name, mcp_server.TOOL_HANDLERS, f"{name} missing from TOOL_HANDLERS")
            self.assertTrue(callable(mcp_server.TOOL_HANDLERS[name]))

    def test_write_tools_are_privileged(self):
        import mcp_server
        for name in ("update_memory", "delete_memory", "crm_decay"):
            self.assertIn(name, mcp_server.PRIVILEGED_TOOLS,
                          f"{name} is a write/destructive tool and must be privileged")


class McpGlobalParity(unittest.TestCase):
    """The ratchet the 2026-07 audit was missing: EVERY registered tool must be
    dispatchable and vice versa. A tool defined in TOOLS but absent from
    TOOL_HANDLERS surfaces to agents in tools/list yet fails every call with
    'Unknown tool' (exactly how get_cycles_calendar/roll_cycle/delete_sprint/
    commit_cycle broke when a dict restructure dropped their entries)."""

    def test_tools_and_handlers_match_exactly(self):
        import mcp_server
        registered = {t["name"] for t in mcp_server.TOOLS}
        wired = set(mcp_server.TOOL_HANDLERS)
        self.assertEqual(registered - wired, set(),
                         "registered in TOOLS but missing from TOOL_HANDLERS "
                         "(agents see the tool, every call fails)")
        self.assertEqual(wired - registered, set(),
                         "wired in TOOL_HANDLERS but not registered in TOOLS "
                         "(dead handler — invisible to agents)")

    def test_privileged_names_are_real_tools(self):
        import mcp_server
        registered = {t["name"] for t in mcp_server.TOOLS}
        self.assertEqual(mcp_server.PRIVILEGED_TOOLS - registered, set(),
                         "PRIVILEGED_TOOLS names a tool that no longer exists")


class ParityFill2(unittest.TestCase):
    """2026-07 audit: backend verbs that lacked an MCP wrapper (15 genuine gaps;
    the other claimed gaps were already exposed under different tool names,
    e.g. update_task_status → sprints.set_task_status)."""

    READS = ["get_deal", "get_deal_fireflies", "get_deal_fireflies_latest",
             "list_deal_children", "compute_funnel", "get_project",
             "get_next_week_tasks", "get_future_tasks"]
    WRITES = ["set_task_assignee", "set_scheduled_week", "assign_task_project",
              "set_auto_cycle", "update_project", "create_cycle", "export_roadmap"]

    def test_registered_and_handler_wired(self):
        import mcp_server
        registered = {t["name"] for t in mcp_server.TOOLS}
        for name in self.READS + self.WRITES:
            self.assertIn(name, registered, f"{name} missing from TOOLS schema")
            self.assertIn(name, mcp_server.TOOL_HANDLERS, f"{name} missing from TOOL_HANDLERS")

    def test_writes_are_privileged_reads_are_not(self):
        import mcp_server
        for name in self.WRITES:
            self.assertIn(name, mcp_server.PRIVILEGED_TOOLS,
                          f"{name} is a write and must be privileged")
        for name in self.READS:
            self.assertNotIn(name, mcp_server.PRIVILEGED_TOOLS,
                             f"{name} is a pure read — default scope")


class JourneyContextualLayer(unittest.TestCase):
    """Journey fase 1 step 7 + ADICIÓN 9: the MCP is THE contextual interface
    for the four hosts (Hermes · Claude · Codex · OpenCode).

    Two ratchets, both about scope rather than existence. The verbs are pure
    reads, and `propose_deliver` is read-only BY DESIGN (ruling 3 — a chat
    conversion is a proposal with a deep link, never a write): if a later edit
    gives it a write path, the honest move is to make it privileged, and this
    test is where that decision has to be made deliberately instead of by
    accident. `get_project_hub` is the MCP twin of `GET /api/projects/{id}/hub`
    — gap set B, the same shape as the six parity fills above."""

    TOOLS = ["get_journey_pulse", "get_project_hub", "propose_deliver"]

    def test_registered_and_handler_wired(self):
        import mcp_server
        registered = {t["name"] for t in mcp_server.TOOLS}
        for name in self.TOOLS:
            self.assertIn(name, registered, f"{name} missing from TOOLS schema")
            self.assertIn(name, mcp_server.TOOL_HANDLERS,
                          f"{name} missing from TOOL_HANDLERS")
            self.assertTrue(callable(mcp_server.TOOL_HANDLERS[name]))

    def test_reads_stay_on_the_default_scope(self):
        import mcp_server
        for name in self.TOOLS:
            self.assertNotIn(name, mcp_server.PRIVILEGED_TOOLS,
                             f"{name} is a read — every agent must be able to ask "
                             f"where a client is (ADICIÓN 9)")


class GraphParityEndpoints(unittest.TestCase):
    """Gap set A: the two new graph endpoints exist and honor their contract."""

    @classmethod
    def setUpClass(cls):
        try:
            from fastapi.testclient import TestClient
            import dashboard.api as api
            cls.client = TestClient(api.app)
        except Exception as e:  # pragma: no cover
            raise unittest.SkipTest(f"dashboard app unavailable: {e}")

    def test_related_reads(self):
        r = self.client.get("/api/graph/related", params={"q": "memory", "limit": 3})
        self.assertEqual(r.status_code, 200)
        self.assertIn("related", r.json())
        self.assertIsInstance(r.json()["related"], list)

    def test_evolve_validates_node_id(self):
        r = self.client.post("/api/graph/evolve", json={"properties": {}})
        self.assertEqual(r.status_code, 400)

    def test_evolve_unknown_node_is_false_not_error(self):
        r = self.client.post("/api/graph/evolve",
                             json={"node_id": "definitely_not_a_real_node", "properties": {"x": 1}})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["evolved"])


if __name__ == "__main__":
    unittest.main()
