"""Cross-process importer lock tests with a shared file-backed ledger adapter."""
from __future__ import annotations
import concurrent.futures
import json
import multiprocessing
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.avrone import AvroneImportError, import_avrone_journal


class FileLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def verify(self):
        try:
            for row in self.read():
                if not isinstance(row, dict):
                    return {"ok": False}
            return {"ok": True}
        except (OSError, ValueError):
            return {"ok": False}

    def read(self):
        return [json.loads(line) for line in self.path.read_text().splitlines()
                if line.strip()]

    def append(self, src, kind, raw):
        event = json.dumps({"source": src, "kind": kind, "raw": raw}, sort_keys=True)
        with self.path.open("a") as stream:
            stream.write(event + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def _worker(args):
    ledger, source = args
    return import_avrone_journal(
        SimpleNamespace(ledger=FileLedger(ledger)), source)


class FileLockedImport(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.TemporaryDirectory()
        self.addCleanup(self.t.cleanup)
        self.root = Path(self.t.name)
        self.path = self.root / "journal.jsonl"
        self.ledgerpath = self.root / "ledger.jsonl"

    def write(self, n=15):
        self.path.write_text("".join(json.dumps({
            "id": f"id-{i}", "run_id": "group", "kind": "finding",
            "text": "sound", "refs": []}) + "\n" for i in range(n)))

    def test_eight_processes_produce_each_event_exactly_once(self):
        self.write(15)
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=8, mp_context=multiprocessing.get_context("spawn")) as pool:
            results = list(pool.map(_worker,
                                    [(str(self.ledgerpath), str(self.path))] * 8))
        self.assertEqual(sum(x["accepted"] for x in results), 15)
        self.assertEqual(sorted(x["accepted"] for x in results), [0]*7 + [15])
        events = FileLedger(self.ledgerpath).read()
        self.assertEqual(len(events), 15)
        self.assertEqual(len({x["raw"]["id"] for x in events}), 15)

    def test_replay_dedupe_after_new_process(self):
        self.write(4)
        self.assertEqual(_worker((self.ledgerpath, self.path))["accepted"], 4)
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
            results = list(pool.map(_worker,
                                    [(str(self.ledgerpath), str(self.path))] * 2))
        self.assertEqual(sum(x["accepted"] for x in results), 0)
        self.assertEqual(sum(x["duplicates"] for x in results), 8)

    def test_lock_hardlink_fails_closed(self):
        self.write(1)
        FileLedger(self.ledgerpath)
        original = self.root / "original"
        original.write_text("protected")
        os.link(original, self.root / ".avrone-import.lock")
        with self.assertRaisesRegex(AvroneImportError, "unsafe"):
            _worker((self.ledgerpath, self.path))
        self.assertEqual(original.read_text(), "protected")
        self.assertEqual(FileLedger(self.ledgerpath).read(), [])

    def test_lock_directory_fails_closed(self):
        self.write(1)
        FileLedger(self.ledgerpath)
        (self.root / ".avrone-import.lock").mkdir()
        with self.assertRaises(AvroneImportError):
            _worker((self.ledgerpath, self.path))
        self.assertEqual(FileLedger(self.ledgerpath).read(), [])

    def test_corrupt_prior_file_denies_new_evidence(self):
        self.write(3)
        self.ledgerpath.write_text("{partial\n")
        with self.assertRaisesRegex(AvroneImportError, "verification"):
            _worker((self.ledgerpath, self.path))
        self.assertEqual(self.ledgerpath.read_text(), "{partial\n")


if __name__ == "__main__":
    unittest.main()
