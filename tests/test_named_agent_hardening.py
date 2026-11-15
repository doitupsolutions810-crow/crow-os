"""Custom-agent naming, instruction persistence, and capability boundaries."""
from __future__ import annotations

import concurrent.futures
import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from crow_os.agents import AgentError, AgentRegistry
from crow_os.capabilities import Broker
from crow_os.fs import CrowFS
from crow_os.ledger import Ledger
from crow_os.worker import AgentWorker
from crow_os.workflow import Workflow


def create_in_process(args):
    home, name = args
    return AgentRegistry(CrowFS(Path(home))).create(
        name, "Review edge cases", tools=["net.fetch"])["id"]


class AgentRegistryHardeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.fs = CrowFS(self.home)
        self.agents = AgentRegistry(self.fs)

    def test_named_instructions_grants_and_avatar_survive_restart(self):
        agent = self.agents.create("Network Auditor",
            "Check SSRF and DNS rebinding.\nDocument any reproducers.",
            tools=["net.fetch", "net.resolve"],
            avatar={"shape": "star", "color": "teal"})
        self.assertEqual(AgentRegistry(CrowFS(self.home)).get(agent["id"]), agent)

    def test_duplicate_names_are_not_overwritten(self):
        first = self.agents.create("Tester", "First instructions")
        second = self.agents.create("Tester", "Second instructions")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.agents.get(first["id"])["instructions"], "First instructions")
        self.assertEqual(self.agents.get(second["id"])["instructions"], "Second instructions")

    def test_bad_tool_lists_fail_as_agent_error(self):
        for value in ("net.fetch", 0, True, {"net.fetch": True}, [["net.fetch"]]):
            with self.subTest(value=value), self.assertRaises(AgentError):
                self.agents.create("Test", "instructions", tools=value)

    def test_bad_avatar_and_model_fail_as_agent_error(self):
        for value in ([], 7, "star", {"shape": []}, {"color": False}):
            with self.subTest(value=value), self.assertRaises(AgentError):
                self.agents.create("Test", "instructions", avatar=value)
        for value in (7, ["bad"], "x" * 201, "bad\x00model"):
            with self.assertRaises(AgentError):
                self.agents.create("Test", "instructions", model=value)

    def test_name_and_instructions_reject_poisoned_unicode(self):
        for value in (chr(0xD800), "name\nwith newline", "name\x00bad"):
            with self.assertRaises(AgentError):
                self.agents.create(value, "instructions")
        with self.assertRaises(AgentError):
            self.agents.create("Name", "text" + chr(0xD800))
        # Newlines and tabs in normal custom instructions remain allowed.
        self.assertIn("\n", self.agents.create("Name", "First\n\tSecond")["instructions"])

    def test_reserved_role_cannot_be_impersonated(self):
        for name in ("debugger", "coder", "tester", "governance", "kernel", "squad"):
            with self.assertRaises(AgentError):
                self.agents.create(name, "instructions")

    def test_invalid_lookup_and_delete_ids_are_rejected(self):
        for ident in ("../etc/passwd", "A/B", "", "bad\x00", "..", None):
            with self.assertRaises(AgentError):
                self.agents.get(ident)
            with self.assertRaises(AgentError):
                self.agents.delete(ident)

    def test_corrupt_agent_is_skipped_in_list(self):
        agent = self.agents.create("Valid", "Retain this")
        self.fs.write_file("/system/agents/poison.json", json.dumps({
            "id": "poison", "name": "Poison", "instructions": "bad",
            "created": float("nan"), "tools": []}))
        self.assertEqual(self.agents.list(), [agent])

    def test_concurrent_threads_use_distinct_ids(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            names = list(pool.map(lambda _: self.agents.create("Parallel", "A")["id"],
                                  range(12)))
        self.assertEqual(len(set(names)), 12)

    def test_concurrent_spawn_processes_use_distinct_ids(self):
        ctx = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(max_workers=8, mp_context=ctx) as pool:
            names = list(pool.map(create_in_process, [(str(self.home), "Parallel")] * 8))
        self.assertEqual(len(set(names)), 8)
        self.assertEqual(len(self.agents.list()), 8)

    def test_symlink_creation_lock_fails_closed(self):
        target = self.home / "protected"
        target.write_text("untouched")
        (self.fs.root / "system" / "agents" / ".create.lock").symlink_to(target)
        with self.assertRaises(AgentError):
            self.agents.create("Unsafe", "do not write")
        self.assertEqual(target.read_text(), "untouched")


class FakeClient:
    def chat_stream(self, system, user, on_token=None):
        if on_token is not None:
            on_token("report")
        return "report"


class AgentSpawnCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = Ledger(Path(self.temp.name) / "ledger.jsonl")
        self.workflow = Workflow(self.ledger)
        self.broker = Broker(b"agent-capability-regression-32bytes")
        self.agent = {"id": "parent", "name": "Parent",
                      "instructions": "Analyze edge cases.",
                      "tools": ["agent.spawn"]}
        self.calls = []
        self.spawner = lambda parent, name, objective: self._spawn(parent, name, objective)

    def _spawn(self, parent, name, objective):
        self.calls.append((parent, name, objective))
        return {"job_id": "child-job", "status": "completed"}

    def run_agent(self, rights, grants=None):
        if grants is not None:
            self.agent["tools"] = grants
        cap = self.broker.issue("kernel", rights, rate_limit=12)
        worker = AgentWorker(self.broker, self.workflow, self.ledger,
                             FakeClient(), spawner=self.spawner)
        outputs = {}
        job = worker.run("delegate to child: inspect network edges",
                         self.agent, cap, outputs=outputs)
        return job, outputs

    def test_definition_cannot_delegate_without_tool_capability(self):
        job, _ = self.run_agent([{"method": "EXECUTE", "resource": "jobs.parent"}])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.calls, [])
        self.assertTrue(self.ledger.verify()["ok"])

    def test_valid_spawn_capability_permits_delegation(self):
        job, outputs = self.run_agent([
            {"method": "EXECUTE", "resource": "jobs.parent"},
            {"method": "TOOL", "resource": "agent.spawn"}])
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(outputs[job["job_id"]]["subagents"][0]["status"], "completed")

    def test_agent_with_no_spawn_grant_cannot_delegate(self):
        job, _ = self.run_agent([
            {"method": "EXECUTE", "resource": "jobs.parent"},
            {"method": "TOOL", "resource": "agent.spawn"}], grants=[])
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
