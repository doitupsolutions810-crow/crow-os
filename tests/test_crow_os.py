import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crow_os.capabilities import Broker, CapabilityError
from crow_os.ledger import Ledger
from crow_os.runtime import Kernel
from crow_os.workflow import Workflow, WorkflowError

SECRET = b"test-secret-test-secret-32bytes!!"


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ledger = Ledger(Path(self.dir) / "ledger.jsonl")

    def test_roundtrip_verifies(self):
        self.ledger.append("t", "a", {"n": 1})
        self.ledger.append("t", "b", {"n": 2})
        v = self.ledger.verify()
        self.assertTrue(v["ok"], v)
        self.assertEqual(v["entries"], 2)

    def test_tamper_detected(self):
        self.ledger.append("t", "a", {"n": 1})
        text = self.ledger.path.read_text().replace('"n":1', '"n":9')
        self.ledger.path.write_text(text)
        self.assertFalse(self.ledger.verify()["ok"])

    def test_seq_gap_detected(self):
        self.ledger.append("t", "a", {})
        self.ledger.append("t", "b", {})
        lines = self.ledger.path.read_text().splitlines()
        self.ledger.path.write_text(lines[1] + "\n")  # drop seq 0
        self.assertFalse(self.ledger.verify()["ok"])

    def test_missing_ledger_is_not_ok(self):
        self.assertFalse(self.ledger.verify()["ok"])


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.broker = Broker(SECRET)
        self.rights = [{"method": "GET", "resource": "/data/x"}]

    def test_method_confusion_denied(self):
        cap = self.broker.issue("agent", self.rights)
        self.assertEqual(self.broker.authorize(cap, "GET", "/data/x")["status"], "ok")
        with self.assertRaises(CapabilityError):
            self.broker.authorize(cap, "DELETE", "/data/x")

    def test_revocation_cascades_to_children(self):
        parent = self.broker.issue("agent", self.rights)
        child = self.broker.issue("agent", self.rights, parent=parent)
        self.broker.revoke(parent["id"])
        with self.assertRaises(CapabilityError):
            self.broker.authorize(child, "GET", "/data/x")

    def test_rate_limit_is_lineage_keyed(self):
        parent = self.broker.issue("agent", self.rights, rate_limit=2)
        child = self.broker.issue("agent", self.rights, parent=parent)
        self.broker.authorize(parent, "GET", "/data/x")
        self.broker.authorize(child, "GET", "/data/x")
        with self.assertRaises(CapabilityError):
            self.broker.authorize(child, "GET", "/data/x")

    def test_attenuation_cannot_amplify(self):
        parent = self.broker.issue("agent", self.rights)
        with self.assertRaises(CapabilityError):
            self.broker.issue("agent", [{"method": "DELETE", "resource": "/data/x"}],
                              parent=parent)

    def test_tampered_capability_rejected(self):
        cap = self.broker.issue("agent", self.rights)
        cap = dict(cap, rights=[{"method": "DELETE", "resource": "/data/x"}])
        with self.assertRaises(CapabilityError):
            self.broker.authorize(cap, "DELETE", "/data/x")


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.ledger = Ledger(Path(self.dir) / "ledger.jsonl")
        self.wf = Workflow(self.ledger)

    def test_role_free_completion_rejected(self):
        job = self.wf.create_job("x")
        for _ in range(10):
            with self.assertRaises(WorkflowError):
                self.wf.report(job, "coder", True)  # debugger owns step 0
        self.assertEqual(self.wf.get(job)["status"], "running")
        self.assertEqual(self.wf.get(job)["step"], 0)

    def test_full_chain_completes_and_survives_reload(self):
        job = self.wf.create_job("x")
        for role in ["debugger", "coder", "tester", "governance"]:
            state = self.wf.report(job, role, True)
        self.assertEqual(state["status"], "completed")
        reloaded = Workflow(self.ledger)  # state rebuilt from ledger alone
        self.assertEqual(reloaded.get(job)["status"], "completed")


class KernelTests(unittest.TestCase):
    def test_demo_completes_with_verified_ledger(self):
        home = Path(tempfile.mkdtemp())
        kernel = Kernel(home)
        job = kernel.run_demo("build the OS")
        self.assertEqual(job["status"], "completed")
        self.assertTrue(kernel.ledger.verify()["ok"])


if __name__ == "__main__":
    unittest.main()

    def test_blank_objective_rejected(self):
        with self.assertRaises(WorkflowError):
            self.wf.create_job("   ")
        with self.assertRaises(WorkflowError):
            self.wf.create_job("")
