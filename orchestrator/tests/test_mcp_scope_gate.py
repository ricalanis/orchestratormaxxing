"""The scope gate is the boundary that decides what an unauthenticated client sees.

Two separate mechanisms, tested separately because they fail independently:

  * `_resolve_scope()` — may a process *claim* privileged? Only when the request
    (`HERMES_MCP_SCOPE=privileged`) is backed by a token that matches the
    configured one, presented either inline (`HERMES_MCP_TOKEN`) or by path
    (`HERMES_MCP_TOKEN_FILE`, so a config file carries a path and not a secret).
  * `_scope_allows()` — given a resolved scope, which tools are visible? A
    privileged tool must never be reachable from the default scope.

The suggestions verbs (WhatsApp/Fireflies evidence — verbatim customer speech)
are asserted privileged here rather than in a suggestions-local test: the
question "can an unauthenticated caller read raw conversation evidence?" belongs
to the gate, and a future verb added without the classification should turn this
file red, not a file its author is editing anyway.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mcp_server  # noqa: E402


class ScopeResolution(unittest.TestCase):
    """_resolve_scope() reads the environment at call time, so each case sets
    exactly the vars it means and clears the rest."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in (
            "HERMES_MCP_SCOPE", "HERMES_MCP_TOKEN", "HERMES_MCP_TOKEN_FILE",
            "HERMES_MCP_PRIVILEGED_TOKEN")}
        for k in self._saved:
            os.environ.pop(k, None)
        self._tmp = tempfile.TemporaryDirectory()
        self.token = "s3cret-token-value"
        os.environ["HERMES_MCP_PRIVILEGED_TOKEN"] = self.token

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def _token_file(self, content: str) -> str:
        p = Path(self._tmp.name) / "tok"
        p.write_text(content)
        return str(p)

    def test_default_when_not_requested(self):
        self.assertEqual(mcp_server._resolve_scope(), "default")

    def test_privileged_requires_a_token_when_one_is_configured(self):
        os.environ["HERMES_MCP_SCOPE"] = "privileged"
        self.assertEqual(mcp_server._resolve_scope(), "default")

    def test_inline_token_elevates(self):
        os.environ["HERMES_MCP_SCOPE"] = "privileged"
        os.environ["HERMES_MCP_TOKEN"] = self.token
        self.assertEqual(mcp_server._resolve_scope(), "privileged")

    def test_wrong_inline_token_stays_default(self):
        os.environ["HERMES_MCP_SCOPE"] = "privileged"
        os.environ["HERMES_MCP_TOKEN"] = "wrong"
        self.assertEqual(mcp_server._resolve_scope(), "default")

    def test_token_file_elevates(self):
        # The Hermes gateway's mcp_servers entry uses this form: the config
        # holds a path, never the secret.
        os.environ["HERMES_MCP_SCOPE"] = "privileged"
        os.environ["HERMES_MCP_TOKEN_FILE"] = self._token_file(self.token + "\n")
        self.assertEqual(mcp_server._resolve_scope(), "privileged")

    def test_wrong_token_file_stays_default(self):
        os.environ["HERMES_MCP_SCOPE"] = "privileged"
        os.environ["HERMES_MCP_TOKEN_FILE"] = self._token_file("wrong")
        self.assertEqual(mcp_server._resolve_scope(), "default")

    def test_missing_token_file_stays_default(self):
        os.environ["HERMES_MCP_SCOPE"] = "privileged"
        os.environ["HERMES_MCP_TOKEN_FILE"] = str(Path(self._tmp.name) / "absent")
        self.assertEqual(mcp_server._resolve_scope(), "default")


class ScopeVisibility(unittest.TestCase):
    """`_scope_allows()` reads the module-global ACTIVE_SCOPE resolved at import,
    so these cases set it directly and restore it."""

    def setUp(self):
        self._saved = mcp_server.ACTIVE_SCOPE

    def tearDown(self):
        mcp_server.ACTIVE_SCOPE = self._saved

    def test_no_privileged_tool_is_visible_by_default(self):
        mcp_server.ACTIVE_SCOPE = "default"
        leaked = sorted(t for t in mcp_server.PRIVILEGED_TOOLS if mcp_server._scope_allows(t))
        self.assertEqual(leaked, [], f"privileged tools reachable unauthenticated: {leaked}")

    def test_default_scope_still_serves_the_read_surface(self):
        mcp_server.ACTIVE_SCOPE = "default"
        unprivileged = [n for n in mcp_server.TOOL_HANDLERS
                        if n not in mcp_server.PRIVILEGED_TOOLS]
        self.assertTrue(unprivileged, "expected a non-empty default read surface")
        for name in sorted(unprivileged)[:5]:
            self.assertTrue(mcp_server._scope_allows(name))

    def test_privileged_scope_sees_privileged_tools(self):
        mcp_server.ACTIVE_SCOPE = "privileged"
        for name in sorted(mcp_server.PRIVILEGED_TOOLS)[:5]:
            self.assertTrue(mcp_server._scope_allows(name))


class EvidenceBearingVerbsArePrivileged(unittest.TestCase):
    """Suggestion verbs carry verbatim meeting/chat evidence. Any of them
    reachable in the default scope means raw customer speech is readable by an
    unauthenticated caller. The set is asserted as it is implemented — a verb
    that does not exist yet cannot be classified, so this guards what ships."""

    EVIDENCE_VERBS = (
        "ingest_fireflies_suggestions",
        "get_suggestion_mining_batch",
        "propose_suggestions",
        "list_suggestions",
        "dispatch_suggestion_cards",
    )

    def test_implemented_suggestion_verbs_are_privileged(self):
        implemented = [v for v in self.EVIDENCE_VERBS if v in mcp_server.TOOL_HANDLERS]
        for name in implemented:
            self.assertIn(name, mcp_server.PRIVILEGED_TOOLS,
                          f"{name} exposes conversation evidence — it must be privileged")

    def test_acceptance_is_not_reachable_over_mcp(self):
        """The human gate is structural: accepting a suggestion creates a task,
        so it lives behind the authenticated dashboard only. A miner agent that
        could call it would be able to close its own loop."""
        for name in ("accept_suggestion", "dismiss_suggestion"):
            self.assertNotIn(name, mcp_server.TOOL_HANDLERS,
                             f"{name} must not be an MCP verb — acceptance is human-only")

    def test_personal_okrs_read_is_privileged(self):
        """The operator's personal objectives never join the fleet's default reads."""
        self.assertIn("get_personal_okrs", mcp_server.TOOL_HANDLERS)
        self.assertIn("get_personal_okrs", mcp_server.PRIVILEGED_TOOLS)
        saved = mcp_server.ACTIVE_SCOPE
        try:
            mcp_server.ACTIVE_SCOPE = "default"
            visible = {t["name"] for t in mcp_server.TOOLS
                       if mcp_server._scope_allows(t["name"])}
            self.assertNotIn("get_personal_okrs", visible)
            denial = mcp_server.handle_request({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "get_personal_okrs", "arguments": {}},
            })
            self.assertEqual(denial["error"]["code"], -32001)
        finally:
            mcp_server.ACTIVE_SCOPE = saved


if __name__ == "__main__":
    unittest.main()
