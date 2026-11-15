"""Full Crow OS kernel, CLI and local Ollama wiring tests (stdlib only)."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crow_os.cli import main
from crow_os.runtime import Kernel


@contextlib.contextmanager
def stub_ollama(*, partial=False):
    class Handler(BaseHTTPRequestHandler):
        calls = 0

        def do_GET(self):
            body = json.dumps({"models": [{"name": "stub:latest"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            type(self).calls += 1
            prompt = req["messages"][-1]["content"]
            if "Plan reference line:" in prompt:
                ref = prompt.split("Plan reference line:")[-1].strip()
                value = "# verified artifact\n" + ref
            else:
                value = "Plan and acceptance criteria"
            frames = [{"message": {"content": value}, "done": False}]
            if not partial:
                frames.append({"done": True, "done_reason": "stop"})
            body = b"\n".join(json.dumps(x).encode() for x in frames) + b"\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", Handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class CrowLocalIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.home = Path(self.t.name) / "crow"

    def test_local_ollama_full_kernel_workflow_and_ledger(self):
        with stub_ollama() as (url, handler):
            kernel = Kernel(self.home)
            job = kernel.run_llm("build AVRONE integration", model="stub:latest",
                                 base_url=url)
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(handler.calls, 2)
            self.assertTrue(kernel.ledger.verify()["ok"])

    def test_incomplete_local_ollama_never_completes_workflow(self):
        with stub_ollama(partial=True) as (url, handler):
            kernel = Kernel(self.home)
            job = kernel.run_llm("must fail closed", model="stub:latest",
                                 base_url=url)
            self.assertEqual(job["status"], "failed", job)
            self.assertEqual(handler.calls, 1)
            self.assertTrue(kernel.ledger.verify()["ok"])

    def test_cli_ollama_check_and_smoke(self):
        with stub_ollama() as (url, handler):
            output = io.StringIO()
            with redirect_stdout(output):
                rc = main(["--home", str(self.home), "ollama-check",
                           "--base-url", url, "--model", "stub:latest", "--smoke"])
            self.assertEqual(rc, 0)
            data = json.loads(output.getvalue())
            self.assertTrue(data["reachable"])
            self.assertTrue(data["smoke_ok"])
            self.assertEqual(handler.calls, 1)

    def test_signed_ledger_accepts_avrone_as_evidence_only(self):
        src = Path(self.t.name) / "bridge.jsonl"
        src.write_text(json.dumps({"id": "evt-1", "run_id": "avrone-run",
            "kind": "conclusion", "text": "peer assertion, not authority",
            "agent": "peer", "refs": []}) + "\n")
        output = io.StringIO()
        with redirect_stdout(output):
            rc = main(["--home", str(self.home), "avrone-import", "--path", str(src)])
        self.assertEqual(rc, 0, output.getvalue())
        kernel = Kernel(self.home)
        self.assertTrue(kernel.ledger.verify()["ok"])
        self.assertEqual(len(kernel.workflow.jobs()), 0)  # no synthetic completion
        items = [row for row in kernel.ledger.read() if row["kind"] == "avrone.observation"]
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["raw"]["untrusted"])


if __name__ == "__main__":
    unittest.main()
