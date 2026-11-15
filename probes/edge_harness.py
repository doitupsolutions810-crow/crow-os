#!/usr/bin/env python3
"""Adversarial edge-case harness for Crow OS v0.2. Prints PASS/FAIL per probe."""
import json, sys, tempfile, threading, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.capabilities import Broker, CapabilityError
from crow_os.ledger import Ledger
from crow_os.workflow import Workflow, WorkflowError

SECRET = b"probe-secret-probe-secret-32byte"
results = []
def probe(name, fn):
    try:
        ok, note = fn()
    except Exception as e:
        ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok, note))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")

def tmp():
    return Path(tempfile.mkdtemp())

# ---------- LEDGER ----------
def l1():
    led = Ledger(tmp() / "l.jsonl"); led.append("t", "a", {"n": 1})
    led.path.write_text(led.path.read_text().replace('"n":1', '"n":2'))
    led.append("t", "b", {"n": 3})  # append after tamper
    v = led.verify()
    return (not v["ok"]), f"tamper still detected after append: {v['errors']}"
def l2():
    led = Ledger(tmp() / "l.jsonl"); led.append("t", "a", {})
    lines = led.path.read_text() + led.path.read_text()  # duplicate seq 0
    led.path.write_text(lines)
    return (not led.verify()["ok"]), "duplicate seq detected"
def l3():
    led = Ledger(tmp() / "l.jsonl"); led.append("t", "a", {"emoji": "🔥", "nul": "a"})
    return led.verify()["ok"], "unicode payload verifies"
def l4():
    led = Ledger(tmp() / "l.jsonl"); led.append("t", "a", {"big": "x" * 1_000_000})
    return led.verify()["ok"], "1MB entry verifies"
def l5():
    p = tmp() / "l.jsonl"; p.write_text("   \n\n")
    v = Ledger(p).verify()
    return v["ok"] and v["entries"] == 0, f"whitespace-only file: {v}"
def l6():
    p = tmp() / "l.jsonl"; p.write_text('{"seq": 0, "digest": "x"}\nNOT JSON\n')
    v = Ledger(p).verify()
    return (not v["ok"]) and any("malformed" in e for e in v["errors"]), f"errors={v['errors']}"
def l7():
    led = Ledger(tmp() / "l.jsonl")
    errs = []
    def app(n):
        try: led.append("t", "x", {"n": n})
        except Exception as e: errs.append(e)
    ts = [threading.Thread(target=app, args=(i,)) for i in range(20)]
    [t.start() for t in ts]; [t.join() for t in ts]
    v = led.verify()
    return v["ok"] and v["entries"] == 20 and not errs, f"20 threaded appends: {v} errs={errs[:1]}"
def l8():
    led = Ledger(tmp() / "l.jsonl"); led.append("t", "a", {"digest": "spoof", "seq": 99})
    return led.verify()["ok"], "digest/seq keys inside raw are inert"

# ---------- CAPABILITIES ----------
def c1():
    b = Broker(SECRET); cap = b.issue("a", [{"method": "GET", "resource": "/x"}], ttl_seconds=0.05)
    time.sleep(0.07)
    try: b.authorize(cap, "GET", "/x"); return False, "expired cap authorized"
    except CapabilityError: return True, "expiry enforced"
def c2():
    b = Broker(SECRET)
    parent = b.issue("a", [{"method": "GET", "resource": "/x"}], ttl_seconds=0.05, rate_limit=5)
    child = b.issue("a", [{"method": "GET", "resource": "/x"}], parent=parent, ttl_seconds=300)
    time.sleep(0.07)
    try:
        b.authorize(child, "GET", "/x")
        return False, "child outlived expired parent (duration amplification)"
    except CapabilityError: return True, "child bounded by parent expiry"
def c3():
    b = Broker(SECRET); cap = b.issue("a", [])
    try: b.authorize(cap, "GET", "/x"); return False, "empty-rights cap authorized"
    except CapabilityError: return True, "empty rights authorize nothing"
def c4():
    b = Broker(SECRET); cap = b.issue("a", [{"method": "get", "resource": "/x"}])
    try: b.authorize(cap, "GET", "/x"); return False, "method case-insensitive (loose)"
    except CapabilityError: return True, "method match is exact/case-sensitive"
def c5():
    b = Broker(SECRET); cap = b.issue("a", [{"method": "GET", "resource": "/x"}])
    cap2 = dict(cap); cap2["sig"] = cap["sig"].upper()
    try: b.authorize(cap2, "GET", "/x"); return False, "uppercased sig accepted"
    except CapabilityError: return True, "sig is exact"
def c6():
    b = Broker(SECRET); cap = b.issue("a", [{"method": "GET", "resource": "/x"}])
    cap2 = dict(cap); cap2["extra"] = "field"
    try: b.authorize(cap2, "GET", "/x"); return False, "extra field tolerated"
    except CapabilityError: return True, "any added field breaks signature"
def c7():
    sp = tmp() / "broker_state.json"
    b1 = Broker(SECRET, state_path=sp)
    cap = b1.issue("a", [{"method": "GET", "resource": "/x"}])
    b1.revoke(cap["id"])
    b2 = Broker(SECRET, state_path=sp)  # second instance, same home
    try:
        b2.authorize(cap, "GET", "/x")
        return False, "revocation invisible to second broker on same home"
    except CapabilityError: return True, "revocation shared across instances via state file"
def c8():
    b = Broker(SECRET)
    try: b.issue("a", [{"method": "GET", "resource": "/x"}], ttl_seconds=0); return False, "ttl 0 issued"
    except CapabilityError: pass
    try: b.issue("a", [{"method": "GET", "resource": "/x"}], rate_limit=-1); return False, "negative rate issued"
    except CapabilityError: return True, "ttl/rate bounds enforced at issue"
def c9():
    b = Broker(SECRET)
    parent = b.issue("a", [{"method": "GET", "resource": "/x"}], rate_limit=3)
    child = b.issue("a", [{"method": "GET", "resource": "/x"}], parent=parent, rate_limit=100)
    for _ in range(3): b.authorize(child, "GET", "/x")
    try: b.authorize(child, "GET", "/x"); return False, "child rate_limit=100 overrode lineage limit 3"
    except CapabilityError: return True, "lineage limit governs despite child asking 100"

# ---------- WORKFLOW ----------
def w1():
    wf = Workflow(Ledger(tmp() / "l.jsonl")); job = wf.create_job("x")
    for r in ["debugger", "coder", "tester", "governance"]: wf.report(job, r, True)
    try: wf.report(job, "debugger", True); return False, "report on completed job accepted"
    except WorkflowError: return True, "completed job is terminal"
def w2():
    wf = Workflow(Ledger(tmp() / "l.jsonl")); job = wf.create_job("x")
    wf.report(job, "debugger", False, "boom")
    try: wf.report(job, "coder", True); return False, "failed job advanced"
    except WorkflowError: return True, "failed job is terminal"
def w3():
    wf = Workflow(Ledger(tmp() / "l.jsonl"))
    a, bjob = wf.create_job("A"), wf.create_job("B")
    wf.report(a, "debugger", True); wf.report(bjob, "debugger", True)
    wf.report(a, "coder", True); wf.report(bjob, "coder", False)
    sa, sb = wf.get(a), wf.get(bjob)
    return sa["step"] == 2 and sb["status"] == "failed", f"interleaved jobs independent: {sa['step']},{sb['status']}"
def w4():
    wf = Workflow(Ledger(tmp() / "l.jsonl")); job = wf.create_job("x")
    wf.report(job, "debugger", True)
    try: wf.report(job, "debugger", True); return False, "duplicate step accepted"
    except WorkflowError: return True, "duplicate step rejected (role moved on)"
def w5():
    led = Ledger(tmp() / "l.jsonl"); wf = Workflow(led); job = wf.create_job("x")
    for r in ["debugger", "coder", "tester"]: wf.report(job, r, True)
    led.path.write_text(led.path.read_text().replace('"objective":"x"', '"objective":"y"'))
    ok = not led.verify()["ok"]
    return ok, "tampered ledger fails verify before governance would pass it"

for name, fn in [("L1 append-after-tamper", l1), ("L2 duplicate seq", l2), ("L3 unicode payload", l3),
                 ("L4 1MB entry", l4), ("L5 whitespace-only file", l5), ("L6 malformed line", l6),
                 ("L7 concurrent appends", l7), ("L8 spoof keys in raw", l8),
                 ("C1 expiry", c1), ("C2 child outlives parent", c2), ("C3 empty rights", c3),
                 ("C4 method case", c4), ("C5 sig case", c5), ("C6 extra field", c6),
                 ("C7 cross-instance revocation", c7), ("C8 issue bounds", c8), ("C9 child rate override", c9),
                 ("W1 completed terminal", w1), ("W2 failed terminal", w2), ("W3 interleaved jobs", w3),
                 ("W4 duplicate step", w4), ("W5 tamper blocks governance", w5)]:
    probe(name, fn)
fails = [r for r in results if not r[1]]
print(f"\n{len(results) - len(fails)}/{len(results)} probes passed; failures: {[f[0] for f in fails]}")
