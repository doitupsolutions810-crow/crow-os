#!/usr/bin/env python3
"""Round-4 probes: the RUNNING OS (live server on :8792) + kernel edges."""
import json, sys, tempfile, time, urllib.request, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.capabilities import Broker, CapabilityError
from crow_os.workflow import Workflow, WorkflowError
from crow_os.ledger import Ledger

SECRET = b"probe-secret-probe-secret-32byte"
B = "http://127.0.0.1:8792"
results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def req(method, path, body=None, raw=None, base=B):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
def tmp(): return Path(tempfile.mkdtemp())

def p2():
    before = {j["job_id"] for j in req("GET", "/api/jobs")[1]["jobs"]}
    code, _ = req("POST", "/api/run", {"objective": "llm mode with no ollama", "mode": "llm"})
    if code != 202: return False, f"llm run -> {code}"
    deadline = time.time() + 30
    while time.time() < deadline:
        jobs = req("GET", "/api/jobs")[1]["jobs"]
        new = [j for j in jobs if j["job_id"] not in before]
        if new and new[0]["status"] != "running":
            return new[0]["status"] == "failed", f"llm job ended {new[0]['status']} (fail-closed through the UI stack)"
        time.sleep(0.5)
    return False, "llm job never settled"

def p3():
    code, _ = req("GET", "/api/run")
    return code == 404, f"GET /api/run -> {code}"

def p4():
    code, _ = req("POST", "/api/nope", {"x": 1})
    return code == 404, f"POST unknown path -> {code}"

def p5():
    c0, d0 = req("GET", "/api/ledger?limit=0")
    c9, d9 = req("GET", "/api/ledger?limit=99999")
    return len(d0["entries"]) == 1 and len(d9["entries"]) >= 5, f"limit=0 -> {len(d0['entries'])} entry (clamped), limit=99999 -> {len(d9['entries'])}"

def p6():
    code, _ = req("POST", "/api/run", {"objective": "   \n\t  "})
    return code == 400, f"whitespace-only objective -> {code}"

def p7():
    code, d = req("POST", "/api/run", {"objective": "y" * 10_000})
    if code != 202: return False, f"10000-char objective -> {code}"
    jid = d["job"]["job_id"]
    deadline = time.time() + 15
    while time.time() < deadline:
        j = req("GET", f"/api/jobs/{jid}")[1]["job"]
        if j["status"] != "running":
            return j["status"] == "completed", f"max-size objective completed={j['status']}"
        time.sleep(0.3)
    return False, "max-size job never settled"

def p8():
    # Cross-broker: B shares the secret but not the state file.
    spa, spb = tmp() / "a.json", tmp() / "b.json"
    a = Broker(SECRET, state_path=spa)
    cap = a.issue("s", [{"method": "GET", "resource": "/x"}], rate_limit=1)
    a.authorize(cap, "GET", "/x")  # exhaust on A
    b = Broker(SECRET, state_path=spb)
    try:
        b.authorize(cap, "GET", "/x")
        return True, "no crash; B treats it as a fresh lineage budget (documented: one home = one state file)"
    except CapabilityError as e:
        return True, f"no crash; B denied: {e}"

def p9():
    wf = Workflow(Ledger(tmp() / "l.jsonl"))
    try:
        wf.create_job("   ")
        return False, "blank objective accepted as a job"
    except WorkflowError:
        return True, "blank objective rejected at the workflow layer"

def p10():
    b = Broker(SECRET)
    parent = b.issue("s", [{"method": "GET", "resource": "/x"}], rate_limit=100)
    chain = [parent]
    for _ in range(10):
        chain.append(b.issue("s", [{"method": "GET", "resource": "/x"}], parent=chain[-1]))
    b.revoke(parent["id"])
    try:
        b.authorize(chain[-1], "GET", "/x")
        return False, "depth-10 descendant survived root revocation"
    except CapabilityError:
        return True, "root revocation reaches depth 10"

def p11():
    led = Ledger(tmp() / "l.jsonl")
    led.append("t", "job.created", {"job_id": "a", "objective": "b"})
    led.path.write_text("")  # external truncation
    led.append("t", "job.created", {"job_id": "c", "objective": "d"})
    v = led.verify()
    return v["ok"] and v["entries"] == 1, f"post-truncation append: ok={v['ok']} entries={v['entries']} (history loss is visible as a restarted chain)"

def p12():
    ids = set()
    for _ in range(3):
        code, d = req("POST", "/api/run", {"objective": "same objective three times"})
        ids.add(d["job"]["job_id"])
    deadline = time.time() + 15
    while time.time() < deadline:
        if all(req("GET", f"/api/jobs/{j}")[1]["job"]["status"] == "completed" for j in ids):
            break
        time.sleep(0.3)
    done = all(req("GET", f"/api/jobs/{j}")[1]["job"]["status"] == "completed" for j in ids)
    return len(ids) == 3 and done, f"3 identical objectives -> {len(ids)} distinct jobs, all completed={done}"

for name, fn in [("P2 llm mode fails closed via API", p2), ("P3 GET on POST route 404", p3),
                 ("P4 unknown POST path 404", p4), ("P5 ledger limit clamps", p5),
                 ("P6 whitespace objective 400", p6), ("P7 max-size objective completes", p7),
                 ("P8 cross-broker cap (no crash)", p8), ("P9 blank objective at workflow layer", p9),
                 ("P10 depth-10 lineage revocation", p10), ("P11 external ledger truncation", p11),
                 ("P12 duplicate objectives distinct jobs", p12)]:
    probe(name, fn)
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-4 probes passed; failures: {fails}")
