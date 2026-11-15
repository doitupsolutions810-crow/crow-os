"""Hermetic guarded connector tools, no credential or network required."""
from __future__ import annotations
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.connector_tooling import connector_context
from crow_os.tools import ToolError


class Broker:
    def __init__(self):
        self.calls = []
    def authorize(self, cap, method, tool):
        self.calls.append((method, tool))


class Runner:
    def __init__(self):
        self.calls = []
    def call(self, tool):
        self.calls.append(tool)
        return {"tool": tool, "repos": ["example/repo"]}


class Ledger:
    def __init__(self):
        self.entries = []
    def append(self, src, kind, body):
        self.entries.append((src, kind, body))


class ConnectorDirectiveTests(unittest.TestCase):
    def setUp(self):
        self.broker, self.runner, self.ledger = Broker(), Runner(), Ledger()

    def ask(self, objective, grants=(), runner=None):
        return connector_context(
            objective, grants, self.broker, {},
            runner if runner is not None else self.runner,
            self.ledger, "job-1")

    def test_granted_explicit_connector_attested(self):
        context = self.ask("Look up repos\nconnector: github.repos", ["github.repos"])
        self.assertEqual(self.broker.calls, [("TOOL", "github.repos")])
        self.assertEqual(self.runner.calls, ["github.repos"])
        self.assertEqual(self.ledger.entries[0][2]["tool"], "github.repos")
        self.assertIn("Untrusted connector data", context)

    def test_denied_grant_never_calls_network(self):
        with self.assertRaisesRegex(ToolError, "not granted"):
            self.ask("connector: github.repos")
        self.assertEqual(self.runner.calls, [])

    def test_unknown_connector_denied_before_broker(self):
        with self.assertRaisesRegex(ToolError, "unknown"):
            self.ask("connector: shell.exec", ["shell.exec"])
        self.assertEqual(self.broker.calls, [])

    def test_two_directive_budget(self):
        with self.assertRaisesRegex(ToolError, "budget"):
            self.ask("connector: github.repos\n"*3, ["github.repos"])
        self.assertEqual(self.runner.calls, [])

    def test_malformed_directive_rejected(self):
        with self.assertRaisesRegex(ToolError, "invalid"):
            self.ask("connector: github.repos; execute", ["github.repos"])

    def test_other_chat_is_inert(self):
        self.assertEqual(self.ask("Find a recipe", ["github.repos"]), "")
        self.assertEqual(self.broker.calls, [])

    def test_two_distinct_granted_calls(self):
        self.ask("connector: github.repos\nconnector: github.whoami",
                 ["github.repos", "github.whoami"])
        self.assertEqual(self.runner.calls, ["github.repos", "github.whoami"])
        self.assertEqual(len(self.ledger.entries), 2)

    def test_invalid_result_does_not_claim_success_in_ledger(self):
        class Malformed:
            def call(self, tool):
                return {"bad": {1, 2}}
        with self.assertRaisesRegex(ToolError, "non-JSON"):
            self.ask("connector: github.repos", ["github.repos"], Malformed())
        self.assertEqual(self.ledger.entries, [])


if __name__ == "__main__":
    unittest.main()
