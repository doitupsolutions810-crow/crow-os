"""Regression gates for crow-os security findings GAP-013/014/015.

Source-review findings of 2026-11-21 became failing tests here:
- GAP-013: malformed, foreign-lineage, expired and revoked parent
  capabilities are refused with CapabilityError (never KeyError etc.)
- GAP-014: concurrent authorize() calls can never exceed the lineage
  rate limit; file-backed usage is shared correctly between brokers
- GAP-015: production mode refuses the deterministic path-derived dev
  secret and starts only with provisioned key material (>= 32 bytes)
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crow_os.capabilities import Broker, CapabilityError
from crow_os.runtime import Kernel, KernelError

SECRET = b"gate-regression-secret-gate-32byte"
RIGHTS = [{"method": "GET", "resource": "/data/x"}]


class Gap013ParentValidationTests(unittest.TestCase):
    def test_foreign_lineage_parent_refused(self):
        # Same key, two brokers: the capability verifies, but the parent
        # id was never recorded in this broker's lineage map.
        b1 = Broker(SECRET)
        b2 = Broker(SECRET)
        parent = b1.issue("agent", RIGHTS)
        with self.assertRaises(CapabilityError):
            b2.issue("agent", RIGHTS, parent=parent)

    def test_foreign_signature_refused(self):
        b1 = Broker(SECRET)
        b2 = Broker(b"other-broker-secret-key!!")
        parent = b1.issue("agent", RIGHTS)
        with self.assertRaises(CapabilityError):
            b2.issue("agent", RIGHTS, parent=parent)

    def test_malformed_parent_missing_fields_refused(self):
        b = Broker(SECRET)
        parent = b.issue("agent", RIGHTS)
        for drop in ("rights", "expires", "sig", "id"):
            bad = {k: v for k, v in parent.items() if k != drop}
            with self.assertRaises(CapabilityError,
                                   msg=f"missing {drop} must refuse"):
                b.issue("agent", RIGHTS, parent=bad)

    def test_malformed_parent_bad_rights_refused(self):
        b = Broker(SECRET)
        parent = b.issue("agent", RIGHTS)
        for junk in ("not-a-list", [{"method": "GET"}],
                     [{"method": 5, "resource": "/data/x"}]):
            bad = dict(parent)
            bad.pop("sig")
            bad["rights"] = junk
            bad["sig"] = b._sign(bad)  # valid signature, illegal shape
            with self.assertRaises(CapabilityError):
                b.issue("agent", RIGHTS, parent=bad)

    def test_expired_parent_refused_at_issue_time(self):
        b = Broker(SECRET)
        parent = b.issue("agent", RIGHTS, ttl_seconds=1)
        time.sleep(1.1)
        self.assertGreater(time.time(), parent["expires"])
        with self.assertRaises(CapabilityError):
            b.issue("agent", RIGHTS, parent=parent)

    def test_revoked_parent_cannot_spawn_children(self):
        b = Broker(SECRET)
        parent = b.issue("agent", RIGHTS)
        b.revoke(parent["id"])
        with self.assertRaises(CapabilityError):
            b.issue("agent", RIGHTS, parent=parent)

    def test_authorize_never_leaks_raw_errors_on_malformed_caps(self):
        b = Broker(SECRET)
        cap = b.issue("agent", RIGHTS)
        def mutate(c, fn):
            bad = dict(c)
            bad.pop("sig")
            fn(bad)
            bad["sig"] = b._sign(bad)  # valid signature, illegal shape
            return bad
        cases = [
            lambda d: d.pop("root"),
            lambda d: d.pop("expires"),
            lambda d: d.update(root=123),
            lambda d: d.update(expires="soon"),
            lambda d: d.update(rate_limit=None),
            lambda d: d.update(rights="GET /data/x"),
        ]
        for fn in cases:
            with self.assertRaises(CapabilityError):
                b.authorize(mutate(cap, fn), "GET", "/data/x")


class Gap014AtomicUsageTests(unittest.TestCase):
    def test_concurrent_authorizations_never_exceed_limit(self):
        b = Broker(SECRET)
        cap = b.issue("agent", RIGHTS, rate_limit=5)
        barrier = threading.Barrier(16)
        lock = threading.Lock()
        results = {"ok": 0, "refused": 0}

        def hit():
            try:
                barrier.wait(timeout=5)
            except threading.BrokenBarrierError:
                pass
            try:
                b.authorize(cap, "GET", "/data/x")
                with lock:
                    results["ok"] += 1
            except CapabilityError:
                with lock:
                    results["refused"] += 1

        threads = [threading.Thread(target=hit) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(results["ok"], 5, results)
        self.assertEqual(results["refused"], 11, results)

    def test_file_backed_consumption_is_shared_between_brokers(self):
        d = tempfile.mkdtemp()
        state_path = Path(d) / "broker_state.json"
        b1 = Broker(SECRET, state_path=state_path)
        cap = b1.issue("agent", RIGHTS, rate_limit=3)
        b2 = Broker(SECRET, state_path=state_path)
        for _ in range(3):
            b2.authorize(cap, "GET", "/data/x")
        # b1 must reload the persisted usage under the lock, so the
        # lineage limit holds across brokers sharing the state file.
        with self.assertRaises(CapabilityError):
            b1.authorize(cap, "GET", "/data/x")

    def test_state_file_forgery_still_fails_closed(self):
        d = tempfile.mkdtemp()
        state_path = Path(d) / "broker_state.json"
        b1 = Broker(SECRET, state_path=state_path)
        b1.issue("agent", RIGHTS)
        state_path.write_text('{"revoked": [], "lineage": {}, "usage": {}, '
                              '"root_limits": {}}')  # unsigned edit
        with self.assertRaises(CapabilityError):
            Broker(SECRET, state_path=state_path)


class Gap015ProductionKeyCustodyTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k)
                       for k in ("CROW_OS_MODE", "CROW_OS_SIGNING_KEY")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_production_refuses_without_provisioned_key(self):
        home = Path(tempfile.mkdtemp())
        with self.assertRaises(KernelError):
            Kernel(home, production=True)

    def test_production_refuses_even_with_existing_dev_secret(self):
        home = Path(tempfile.mkdtemp())
        Kernel(home)  # creates the path-derived dev secret file
        self.assertTrue((home / "broker.secret").exists())
        with self.assertRaises(KernelError):
            Kernel(home, production=True)

    def test_production_starts_with_provisioned_key(self):
        home = Path(tempfile.mkdtemp())
        os.environ["CROW_OS_SIGNING_KEY"] = "11" * 32
        kernel = Kernel(home, production=True)
        kernel.run_demo("gate check")
        self.assertTrue(kernel.ledger.verify()["ok"])

    def test_production_refuses_short_key(self):
        home = Path(tempfile.mkdtemp())
        os.environ["CROW_OS_SIGNING_KEY"] = "11" * 8
        with self.assertRaises(KernelError):
            Kernel(home, production=True)

    def test_dev_mode_still_uses_path_derived_secret(self):
        home1 = Path(tempfile.mkdtemp())
        home2 = Path(tempfile.mkdtemp())
        k1 = Kernel(home1)
        k2 = Kernel(home2)
        self.assertFalse(k1._production)
        self.assertNotEqual((home1 / "broker.secret").read_bytes(),
                            (home2 / "broker.secret").read_bytes())


if __name__ == "__main__":
    unittest.main()
