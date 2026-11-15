#!/usr/bin/env python3
"""Round-2 adversarial probes for Crow OS."""
import hashlib, json, sys, tempfile, time
from multiprocessing import Process
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.capabilities import Broker, CapabilityError
from crow_os.ledger import Ledger, _canon, _digest

SECRET = b"probe-secret-probe-secret-32byte"
results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())

def m1():
    # Full-rewrite forgery against a SIGNED ledger (the Kernel's):
    # attacker rewrites the whole file with fabricated entries and
    # correctly recomputed unkeyed digests, but cannot sign them.
    from crow_os.runtime import Kernel
    home = tmp()
    kernel = Kernel(home)
    kernel.run_demo("real objective")
    led = kernel.ledger
    fake = {"seq": 0, "ts": time.time(), "source": "attacker", "kind": "job.created",
            "prev_hash": None, "raw": {"job_id": "fake", "objective": "forged"}, "digest": ""}
    fake["digest"] = _digest({k: v for k, v in fake.items() if k != "digest"})
    led.path.write_text(_canon(fake) + "\n")
    v = led.verify()
    return (not v["ok"]), f"signed ledger rejects full rewrite: errors={v['errors']}"

def m2():
    # Broker state tamper: exhaust the lineage limit, edit usage back to 0
    # in broker_state.json, reopen -> if it authorizes, the state file is
    # a rate-limit reset button.
    sp = tmp() / "state.json"
    b = Broker(SECRET, state_path=sp)
    cap = b.issue("a", [{"method": "GET", "resource": "/x"}], rate_limit=1)
    b.authorize(cap, "GET", "/x")
    saved = json.loads(sp.read_text()); saved["usage"] = {}
    sp.write_text(json.dumps(saved))
    try:
        b2 = Broker(SECRET, state_path=sp)  # must refuse the forged state
        b2.authorize(cap, "GET", "/x")
        return False, "edited state file reset the rate limit"
    except CapabilityError:
        return True, "state tamper detected at load (fail closed)"

def _append_worker(path, n, q):
    import sys as _s
    _s.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from crow_os.ledger import Ledger as L
    led = L(Path(path))
    for i in range(n): led.append("proc", "x", {"i": i})

def m3():
    p = tmp() / "l.jsonl"
    procs = [Process(target=_append_worker, args=(str(p), 10, None)) for _ in range(4)]
    [pr.start() for pr in procs]; [pr.join() for pr in procs]
    v = Ledger(p).verify()
    return v["ok"] and v["entries"] == 40, f"4 processes x10 appends: {v['entries']} entries ok={v['ok']} errs={v['errors'][:2]}"

def m4():
    # Ollama response with content: null must fail closed, not crash.
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from crow_os.llm import OllamaClient
    from crow_os.workflow import Workflow
    from crow_os.worker import LLMWorker
    class Stub(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            out = json.dumps({"message": {"content": None}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out)
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        d = tmp(); led = Ledger(d / "l.jsonl"); br = Broker(SECRET); wf = Workflow(led)
        client = OllamaClient(base_url=f"http://127.0.0.1:{srv.server_address[1]}", model="stub")
        cap = br.issue("k", [{"method": "EXECUTE", "resource": "jobs.debugger"},
                             {"method": "EXECUTE", "resource": "jobs.coder"}], rate_limit=10)
        job = LLMWorker(br, wf, led, client).run("x", cap)
        return job["status"] == "failed", f"null content -> job {job['status']} (no crash)"
    finally: srv.shutdown()

def m5():
    # Revocation mid-run: cap revoked after debugger step -> coder authorize
    # must fail and the job must end failed, not completed.
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from crow_os.llm import OllamaClient
    from crow_os.workflow import Workflow
    from crow_os.worker import LLMWorker
    d = tmp(); led = Ledger(d / "l.jsonl"); br = Broker(SECRET); wf = Workflow(led)
    cap = br.issue("k", [{"method": "EXECUTE", "resource": "jobs.debugger"},
                         {"method": "EXECUTE", "resource": "jobs.coder"}], rate_limit=10)
    class Stub(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            br.revoke(cap["id"])  # revoke while the run is in flight
            out = json.dumps({"message": {"content": "plan"}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out)
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        client = OllamaClient(base_url=f"http://127.0.0.1:{srv.server_address[1]}", model="stub")
        job = LLMWorker(br, wf, led, client).run("x", cap)
        return job["status"] == "failed", f"mid-run revocation -> job {job['status']}"
    finally: srv.shutdown()

def m6():
    # Line-swap: reordering two valid entries must break verification.
    led = Ledger(tmp() / "l.jsonl")
    led.append("t", "a", {"n": 1}); led.append("t", "b", {"n": 2}); led.append("t", "c", {"n": 3})
    lines = led.path.read_text().splitlines()
    lines[0], lines[2] = lines[2], lines[0]
    led.path.write_text("\n".join(lines) + "\n")
    return not led.verify()["ok"], "reordered entries detected"

for name, fn in [("M1 full-rewrite ledger forgery", m1), ("M2 broker-state tamper resets limits", m2),
                 ("M3 cross-process appends (4x10)", m3), ("M4 Ollama null content", m4),
                 ("M5 mid-run revocation", m5), ("M6 ledger line swap", m6)]:
    probe(name, fn)
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-2 probes passed; failures: {fails}")
