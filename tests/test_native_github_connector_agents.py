"""Full kernel tests: named custom agents + governed GitHub connector."""
from __future__ import annotations
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.agents import AgentError
from crow_os.runtime import Kernel


class FakeOllama:
    def __init__(self, *args, **kwargs):
        self.prompts = []

    def chat_stream(self, system, user, on_token=None):
        self.prompts.append((system, user))
        return "Verified mock agent response"


class NativeGitHubConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.kernel = Kernel(Path(self.temp.name) / "crow")

    def test_named_agent_with_connector_and_custom_instructions_persists(self):
        catalog = self.kernel.connectors.tool_names()
        self.assertIn("github.repos", catalog)
        agent = self.kernel.agents.create(
            "GitHub Repo Auditor", "Inspect public repository listings carefully.",
            tools=["github.repos"], extra_tools=catalog)
        persisted = Kernel(self.kernel.home).agents.get(agent["id"])
        self.assertEqual(persisted["name"], "GitHub Repo Auditor")
        self.assertEqual(persisted["instructions"], "Inspect public repository listings carefully.")
        self.assertEqual(persisted["tools"], ["github.repos"])

    def test_explicit_connector_directive_executes_only_with_capability(self):
        agent = self.kernel.agents.create(
            "Repo Reader", "Summarize read-only repository evidence.",
            tools=["github.repos"],
            extra_tools=self.kernel.connectors.tool_names())
        with mock.patch("crow_os.runtime.OllamaClient", FakeOllama), \
             mock.patch.object(self.kernel.connectors, "call", return_value={
                 "tool": "github.repos", "repos": ["example/repo"], "count": 1
             }) as connector:
            job = self.kernel.run_agent(
                "connector: github.repos\nSummarize the listing",
                agent["id"])
        self.assertEqual(job["status"], "completed", job)
        connector.assert_called_once_with("github.repos")
        self.assertTrue(self.kernel.ledger.verify()["ok"])
        records = [row["raw"] for row in self.kernel.ledger.read()
                   if row["kind"] == "tool.invoked"
                   and row["raw"].get("tool") == "github.repos"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["job_id"], job["job_id"])

    def test_missing_grant_denies_connector_without_network_call(self):
        agent = self.kernel.agents.create("Plain Agent", "Do not use tools.")
        with mock.patch("crow_os.runtime.OllamaClient", FakeOllama), \
             mock.patch.object(self.kernel.connectors, "call") as connector:
            job = self.kernel.run_agent("connector: github.repos", agent["id"])
        self.assertEqual(job["status"], "failed")
        connector.assert_not_called()
        self.assertTrue(self.kernel.ledger.verify()["ok"])

    def test_unknown_connector_tool_cannot_be_authorized_by_catalog_injection(self):
        with self.assertRaises(AgentError):
            self.kernel.agents.create(
                "Unsafe", "Attempt a forbidden grant.",
                tools=["shell.exec"],
                extra_tools=["github.repos", "shell.exec"])


if __name__ == "__main__":
    unittest.main()
