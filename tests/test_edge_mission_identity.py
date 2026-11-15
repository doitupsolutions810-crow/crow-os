"""Untrusted API identities must never become ambiguous mission references."""
from __future__ import annotations
import importlib.util
import io
import random
import unittest
from contextlib import redirect_stderr
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "edge_mission.py"
spec = importlib.util.spec_from_file_location("crow_mission_ids", SCRIPT)
mission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mission)


class IdentitySafetyTests(unittest.TestCase):
    def test_invalid_job_and_agent_ids_denied(self):
        values = ("", "../x", "a/b", "x\n", "x\x00", "x"*129, None,
                  42, "%2e%2e", "x?y", "x#y")
        for value in values:
            with self.subTest(value=value), self.assertRaises(mission.MissionError):
                mission._safe_id(value, "job")

    def test_normal_ids_pass(self):
        for value in ("agent-1", "fleet_123", "abc.123", "job:xyz"):
            self.assertEqual(mission._safe_id(value, "job"), value)

    def test_duplicate_agent_id_fails_before_workload_dispatch(self):
        class DuplicateIDs:
            def __init__(self):
                self.posts = []

            def request(self, method, path, payload=None):
                if method == "GET":
                    return {"agents": []}
                self.posts.append((path, payload))
                return {"agent": {"id": "reused-id", "name": payload["name"],
                        "instructions": payload["instructions"],
                        "tools": payload["tools"]}}

        client = DuplicateIDs()
        with self.assertRaisesRegex(mission.MissionError, "repeated agent ID"):
            mission.ensure_agents(client)
        self.assertEqual([path for path, _ in client.posts],
                         ["/api/agents", "/api/agents"])

    def test_false_fleet_start_is_not_trusted(self):
        class FalseStart:
            def request(self, method, path, payload=None):
                if method == "GET":
                    return {"agents": []}
                if path == "/api/agents":
                    return {"agent": {"id": payload["name"].lower().replace(" ", "-"),
                                      "name": payload["name"],
                                      "instructions": payload["instructions"],
                                      "tools": payload["tools"]}}
                return {"started": False, "job_id": "orphan"}
        with self.assertRaisesRegex(mission.MissionError, "confirm"):
            mission.start_fleet_mission(FalseStart())

    def test_wrong_polled_job_identity_fails_closed(self):
        class WrongJob:
            def request(self, method, path, payload=None):
                return {"job": {"job_id": "other", "status": "completed"}}
        with self.assertRaisesRegex(mission.MissionError, "mismatched"):
            mission.watch_mission(WrongJob(), [{"job_id": "expected"}], deadline=1)

    def test_250_seeded_unsafe_identifiers(self):
        rng = random.Random(15112026)
        for _ in range(250):
            value = f"job-{rng.randint(1, 99999)}{rng.choice(['/', '?', '#', chr(0), chr(10), ' ', '%'])}"
            with self.assertRaises(mission.MissionError):
                mission._safe_id(value, "job")


if __name__ == "__main__":
    unittest.main()
