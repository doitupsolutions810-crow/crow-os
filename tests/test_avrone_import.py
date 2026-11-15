"""AVRONE evidence ingress is bounded, deduplicated, and never executable."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.avrone import AvroneImportError, import_avrone_journal


class LedgerStub:
    def __init__(self):
        self.events = []
        self.good = True

    def verify(self):
        return {"ok": self.good}

    def read(self):
        return list(self.events)

    def append(self, source, kind, raw):
        entry = {"source": source, "kind": kind, "raw": raw}
        self.events.append(entry)
        return entry


class AvroneImportTests(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.path = Path(self.t.name) / "avrone.jsonl"
        self.ledger = LedgerStub()
        self.kernel = SimpleNamespace(ledger=self.ledger)

    def record(self, pid="id-1", kind="finding", **extra):
        return {"id": pid, "run_id": "test-run", "kind": kind,
                "text": "a finding", "refs": [], **extra}

    def write(self, *rows):
        self.path.write_text("".join(json.dumps(x) + "\n" for x in rows), encoding="utf-8")

    def test_import_and_replay_dedupe(self):
        self.write(self.record(), self.record("id-2", kind="conclusion"))
        first = import_avrone_journal(self.kernel, self.path)
        self.assertEqual(first["accepted"], 2)
        self.assertEqual(import_avrone_journal(self.kernel, self.path)["duplicates"], 2)
        self.assertEqual(len(self.ledger.read()), 2)
        for entry in self.ledger.read():
            self.assertEqual(entry["kind"], "avrone.observation")
            self.assertEqual(entry["source"], "integration/avrone")
            self.assertTrue(entry["raw"]["untrusted"])
            self.assertNotIn("tools", entry["raw"])

    def test_other_run_same_post_id_is_distinct(self):
        other = self.record()
        other["run_id"] = "other-run"
        self.write(self.record(), other)
        self.assertEqual(import_avrone_journal(self.kernel, self.path)["accepted"], 2)

    def test_malformed_line_has_zero_side_effects(self):
        self.path.write_text(json.dumps(self.record()) + '\n{"invalid":', encoding="utf-8")
        with self.assertRaises(AvroneImportError):
            import_avrone_journal(self.kernel, self.path)
        self.assertEqual(self.ledger.read(), [])

    def test_invalid_kind_has_zero_side_effects(self):
        self.write(self.record(), self.record("evil", kind="execute.shell"))
        with self.assertRaises(AvroneImportError):
            import_avrone_journal(self.kernel, self.path)
        self.assertEqual(self.ledger.read(), [])

    def test_invalid_id_newline_is_rejected(self):
        self.write(self.record("bad\n"))
        with self.assertRaises(AvroneImportError):
            import_avrone_journal(self.kernel, self.path)

    def test_invalid_unicode_rejected(self):
        self.path.write_text(json.dumps(self.record(text="\ud800")) + "\n")
        with self.assertRaises(AvroneImportError):
            import_avrone_journal(self.kernel, self.path)

    def test_oversized_line_rejected(self):
        self.path.write_text('z' * (128 * 1024 + 20))
        with self.assertRaises(AvroneImportError):
            import_avrone_journal(self.kernel, self.path)

    def test_oversized_file_rejected(self):
        with self.path.open("wb") as f:
            f.truncate(4 * 1024 * 1024 + 1)
        with self.assertRaises(AvroneImportError):
            import_avrone_journal(self.kernel, self.path)

    def test_symlink_denied(self):
        original = Path(self.t.name) / "target"
        original.write_text(json.dumps(self.record()))
        self.path.symlink_to(original)
        with self.assertRaises(AvroneImportError):
            import_avrone_journal(self.kernel, self.path)

    def test_hardlink_denied(self):
        original = Path(self.t.name) / "target"
        original.write_text(json.dumps(self.record()))
        os.link(original, self.path)
        with self.assertRaises(AvroneImportError):
            import_avrone_journal(self.kernel, self.path)

    def test_tampered_ledger_denies_import(self):
        self.write(self.record())
        self.ledger.good = False
        with self.assertRaisesRegex(AvroneImportError, "verification"):
            import_avrone_journal(self.kernel, self.path)
        self.assertEqual(self.ledger.read(), [])

    def test_tool_metadata_cannot_inject_commands(self):
        self.write(self.record(tools=["priv:shell"], status="completed",
                               roles=["governance"], command="rm -rf /"))
        import_avrone_journal(self.kernel, self.path)
        saved = self.ledger.read()[0]["raw"]
        self.assertEqual(saved["kind"], "finding")
        self.assertTrue(saved["untrusted"])
        for k in ("tools", "status", "roles", "command"):
            self.assertNotIn(k, saved)

    def test_compacted_journal_tombstone_skips_old_ids(self):
        self.write(self.record("removed"), self.record("live"),
                   {"tombstones": ["removed"]})
        imported = import_avrone_journal(self.kernel, self.path)
        self.assertEqual(imported["accepted"], 1)
        self.assertEqual(self.ledger.read()[0]["raw"]["id"], "live")

    def test_invalid_tombstone_fails_without_writes(self):
        self.write(self.record(), {"tombstones": ["bad\n"]})
        with self.assertRaises(AvroneImportError):
            import_avrone_journal(self.kernel, self.path)
        self.assertEqual(self.ledger.read(), [])

    def test_parallel_threads_import_each_id_once(self):
        self.write(self.record("one"), self.record("two"))
        barrier = threading.Barrier(8)
        results, errors = [], []
        def run():
            try:
                barrier.wait(timeout=5)
                results.append(import_avrone_journal(self.kernel, self.path))
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=run) for _ in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(sum(x["accepted"] for x in results), 2)
        self.assertEqual(len(self.ledger.read()), 2)

    def test_unsafe_lock_symlink_denies_import(self):
        self.write(self.record())
        ledgerfile = Path(self.t.name) / "ledger.jsonl"
        self.ledger.path = ledgerfile
        protected = Path(self.t.name) / "protected"
        protected.write_text("keep")
        (ledgerfile.parent / ".avrone-import.lock").symlink_to(protected)
        with self.assertRaises(AvroneImportError):
            import_avrone_journal(self.kernel, self.path)
        self.assertFalse(self.ledger.read())
        self.assertEqual(protected.read_text(), "keep")

    def test_101_posts_rejected_without_mutation(self):
        self.write(*[self.record(f"id-{i}") for i in range(101)])
        with self.assertRaises(AvroneImportError):
            import_avrone_journal(self.kernel, self.path)
        self.assertFalse(self.ledger.read())


if __name__ == "__main__":
    unittest.main()
