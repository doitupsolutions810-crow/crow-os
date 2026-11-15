"""Checkpoint, real HTTP admission and multi-round recovery regressions."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout, redirect_stderr
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("crow_mission_recovery", ROOT / "scripts" / "edge_mission.py")
mission = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mission)


@contextlib.contextmanager
def api(*, fail_after=None, healthy=True, ledger=True):
    class Handler(BaseHTTPRequestHandler):
        agents = []
        jobs = {}
        passes = 0
        fail_jobs = False

        def send(self, code, value):
            raw = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/api/status":
                return self.send(200, {"ollama": {"reachable": healthy},
                                       "ledger": {"ok": ledger}})
            if self.path == "/api/agents":
                return self.send(200, {"agents": list(type(self).agents)})
            if self.path.startswith("/api/jobs/"):
                jid = self.path.rsplit("/", 1)[-1]
                job = type(self).jobs.get(jid)
                if job is None:
                    return self.send(404, {"error": "unknown job"})
                job["polls"] += 1
                status = "running" if job["polls"] < 2 else (
                    "failed" if type(self).fail_jobs else "completed")
                return self.send(200, {"job": {"job_id": jid, "status": status},
                                       "outputs": {"artifact": "evidence: " + jid}})
            return self.send(404, {})

        def do_POST(self):
            cls = type(self)
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/agents":
                agent = {"id": f"agent-{len(cls.agents)+1}", **payload}
                cls.agents.append(agent)
                return self.send(201, {"agent": agent})
            if self.path == "/api/run":
                cls.passes += 1
                if fail_after is not None and cls.passes > fail_after:
                    return self.send(503, {"error": "injected outage"})
                jid = f"job-{len(cls.jobs)+1}"
                cls.jobs[jid] = {"polls": 0}
                return self.send(202, {"job": {"job_id": jid, "status": "running"}})
            return self.send(404, {})

        def log_message(self, *args):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", Handler
    finally:
        http.shutdown()
        http.server_close()
        thread.join(timeout=5)


class MissionRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "evidence.json"

    def execute(self, base, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = mission.main([
                "--base-url", base, "--evidence", str(self.path),
                "--deadline", "8", *args])
        return code, json.loads(self.path.read_text()), stdout.getvalue(), stderr.getvalue()

    def test_two_rounds_submit_and_observe_eight_jobs(self):
        with api() as (base, backend):
            code, evidence, stdout, _ = self.execute(base, "--rounds", "2", "--enable-network")
            self.assertEqual(code, 0, stdout)
            self.assertTrue(evidence["all_complete"])
            self.assertEqual(evidence["rounds_finished"], 2)
            self.assertEqual(len(backend.jobs), 8)
            self.assertEqual(len(evidence["jobs"]), 8)
            self.assertTrue(all(j["terminal"] for j in evidence["jobs"]))
            self.assertTrue(all(j["tools"] == ["net.fetch", "net.resolve"] for j in evidence["jobs"]))

    def test_submission_failure_keeps_two_confirmed_job_ids(self):
        with api(fail_after=2) as (base, backend):
            code, evidence, _, stderr = self.execute(base)
            self.assertEqual(code, 2)
            self.assertEqual(len(evidence["jobs"]), 2)
            self.assertEqual(len(backend.jobs), 2)
            self.assertIn("503", evidence["error"])
            self.assertFalse(evidence["all_complete"])
            self.assertIn("mission failed", stderr)

    def test_no_local_ollama_denies_without_posting(self):
        with api(healthy=False) as (base, backend):
            code, evidence, _, _ = self.execute(base)
            self.assertEqual(code, 2)
            self.assertFalse(backend.agents)
            self.assertFalse(backend.jobs)
            self.assertIn("Ollama", evidence["error"])

    def test_invalid_ledger_denies_without_posting(self):
        with api(ledger=False) as (base, backend):
            code, evidence, _, _ = self.execute(base)
            self.assertEqual(code, 2)
            self.assertFalse(backend.agents)
            self.assertFalse(backend.jobs)
            self.assertIn("ledger", evidence["error"])

    def test_failure_stops_followup_rounds(self):
        with api() as (base, backend):
            backend.fail_jobs = True
            code, evidence, _, _ = self.execute(base, "--rounds", "5")
            self.assertEqual(code, 1)
            self.assertEqual(evidence["rounds_finished"], 1)
            self.assertEqual(len(backend.jobs), 4)
            self.assertFalse(evidence["all_complete"])

    def test_atomic_evidence_replaces_symlink_not_target(self):
        target = Path(self.temp.name) / "protected"
        target.write_text("preserved")
        self.path.symlink_to(target)
        mission.save_evidence(self.path, {"safe": True})
        self.assertEqual(target.read_text(), "preserved")
        self.assertFalse(self.path.is_symlink())
        self.assertEqual(json.loads(self.path.read_text()), {"safe": True})

    def test_unbounded_rounds_rejected(self):
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                mission.main(["--dry-run", "--rounds", "6"])

    def test_unavailable_http_still_generates_failed_evidence(self):
        code, evidence, _, _ = self.execute("http://127.0.0.1:1")
        self.assertEqual(code, 2)
        self.assertEqual(evidence["jobs"], [])
        self.assertFalse(evidence["all_complete"])
        self.assertTrue(evidence["error"])


if __name__ == "__main__":
    unittest.main()
