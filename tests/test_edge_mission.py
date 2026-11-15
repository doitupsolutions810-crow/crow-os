"""Hermetic tests for the operator-driven Crow OS edge-mission chat client."""
from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "edge_mission.py"
spec = importlib.util.spec_from_file_location("edge_mission", SCRIPT)
mission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mission)


@contextlib.contextmanager
def fake_crow():
    class Handler(BaseHTTPRequestHandler):
        agents = []
        jobs = {}
        submissions = []
        broken_listing = False
        fail_jobs = False

        def respond(self, code, value):
            raw = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            cls = type(self)
            if self.path == "/api/agents":
                return self.respond(200, {"agents": {} if cls.broken_listing else list(cls.agents)})
            if self.path.startswith("/api/jobs/"):
                jid = self.path.rsplit("/", 1)[-1]
                job = cls.jobs[jid]
                job["polls"] += 1
                status = "running" if job["polls"] < 2 else ("failed" if cls.fail_jobs else "completed")
                return self.respond(200, {"job": {"job_id": jid, "status": status},
                                          "outputs": {"artifact": "evidence for " + jid}})
            return self.respond(404, {})

        def do_POST(self):
            cls = type(self)
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            cls.submissions.append((self.path, payload))
            if self.path == "/api/agents":
                item = {"id": "agent-" + str(len(cls.agents) + 1), **payload}
                cls.agents.append(item)
                return self.respond(201, {"agent": item})
            if self.path == "/api/run":
                jid = "job-" + str(len(cls.jobs) + 1)
                cls.jobs[jid] = {"polls": 0}
                return self.respond(202, {"job": {"job_id": jid, "status": "running"}})
            return self.respond(404, {})

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield mission.CrowChat(f"http://127.0.0.1:{srv.server_port}"), Handler
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


class MissionTests(unittest.TestCase):
    def test_four_named_agents_get_exact_instruction_and_public_scoped_source(self):
        with fake_crow() as (c, api):
            jobs = mission.start_mission(c, network=True, model="local-test")
            self.assertEqual(len(jobs), 4)
            runs = [x for path, x in api.submissions if path == "/api/run"]
            self.assertEqual(len(runs), 4)
            self.assertTrue(all(x["objective"].startswith(mission.PROMPT) for x in runs))
            self.assertTrue(all(mission.SOURCE_URL in x["objective"] for x in runs))
            self.assertTrue(all(x["tools"] is True for x in runs))
            self.assertTrue(all(x["tools"] == ["net.fetch", "net.resolve"] for x in api.agents))
            mission.watch_mission(c, jobs, deadline=4, interval=0.01)
            self.assertTrue(all(j["status"] == "completed" for j in jobs))

    def test_disabled_network_never_grants_network_tools(self):
        with fake_crow() as (c, api):
            mission.start_mission(c)
            self.assertTrue(all(a["tools"] == [] for a in api.agents))
            self.assertTrue(all(p["tools"] is False for path, p in api.submissions if path == "/api/run"))

    def test_agents_reused_across_missions(self):
        with fake_crow() as (c, api):
            mission.start_mission(c)
            mission.start_mission(c)
            self.assertEqual(len(api.agents), 4)
            self.assertEqual(len(api.jobs), 8)

    def test_altered_existing_instructions_do_not_get_overwritten(self):
        with fake_crow() as (c, api):
            api.agents = [{"id": "legacy", "name": "Edge Case Tester",
                           "instructions": "original private instructions", "tools": []}]
            mission.start_mission(c)
            self.assertEqual(api.agents[0]["instructions"], "original private instructions")
            self.assertEqual(api.agents[1]["name"], "Edge Case Tester 2")
            mission.start_mission(c)
            self.assertEqual(len(api.agents), 5)

    def test_malformed_listing_stops_before_writes(self):
        with fake_crow() as (c, api):
            api.broken_listing = True
            with self.assertRaises(mission.MissionError):
                mission.start_mission(c)
            self.assertFalse(api.submissions)

    def test_failed_job_is_not_marked_complete(self):
        with fake_crow() as (c, api):
            api.fail_jobs = True
            jobs = mission.start_mission(c)
            mission.watch_mission(c, jobs, deadline=5, interval=0.01)
            self.assertTrue(all(j["status"] == "failed" for j in jobs))

    def test_deadline_stays_unverified(self):
        with fake_crow() as (c, api):
            jobs = mission.start_mission(c)
            mission.watch_mission(c, jobs, deadline=0.00001, interval=0)
            self.assertTrue(all(j["status"] == "unverified-timeout" for j in jobs))

    def test_only_loopback_crow_os_endpoint_allowed(self):
        for url in ("http://example.com:8787", "https://127.0.0.1:8787",
                    "http://127.0.0.1:8787/unrelated",
                    "http://user:pass@localhost:8787", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(mission.MissionError):
                mission.CrowChat(url)


if __name__ == "__main__":
    unittest.main()
