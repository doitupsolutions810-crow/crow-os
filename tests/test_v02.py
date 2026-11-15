import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crow_os.capabilities import Broker
from crow_os.ledger import Ledger
from crow_os.llm import OllamaClient
from crow_os.runtime import Kernel
from crow_os.workflow import Workflow
from crow_os.worker import LLMWorker

SECRET = b"test-secret-test-secret-32bytes!!"


class StubOllama(BaseHTTPRequestHandler):
    calls = 0
    job_ref = "plan:unknown"

    def do_POST(self):
        type(self).calls += 1
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        user = body["messages"][-1]["content"]
        if "Plan reference line:" in user:
            ref = user.split("Plan reference line:")[-1].strip()
            content = f"# artifact\n{ref}\nprint('hi')\n"
        else:
            content = "1. do the thing\nCriteria: artifact references plan"
        out = json.dumps({"message": {"role": "assistant", "content": content}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


def start_stub():
    StubOllama.calls = 0
    srv = HTTPServer(("127.0.0.1", 0), StubOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class LLMWorkerTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.ledger = Ledger(self.dir / "ledger.jsonl")
        self.broker = Broker(SECRET)
        self.wf = Workflow(self.ledger)

    def test_llm_run_completes_against_stub(self):
        srv, url = start_stub()
        try:
            client = OllamaClient(base_url=url, model="stub")
            cap = self.broker.issue("k", [{"method": "EXECUTE", "resource": "jobs.debugger"},
                                          {"method": "EXECUTE", "resource": "jobs.coder"}],
                                    rate_limit=10)
            job = LLMWorker(self.broker, self.wf, self.ledger, client).run("build it", cap)
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(StubOllama.calls, 2)
            self.assertTrue(self.ledger.verify()["ok"])
        finally:
            srv.shutdown()

    def test_gate_blocks_before_model_is_called(self):
        srv, url = start_stub()
        try:
            client = OllamaClient(base_url=url, model="stub")
            cap = self.broker.issue("k", [{"method": "EXECUTE", "resource": "jobs.debugger"}],
                                    rate_limit=10)  # no jobs.coder right
            job = LLMWorker(self.broker, self.wf, self.ledger, client).run("build it", cap)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(StubOllama.calls, 1)  # coder never reached the model
        finally:
            srv.shutdown()

    def test_unreachable_ollama_fails_closed(self):
        client = OllamaClient(base_url="http://127.0.0.1:9", model="stub", timeout=1)
        cap = self.broker.issue("k", [{"method": "EXECUTE", "resource": "jobs.debugger"},
                                      {"method": "EXECUTE", "resource": "jobs.coder"}],
                                rate_limit=10)
        job = LLMWorker(self.broker, self.wf, self.ledger, client).run("build it", cap)
        self.assertEqual(job["status"], "failed")
        self.assertIn("LLMUnavailable", job.get("error", "") + str(job))


class Ed25519Tests(unittest.TestCase):
    def test_ed25519_broker_roundtrip_and_tamper(self):
        try:
            from crow_os.signers import Ed25519Signer
            signer = Ed25519Signer.generate()
        except ImportError:
            self.skipTest("cryptography not installed")
        broker = Broker(signer=signer)
        cap = broker.issue("agent", [{"method": "GET", "resource": "/x"}])
        self.assertEqual(broker.authorize(cap, "GET", "/x")["status"], "ok")
        forged = dict(cap, rights=[{"method": "DELETE", "resource": "/x"}])
        from crow_os.capabilities import CapabilityError
        with self.assertRaises(CapabilityError):
            broker.authorize(forged, "DELETE", "/x")
        # a verifier holding only the public key verifies but cannot issue
        verifier = Broker(signer=Ed25519Signer.verifier(signer.public_key()))
        self.assertEqual(verifier.authorize(cap, "GET", "/x")["status"], "ok")


class KernelLLMTests(unittest.TestCase):
    def test_kernel_run_llm_against_stub(self):
        srv, url = start_stub()
        try:
            kernel = Kernel(Path(tempfile.mkdtemp()))
            job = kernel.run_llm("build the OS", model="stub", base_url=url)
            self.assertEqual(job["status"], "completed", job)
        finally:
            srv.shutdown()


if __name__ == "__main__":
    unittest.main()


class Round10RegressionTests(unittest.TestCase):
    """Regressions for the edge-harness probes L7 / C2 / C7."""

    def test_concurrent_appends_keep_chain(self):
        import threading
        ledger = Ledger(Path(tempfile.mkdtemp()) / "l.jsonl")
        threads = [threading.Thread(target=ledger.append, args=("t", "x", {"n": i}))
                   for i in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        v = ledger.verify()
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["entries"], 20)

    def test_child_cannot_outlive_parent(self):
        import time
        broker = Broker(SECRET)
        parent = broker.issue("a", [{"method": "GET", "resource": "/x"}],
                              ttl_seconds=0.05)
        child = broker.issue("a", [{"method": "GET", "resource": "/x"}],
                             parent=parent, ttl_seconds=300)
        self.assertLessEqual(child["expires"], parent["expires"])
        time.sleep(0.07)
        from crow_os.capabilities import CapabilityError
        with self.assertRaises(CapabilityError):
            broker.authorize(child, "GET", "/x")

    def test_revocation_shared_via_state_file(self):
        home = Path(tempfile.mkdtemp())
        sp = home / "broker_state.json"
        b1 = Broker(SECRET, state_path=sp)
        cap = b1.issue("a", [{"method": "GET", "resource": "/x"}])
        b1.revoke(cap["id"])
        b2 = Broker(SECRET, state_path=sp)
        from crow_os.capabilities import CapabilityError
        with self.assertRaises(CapabilityError):
            b2.authorize(cap, "GET", "/x")

    def test_tester_failure_stops_chain_without_exception(self):
        # Regression (stub probe S3): a failed tester report ends the job;
        # the worker must return the failed job, not raise WorkflowError
        # by reporting governance on a terminal job.
        class NoRefStub(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                content = "artifact with no reference line"
                out = json.dumps({"message": {"content": content}}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), NoRefStub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{srv.server_address[1]}"
            client = OllamaClient(base_url=url, model="stub")
            ledger = Ledger(Path(tempfile.mkdtemp()) / "l.jsonl")
            broker = Broker(SECRET)
            wf = Workflow(ledger)
            cap = broker.issue("k", [{"method": "EXECUTE", "resource": "jobs.debugger"},
                                     {"method": "EXECUTE", "resource": "jobs.coder"}],
                               rate_limit=10)
            job = LLMWorker(broker, wf, ledger, client).run("x", cap)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["step"], 2)  # died at tester, governance never ran
        finally:
            srv.shutdown()
