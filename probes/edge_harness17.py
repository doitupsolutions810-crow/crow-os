#!/usr/bin/env python3
"""Round-17 probes: the wired hypotheticals — agent memory, routines,
fleet dispatch."""
import json, os, subprocess, sys, tempfile, threading, time
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.runtime import Kernel
from crow_os.routines import RoutineError

results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())

LAST = {"prompt_len": 0}
class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        out = json.dumps({"models": [{"name": "m"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = json.dumps(body)
        LAST["prompt_len"] = len(prompt)
        if body.get("stream"):
            text = "I recall cobalt-7 from memory" if "cobalt-7" in prompt else "noted"
            payload = (json.dumps({"message": {"content": text}}).encode() + b"\n"
                       + json.dumps({"done": True}).encode() + b"\n")
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        out = json.dumps({"message": {"content": "reply"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
stub = HTTPServer(("127.0.0.1", 0), Stub); threading.Thread(target=stub.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{stub.server_address[1]}"

def dd1():
    k = Kernel(tmp())
    ag = k.agents.create("Keeper17", "Keep notes.")
    j1 = k.run_agent("Log this.\nremember: the launch code is cobalt-7", ag["id"], base_url=STUB)
    mem = k.agent_memory_read(ag["id"])
    j2 = k.run_agent("What is the launch code?", ag["id"], base_url=STUB)
    artifact2 = k.outputs[j2["job_id"]]["artifact"]
    remembered = [e for e in k.ledger.read() if e["kind"] == "agent.remembered"]
    return (j1["status"] == "completed" and "cobalt-7" in mem
            and "cobalt-7" in artifact2 and len(remembered) == 1), \
        "remember: persists, is attested, and is recalled in a later run"

def dd2():
    k = Kernel(tmp())
    ag = k.agents.create("Keeper17b", "Keep notes.")
    k.agent_memory_append(ag["id"], "x" * 9000 + "\n")
    k.run_agent("hello", ag["id"], base_url=STUB)
    return LAST["prompt_len"] < 6000, f"memory in context is bounded (prompt {LAST['prompt_len']} chars despite 9k memory)"

def dd3():
    k = Kernel(tmp())
    ag = k.agents.create("Ticker17", "Tick.")
    r = k.create_routine("Heartbeat", ag["id"], "Say the status", 60)
    due_now = k.routines.due(now=time.time() + 3600)
    job = k.fire_routine(r, base_url=STUB)
    fired = [e for e in k.ledger.read() if e["kind"] == "routine.fired"]
    after = k.routines.get(r["id"])
    not_due = k.routines.due(now=time.time())
    refused = False
    try: k.create_routine("Bad", ag["id"], "x", 5)
    except RoutineError: refused = True
    return (len(due_now) == 1 and job["status"] == "completed" and len(fired) == 1
            and after["runs"] == 1 and not not_due and refused), \
        "routine due-check, firing (attested), bookkeeping, interval floor"

def dd4():
    k = Kernel(tmp())
    for n in ("Alpha17", "Beta17", "Gamma17"):
        k.agents.create(n, f"You are {n}.")
    job = k.fleet_run("Assess the situation", ["alpha17", "beta17", "gamma17"], base_url=STUB)
    members = [e for e in k.ledger.read() if e["kind"] == "fleet.member"]
    out = k.outputs[job["job_id"]]
    refused = False
    try: k.fleet_run("x", ["alpha17", "ghost"], base_url=STUB)
    except Exception: refused = True
    return (job["status"] == "completed" and len(members) == 3
            and len(out["fleet"]) == 3
            and all(f["status"] == "completed" for f in out["fleet"]) and refused), \
        "fleet: 3 members attested, all complete, parent completes; ghost fleet refused up front"

def dd5():
    k = Kernel(tmp(), allow_private_tools=True)
    k.agents.create("Good17", "Be good.")
    k.agents.create("Bad17", "Fetch.", tools=["net.fetch"])
    # the bad member's objective fetch targets a dead port via the shared
    # objective? objectives are shared; instead fail via model: give the
    # fleet a base_url that works, then revoke by using a member whose
    # run hits the file:// redirect — simpler: dead Ollama for everyone
    # would fail all. Use per-member sabotage: bad agent name resolves,
    # but point the whole fleet at the stub and make one member a
    # duplicate chain? Fallback: fleet where one member is an agent
    # whose instructions make the stub fail is not available; assert the
    # all-or-nothing rule via a member deleted mid-flight is racy.
    # Deterministic alternative: fleet_run with a member agent whose
    # job chain is valid but whose run_agent raises because the agent
    # was deleted after validation is racy too. Skip sabotage; verify
    # the parent fails when a member fails by monkeypatching run_agent.
    orig = k.run_agent
    def flaky(objective, agent_id, **kw):
        if agent_id == "bad17":
            jid = kw.get("job_id")
            k.workflow.report(jid, "bad17", False, "sabotaged")
            return k.workflow.get(jid)
        return orig(objective, agent_id, **kw)
    k.run_agent = flaky
    job = k.fleet_run("Joint op", ["good17", "bad17"], base_url=STUB)
    k.run_agent = orig
    return job["status"] == "failed" and "1/2" in json.dumps(k.ledger.read()), \
        "fleet parent fails honestly when a member fails"

def dd6():
    home = tmp()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), OLLAMA_HOST=STUB)
    proc = subprocess.Popen([sys.executable, "-c",
        "import sys; sys.argv=['crow','--home',sys.argv[1],'serve','--port','8818'];"
        " from crow_os.cli import main; main()", str(home)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = "http://127.0.0.1:8818"
        for _ in range(60):
            try: urllib.request.urlopen(base + "/api/status", timeout=2); break
            except Exception: time.sleep(0.2)
        def call(method, path, payload=None):
            req = urllib.request.Request(base + path, method=method,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=20) as r: return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
        call("POST", "/api/agents", {"name": "HTTP One", "instructions": "One."})
        call("POST", "/api/agents", {"name": "HTTP Two", "instructions": "Two."})
        c1, r = call("POST", "/api/routines", {"name": "Pulse", "agent": "http-one", "objective": "check in", "interval_seconds": 600})
        c2, lst = call("GET", "/api/routines")
        c3, fleet = call("POST", "/api/fleet", {"objective": "joint assessment", "agents": ["http-one", "http-two"]})
        for _ in range(40):
            _, d = call("GET", f"/api/jobs/{fleet['job_id']}")
            if d["job"]["status"] != "running": break
            time.sleep(0.3)
        c4, mem = call("GET", "/api/agents/http-one/memory")
        return (c1 == 201 and c2 == 200 and len(lst["routines"]) == 1
                and c3 == 202 and d["job"]["status"] == "completed"
                and c4 == 200 and "joint assessment" in mem["memory"]), \
            f"HTTP: routines {c1}/{c2}, fleet {c3} -> {d['job']['status']}, memory endpoint {c4} (auto-note landed)"
    finally: proc.terminate()

for name, fn in [("DD1 agent memory: remember + recall", dd1), ("DD2 memory context bounded", dd2),
                 ("DD3 routines fire when due", dd3), ("DD4 fleet dispatch", dd4),
                 ("DD5 fleet fails honestly", dd5), ("DD6 HTTP: routines, fleet, memory", dd6)]:
    probe(name, fn)
stub.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-17 probes passed; failures: {fails}")
