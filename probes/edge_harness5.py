#!/usr/bin/env python3
"""Round-5 wiring probes: one chain, signed verify, uniform run API."""
import json, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error
from http.server import ThreadingHTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.runtime import Kernel
from crow_os.server import HomeServer, make_handler
from crow_os.workflow import WorkflowError

results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())
SRC = str(Path(__file__).resolve().parents[1] / "src")

def cli(home, *args):
    return subprocess.run([sys.executable, "-m", "crow_os.cli", "--home", str(home), *args],
                          capture_output=True, text=True,
                          env={"PYTHONPATH": SRC, "PATH": "/usr/bin:/bin"})

def q1():
    home = tmp()
    r = cli(home, "demo", "--objective", "verify me")
    if r.returncode != 0: return False, f"demo rc={r.returncode}"
    v = cli(home, "verify")
    out = json.loads(v.stdout)
    if not (v.returncode == 0 and out["ok"] and out.get("signed")):
        return False, f"verify on signed home: rc={v.returncode} out={out}"
    # tamper: rewrite one entry's objective, keep digests as-is
    led = home / "ledger.jsonl"
    lines = led.read_text().splitlines()
    e = json.loads(lines[0]); e["raw"]["objective"] = "forged objective"
    lines[0] = json.dumps(e)
    led.write_text("\n".join(lines) + "\n")
    v2 = cli(home, "verify")
    return v2.returncode == 1, f"tampered signed home -> CLI verify rc={v2.returncode} (signed path catches it)"

def boot():
    kernel = Kernel(tmp())
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(HomeServer(kernel)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return kernel, httpd, f"http://127.0.0.1:{httpd.server_address[1]}"

def req(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def q2():
    kernel, httpd, base = boot()
    try:
        code, d = req(base, "POST", "/api/run", {"objective": "llm please", "mode": "llm"})
        if not d.get("job"): return False, "llm run returned no job (old discovery path)"
        jid = d["job"]["job_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            j = req(base, "GET", f"/api/jobs/{jid}")[1]["job"]
            if j["status"] != "running":
                return j["status"] == "failed", f"llm job returned immediately, settled {j['status']}"
            time.sleep(0.4)
        return False, "llm job never settled"
    finally: httpd.shutdown()

def q3():
    # CLI demo and server demo must produce the identical entry shape:
    # one chain implementation, two front doors.
    home = tmp(); cli(home, "demo", "--objective", "shape check")
    k1 = Kernel(home)
    cli_job = k1.workflow.jobs()[0]
    cli_entries = [(e["kind"], e["raw"].get("role")) for e in k1.ledger.read()
                   if e["raw"].get("job_id") == cli_job["job_id"]]
    kernel, httpd, base = boot()
    try:
        _, d = req(base, "POST", "/api/run", {"objective": "shape check"})
        jid = d["job"]["job_id"]
        deadline = time.time() + 15
        while time.time() < deadline:
            if req(base, "GET", f"/api/jobs/{jid}")[1]["job"]["status"] == "completed": break
            time.sleep(0.3)
        srv_entries = [(e["kind"], e["raw"].get("role")) for e in kernel.ledger.read()
                       if e["raw"].get("job_id") == jid]
        return cli_entries == srv_entries, f"cli {cli_entries} == server {srv_entries}"
    finally: httpd.shutdown()

def q4():
    k = Kernel(tmp())
    jid = k.workflow.create_job("pre-created")
    k.run_demo("pre-created", job_id=jid)
    created = [e for e in k.ledger.read()
               if e["kind"] == "job.created" and e["raw"]["job_id"] == jid]
    return len(created) == 1 and k.workflow.get(jid)["status"] == "completed", \
        f"exactly one job.created for pre-created job: {len(created)}"

def q5():
    k = Kernel(tmp())
    try:
        k.run_demo("x", job_id="doesnotexist")
        return False, "run_demo accepted a nonexistent pre-created job id"
    except WorkflowError:
        return True, "nonexistent job_id rejected"

for name, fn in [("Q1 CLI verify checks signatures", q1), ("Q2 llm run returns job at once", q2),
                 ("Q3 one chain shape, CLI == server", q3), ("Q4 pre-created job not duplicated", q4),
                 ("Q5 bogus pre-created id rejected", q5)]:
    probe(name, fn)
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-5 probes passed; failures: {fails}")
