"""Cross-batch adversarial integrity and compaction tests for AVRONE importer."""
from __future__ import annotations
import json
import random
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.avrone import AvroneImportError, import_avrone_journal


class AuditLedger:
    def __init__(self):
        self.records = []
        self.fail_after = None
        self.calls = 0

    def verify(self):
        return {"ok": True}

    def read(self):
        return list(self.records)

    def append(self, source, kind, raw):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise OSError("injected storage outage")
        self.records.append({"source": source, "kind": kind, "raw": raw})


class EvidenceBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.file = self.dir / "avrone.jsonl"
        self.ledger = AuditLedger()
        self.kernel = SimpleNamespace(ledger=self.ledger)

    def post(self, i, text="same", run="unit"):
        return {"id": f"p-{i}", "run_id": run, "kind": "finding",
                "text": text, "agent": "agent", "origin_node": "node", "refs": []}

    def write(self, rows):
        self.file.write_text("".join(json.dumps(x, ensure_ascii=True) + "\n"
                                     for x in rows))

    def ingest(self):
        return import_avrone_journal(self.kernel, self.file)

    def test_exact_100_records_then_tombstone(self):
        self.write([self.post(i) for i in range(100)] +
                   [{"tombstones": ["p-99"]}])
        info = self.ingest()
        self.assertEqual(info["accepted"], 99)
        self.assertNotIn("p-99", {r["raw"]["id"] for r in self.ledger.read()})

    def test_exact_100_records_then_multiple_tombstones(self):
        self.write([self.post(i) for i in range(100)] +
                   [{"tombstones": ["p-98"]}, {"tombstones": ["p-99"]}])
        self.assertEqual(self.ingest()["accepted"], 98)

    def test_post_101_rejected_even_if_later_tombstoned(self):
        self.write([self.post(i) for i in range(101)] +
                   [{"tombstones": ["p-100"]}])
        with self.assertRaises(AvroneImportError):
            self.ingest()
        self.assertEqual(self.ledger.records, [])

    def test_conflicting_post_rejected_without_partial_writes(self):
        self.write([self.post(1, "first"), self.post(2),
                    self.post(1, "second")])
        with self.assertRaisesRegex(AvroneImportError, "conflicting"):
            self.ingest()
        self.assertEqual(self.ledger.records, [])

    def test_identical_duplicate_one_admission(self):
        self.write([self.post(1), self.post(1), self.post(2)])
        info = self.ingest()
        self.assertEqual((info["accepted"], info["duplicates"]), (2, 1))
        self.assertEqual(len(self.ledger.records), 2)

    def test_existing_conflict_rejected_without_new_admissions(self):
        self.write([self.post(1, "original")])
        self.ingest()
        self.write([self.post(2), self.post(1, "forged")])
        with self.assertRaisesRegex(AvroneImportError, "conflicting"):
            self.ingest()
        self.assertEqual([e["raw"]["id"] for e in self.ledger.records], ["p-1"])

    def test_existing_identical_replay_dedupes(self):
        self.write([self.post(1, "original")])
        self.ingest()
        info = self.ingest()
        self.assertEqual((info["accepted"], info["duplicates"]), (0, 1))

    def test_unknown_fields_cannot_create_false_conflict(self):
        self.write([self.post(1)])
        self.ingest()
        self.write([dict(self.post(1), status="COMPLETED",
                         privileged=True, command="execute")])
        self.assertEqual(self.ingest()["duplicates"], 1)
        self.assertEqual(len(self.ledger.records), 1)

    def test_same_id_different_run_is_distinct(self):
        self.write([self.post(1, run="A"), self.post(1, run="B")])
        self.assertEqual(self.ingest()["accepted"], 2)

    def test_same_id_different_origin_is_conflict(self):
        first = self.post(1)
        other = dict(first, origin_node="other")
        self.write([first, other])
        with self.assertRaises(AvroneImportError):
            self.ingest()
        self.assertEqual(self.ledger.records, [])

    def test_two_conflicting_stored_records_deny_import(self):
        self.write([self.post(3)])
        self.ledger.records = [
            {"kind": "avrone.observation", "raw": self.post(1, "a")},
            {"kind": "avrone.observation", "raw": self.post(1, "b")},
        ]
        with self.assertRaisesRegex(AvroneImportError, "conflicting"):
            self.ingest()
        self.assertEqual(len(self.ledger.records), 2)

    def test_malformed_prior_identity_denied(self):
        self.write([self.post(3)])
        self.ledger.records = [{"kind": "avrone.observation",
                                "raw": {"run_id": ["bad"], "id": "p-1"}}]
        with self.assertRaisesRegex(AvroneImportError, "identity"):
            self.ingest()
        self.assertEqual(len(self.ledger.records), 1)

    def test_malformed_prior_body_denied(self):
        self.write([self.post(3)])
        self.ledger.records = [{"kind": "avrone.observation", "raw": []}]
        with self.assertRaisesRegex(AvroneImportError, "existing"):
            self.ingest()

    def test_tombstoned_conflict_not_admitted(self):
        self.write([self.post(1, "a"), self.post(1, "b"), self.post(2),
                    {"tombstones": ["p-1"]}])
        self.assertEqual(self.ingest()["accepted"], 1)
        self.assertEqual(self.ledger.records[0]["raw"]["id"], "p-2")

    def test_after_storage_failure_replay_adds_only_missing(self):
        self.write([self.post(1), self.post(2), self.post(3)])
        self.ledger.fail_after = 1
        with self.assertRaises(OSError):
            self.ingest()
        self.assertEqual(len(self.ledger.records), 1)
        self.ledger.fail_after = None
        info = self.ingest()
        self.assertEqual((info["accepted"], info["duplicates"]), (2, 1))
        self.assertEqual(len(self.ledger.records), 3)

    def test_racing_identical_imports_are_deduped(self):
        self.write([self.post(i) for i in range(8)])
        barrier = threading.Barrier(12)
        errors, results = [], []

        def worker():
            try:
                barrier.wait(timeout=5)
                results.append(self.ingest())
            except BaseException as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(errors, [])
        self.assertEqual(sum(x["accepted"] for x in results), 8)
        self.assertEqual(len(self.ledger.records), 8)

    def test_300_seeded_revisions_preserve_immutability(self):
        rng = random.Random(2026111504)
        for _ in range(300):
            self.ledger.records = []
            self.write([self.post(1, "original")])
            self.ingest()
            earlier = len(self.ledger.records)
            change = rng.choice(["text", "agent", "origin_node",
                                 "kind", "refs", "unknown"])
            x = self.post(1, "original")
            if change == "text":
                x["text"] = "mutated"
            elif change == "agent":
                x["agent"] = "mutated"
            elif change == "origin_node":
                x["origin_node"] = "mutated"
            elif change == "kind":
                x["kind"] = "correction"
            elif change == "refs":
                x["refs"] = ["anchor"]
            else:
                x["irrelevant"] = "ignored"
            self.write([self.post(2), x])
            if change == "unknown":
                self.assertEqual(self.ingest()["accepted"], 1)
            else:
                with self.assertRaises(AvroneImportError):
                    self.ingest()
            self.assertEqual(len(self.ledger.records),
                             earlier + (1 if change == "unknown" else 0))


if __name__ == "__main__":
    unittest.main()
