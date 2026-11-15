#!/usr/bin/env python3
"""Round-14 probes: the HTTP surface under stress and at its edges."""
import json, os, subprocess, sys, tempfile, threading, time
import urllib.request, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.runtime import Kernel
from crow_os.capabilities import CapabilityError
from crow_os.fs import FSError
from crow_os.tools import FileTools

results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())

HOME = tmp()
env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
proc = subprocess.Popen([sys.executable, "-c",
    "import sys; sys.argv=['crow','--home',sys.argv[1],'serve','--port','8815'];"
    " from crow_os.cli import main; main()", str(HOME)],
    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
BASE = "http://127.0.0.1:8815"
def call(method, path, payload=None, raw=None):
    data = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
    req = urllib.request.Request(BASE + path, method=method, data=data,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return r.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read()
        try: return e.code, json.loads(body)
        except Exception: return e.code, {}
def wait_job(job_id, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        _, d = call("GET", f"/api/jobs/{job_id}")
        if d["job"]["status"] != "running": return d["job"]
        time.sleep(0.25)
    raise TimeoutError(job_id)
for _ in range(60):
    try: urllib.request.urlopen(BASE + "/api/status", timeout=2); break
    except Exception: time.sleep(0.2)

def aa1():
    code, d = call("GET", "/api/jobs/deadbeef0000")
    code2, _ = call("GET", "/api/status")
    return code == 404 and code2 == 200, f"unknown job -> {code}, server alive"

def aa2():
    _, run = call("POST", "/api/run", {"objective": "sse probe", "mode": "demo"})
    jid = run["job"]["job_id"]
    req = urllib.request.Request(BASE + f"/api/jobs/{jid}/stream")
    events = []
    with urllib.request.urlopen(req, timeout=20) as r:
        for line in r:
            line = line.decode().strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
            if events and events[-1].get("job", {}).get("status") in ("completed", "failed"):
                break
    return events and events[-1]["job"]["status"] == "completed", \
        f"SSE delivered {len(events)} events, final={events[-1]['job']['status']}"

def aa3():
    ids = []
    def start(i):
        _, run = call("POST", "/api/run", {"objective": f"parallel {i}", "mode": "demo"})
        ids.append(run["job"]["job_id"])
    ts = [threading.Thread(target=start, args=(i,)) for i in range(5)]
    [t.start() for t in ts]; [t.join() for t in ts]
    done = [wait_job(j)["status"] for j in ids]
    _, st = call("GET", "/api/status")
    return all(s == "completed" for s in done) and st["ledger"]["ok"], \
        f"5 parallel HTTP runs: {done}, ledger ok"

def aa4():
    _, run = call("POST", "/api/run", {"objective": "needs a model", "mode": "llm"})
    job = wait_job(run["job"]["job_id"])
    return job["status"] == "failed", "llm mode with Ollama down fails closed over HTTP"

def aa5():
    code, a = call("POST", "/api/agents", {"name": "Tëst Üser 🚀", "instructions": "Réponds en français. Teste les limites — ünïcode ✓"})
    _, lst = call("GET", "/api/agents")
    kept = [x for x in lst["agents"] if x["id"] == a["agent"]["id"]]
    _, run = call("POST", "/api/run", {"objective": "Vérifie les émojis 🚀✓ et les accents", "mode": "demo"})
    job = wait_job(run["job"]["job_id"])
    return (code == 201 and kept and kept[0]["instructions"].startswith("Réponds en français")
            and job["status"] == "completed"), \
        f"unicode agent ({a.get('agent',{}).get('id')}) stored verbatim; emoji objective -> {job['status']}"

def aa6():
    c1, _ = call("POST", "/api/agents", {"name": "x" * 500, "instructions": "ok"})
    c2, _ = call("POST", "/api/agents", {"name": "Boundary", "instructions": "y" * 4001})
    c3, _ = call("POST", "/api/agents", {"name": "Boundary OK", "instructions": "y" * 4000})
    return c1 == 400 and c2 == 400 and c3 == 201, f"name 500 -> {c1}, instructions 4001 -> {c2}, 4000 -> {c3}"

def aa7():
    _, a = call("POST", "/api/agents", {"name": "Ephemeral", "instructions": "Short-lived."})
    aid = a["agent"]["id"]
    code, _ = call("DELETE", f"/api/agents/{aid}")
    c2, _ = call("POST", "/api/run", {"objective": "talk to ghost", "agent": aid})
    _, lst = call("GET", "/api/agents")
    gone = all(x["id"] != aid for x in lst["agents"])
    return code == 200 and c2 == 400 and gone, f"delete -> {code}, run deleted agent -> {c2}, unlisted"

def aa8():
    k = Kernel(tmp())
    cap = k.broker.issue("crow-os/kernel", [{"method": "TOOL", "resource": "fs.write"},
                                            {"method": "TOOL", "resource": "fs.read"}], ttl_seconds=60, rate_limit=10)
    tools = FileTools(k.broker, cap, k.fs)
    refused = False
    try: tools.write("/home/user/big.txt", "z" * 300_000)
    except FSError: refused = True
    exists = k.fs.exists("/home/user/big.txt")
    return refused and not exists, "300KB write refused by the fs cap, nothing stored"

def aa9():
    k = Kernel(tmp())
    mint_refused = False
    try:
        k.broker.issue("crow-os/kernel", [{"method": "EXECUTE", "resource": "jobs.run"}],
                       ttl_seconds=-5, rate_limit=5)
    except CapabilityError:
        mint_refused = True
    cap = k.broker.issue("crow-os/kernel", [{"method": "EXECUTE", "resource": "jobs.run"}],
                         ttl_seconds=1, rate_limit=5)
    k.broker.authorize(cap, "EXECUTE", "jobs.run")  # works while fresh
    time.sleep(1.2)
    expired_refused = False
    try: k.broker.authorize(cap, "EXECUTE", "jobs.run")
    except CapabilityError: expired_refused = True
    return mint_refused and expired_refused, \
        "expired caps refused twice: cannot be minted, and die on schedule"

def aa10():
    code, d = call("POST", "/api/run", {"objective": "q" * 50_000, "mode": "demo"})
    _, run = call("POST", "/api/run", {"objective": "q" * 10_000, "mode": "demo"})
    job = wait_job(run["job"]["job_id"], timeout=60)
    code2, _ = call("GET", "/api/status")
    return (code == 400 and "too long" in d.get("error", "")
            and job["status"] == "completed" and code2 == 200), \
        f"50k objective -> clean 400 ({d.get('error')}); 10k boundary completes"

for name, fn in [("AA1 unknown job 404s cleanly", aa1), ("AA2 SSE stream completes", aa2),
                 ("AA3 five parallel HTTP runs", aa3), ("AA4 llm fail-closed over HTTP", aa4),
                 ("AA5 unicode names + emoji objectives", aa5), ("AA6 length boundaries exact", aa6),
                 ("AA7 delete-then-run refused", aa7), ("AA8 fs write cap enforced", aa8),
                 ("AA9 expired capability refused", aa9), ("AA10 50k objective, server survives", aa10)]:
    probe(name, fn)
proc.terminate()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-14 probes passed; failures: {fails}")
