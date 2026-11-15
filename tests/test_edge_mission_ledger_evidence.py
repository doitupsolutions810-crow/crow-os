"""Observe network tool usage from ledger events, not just agent grants."""
from __future__ import annotations
import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "edge_mission.py"
spec = importlib.util.spec_from_file_location("crow_edge_evidence", SCRIPT)
mission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mission)


class FakeDetail:
    def __init__(self, entries=(), error=None, include=True):
        self.entries = list(entries)
        self.error = error
        self.include = include

    def request(self, method, path, payload=None):
        assert method == "GET" and path == "/api/jobs/job-1"
        outcome = {"job": {"job_id": "job-1",
                           "status": "failed" if self.error else "completed"},
                   "outputs": {"artifact": "inspected"}}
        if self.error:
            outcome["job"]["error"] = self.error
        if self.include:
            outcome["entries"] = self.entries
        return outcome


class LedgerObservations(unittest.TestCase):
    def test_grants_do_not_inflate_actual_network_use(self):
        job = {"job_id": "job-1", "status": "running",
               "tools": ["net.fetch", "net.resolve"]}
        entries = [{"kind": "tool.invoked", "raw": {"tool": "net.fetch"}},
                   {"kind": "tool.invoked", "raw": {"tool": "fs.write"}},
                   {"kind": "agent.spawned", "raw": {"tool": "net.fetch"}}]
        mission.watch_mission(FakeDetail(entries), [job], deadline=2, interval=0)
        self.assertEqual(job["network_calls_observed"], 1)
        self.assertEqual(job["ledger_events_observed"], 3)

    def test_missing_entries_remain_unknown_not_zero(self):
        job = {"job_id": "job-1", "status": "running", "tools": ["net.fetch"]}
        mission.watch_mission(FakeDetail(include=False), [job], deadline=2, interval=0)
        self.assertIsNone(job["network_calls_observed"])

    def test_worker_failure_details_are_preserved(self):
        job = {"job_id": "job-1", "status": "running", "tools": []}
        mission.watch_mission(FakeDetail(error="local model unloaded"),
                              [job], deadline=2, interval=0)
        self.assertEqual(job["error"], "local model unloaded")
        self.assertEqual(job["status"], "failed")


if __name__ == "__main__":
    unittest.main()
