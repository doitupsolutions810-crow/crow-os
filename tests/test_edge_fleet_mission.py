"""Native four-agent parallel fleet mission through simulated Crow OS HTTP."""
from __future__ import annotations
import contextlib
import importlib.util
import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "edge_mission.py"
spec = importlib.util.spec_from_file_location("crow_fleet_mission", SCRIPT)
mission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mission)


@contextlib.contextmanager
def fleet_api(*, failed=False):
    class Handler(BaseHTTPRequestHandler):
        agents = []
        parents = {}
        requests = []

        def respond(self, code, payload):
            raw = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/api/status":
                return self.respond(200, {"ollama": {"reachable": True},
                                          "ledger": {"ok": True}})
            if self.path == "/api/agents":
                return self.respond(200, {"agents": list(type(self).agents)})
            if self.path.startswith("/api/jobs/"):
                jid = self.path.rsplit("/", 1)[-1]
                parent = type(self).parents[jid]
                parent["polls"] += 1
                status = "running" if parent["polls"] == 1 else (
                    "failed" if failed else "completed")
                members = [{"agent": agent, "job_id": "member-" + agent,
                            "status": status} for agent in parent["agents"]]
                return self.respond(200, {"job": {"job_id": jid, "status": status},
                    "entries": [{"kind": "fleet.member", "raw": {"agent": a}}
                                for a in parent["agents"]],
                    "outputs": {"artifact": "fleet report", "fleet": members}})
            return self.respond(404, {})

        def do_POST(self):
            cls = type(self)
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            cls.requests.append((self.path, payload))
            if self.path == "/api/agents":
                agent = {"id": f"agent-{len(cls.agents)+1}", **payload}
                cls.agents.append(agent)
                return self.respond(201, {"agent": agent})
            if self.path == "/api/fleet":
                jid = f"fleet-{len(cls.parents)+1}"
                cls.parents[jid] = {"polls": 0, "agents": payload["agents"]}
                return self.respond(202, {"started": True, "job_id": jid})
            return self.respond(404, {})

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", Handler
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


class FleetHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evidence = Path(self.temp.name) / "mission.json"

    def execute(self, base, *args):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = mission.main(["--base-url", base, "--deadline", "6",
                                 "--evidence", str(self.evidence), *args])
        return code, json.loads(self.evidence.read_text())

    def test_two_parallel_fleet_rounds_reuse_four_named_agents(self):
        with fleet_api() as (base, backend):
            code, evidence = self.execute(base, "--fleet", "--rounds", "2",
                                          "--enable-network")
            self.assertEqual(code, 0)
            self.assertTrue(evidence["all_complete"])
            self.assertTrue(evidence["fleet_mode"])
            self.assertEqual(len(evidence["jobs"]), 2)
            self.assertEqual(len(backend.agents), 4)
            self.assertTrue(all(a["name"] and a["instructions"] for a in backend.agents))
            requests = [body for path, body in backend.requests if path == "/api/fleet"]
            self.assertEqual(len(requests), 2)
            self.assertEqual(len(requests[0]["agents"]), 4)
            self.assertTrue(requests[0]["objective"].startswith(mission.PROMPT))
            self.assertTrue(all(len(j["fleet_members"]) == 4 for j in evidence["jobs"]))
            self.assertTrue(all(j["network_calls_observed"] == 0 for j in evidence["jobs"]))

    def test_failed_fleet_prevents_later_rounds(self):
        with fleet_api(failed=True) as (base, backend):
            code, evidence = self.execute(base, "--fleet", "--rounds", "3")
            self.assertEqual(code, 1)
            self.assertFalse(evidence["all_complete"])
            self.assertEqual(evidence["rounds_finished"], 1)
            self.assertEqual(len(backend.parents), 1)

    def test_model_override_not_silently_ignored(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                mission.main(["--fleet", "--model", "unavailable", "--dry-run"])

    def test_fleet_without_network_gets_no_network_tools(self):
        with fleet_api() as (base, backend):
            code, evidence = self.execute(base, "--fleet")
            self.assertEqual(code, 0)
            self.assertTrue(all(a["tools"] == [] for a in backend.agents))
            self.assertEqual(evidence["jobs"][0]["tools"], [])


if __name__ == "__main__":
    unittest.main()
