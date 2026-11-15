#!/usr/bin/env python3
"""Round-11 probes: sub-agent spawning, devices & hosting, HUD tools switch."""
import json, os, subprocess, sys, tempfile, threading, time
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.runtime import Kernel

results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())

class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("stream"):
            payload = b"\n".join([json.dumps({"message": {"content": "sub "}}).encode(),
                                  json.dumps({"message": {"content": "reply"}}).encode(),
                                  json.dumps({"done": True}).encode()]) + b"\n"
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload))); self.end_headers()
            self.wfile.write(payload); return
        out = json.dumps({"message": {"content": "reply"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out)))
        self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
stub = HTTPServer(("127.0.0.1", 0), Stub); threading.Thread(target=stub.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{stub.server_address[1]}"

def x1():
    k = Kernel(tmp())
    k.agents.create("Helper", "You help tersely.")
    boss = k.agents.create("Boss", "You delegate.", tools=["agent.spawn"])
    job = k.run_agent("Summarize the plan.\ndelegate to helper: check the details", boss["id"], base_url=STUB)
    spawned = [e for e in k.ledger.read() if e["kind"] == "agent.spawned"]
    subs = (k.outputs.get(job["job_id"]) or {}).get("subagents") or []
    child = k.workflow.get(subs[0]["job_id"]) if subs else {}
    return (job["status"] == "completed" and len(spawned) == 1
            and spawned[0]["raw"]["parent_job_id"] == job["job_id"]
            and child.get("status") == "completed"
            and child.get("chain") == ["helper", "governance"]), \
        f"spawn attested + child completed: parent={job['status']} subs={len(subs)} child={child.get('status')}"

def x2():
    k = Kernel(tmp())
    k.agents.create("Helper", "You help.")
    plain = k.agents.create("Plain", "No grants.")
    job = k.run_agent("Do it.\ndelegate to helper: check", plain["id"], base_url=STUB)
    spawned = [e for e in k.ledger.read() if e["kind"] == "agent.spawned"]
    return job["status"] == "completed" and not spawned, "no agent.spawn grant -> delegate line is inert"

def x3():
    k = Kernel(tmp())
    k.agents.create("L3", "leaf", tools=["agent.spawn"])
    k.agents.create("L2", "mid", tools=["agent.spawn"])
    boss = k.agents.create("L1", "top", tools=["agent.spawn"])
    obj = "go\ndelegate to l2: go deeper\ndelegate to l3: leaf task"
    job = k.run_agent(obj, boss["id"], base_url=STUB)
    spawned = [e for e in k.ledger.read() if e["kind"] == "agent.spawned"]
    # L1 spawns L2 and L3 (depth 1). The delegated objective for L2 is
    # "go deeper" (no further directive) — depth cap itself is exercised
    # directly below.
    try:
        deep_parent = spawned[0]["raw"]["child_job_id"] if spawned else None
        k.spawn_subagent(deep_parent, "l3", "x", base_url=STUB)  # depth 2 ok
        k.spawn_subagent(deep_parent, "l3", "x", base_url=STUB)  # still depth 2 (sibling)
        # force depth 3: child of a depth-2 job
        d2 = [e for e in k.ledger.read() if e["kind"] == "agent.spawned"][-1]["raw"]["child_job_id"]
        k.spawn_subagent(d2, "l3", "x", base_url=STUB)
        return False, "depth 3 spawn accepted"
    except ValueError:
        return len(spawned) >= 2, f"depth cap enforced at {Kernel.MAX_SPAWN_DEPTH}; spawns attested: {len(spawned)}"

def x4():
    home = tmp()
    k = Kernel(home)
    local = k.devices.list({"reachable": True, "installed": ["m"]})[0]
    k.devices.register("Studio Mac", "device", "studio.local")
    k2 = Kernel(home)
    devices = k2.devices.list()
    return (local["kind"] == "host" and local["verified"] and local["cpus"] >= 1
            and len(devices) == 2 and devices[1]["name"] == "Studio Mac"
            and devices[1]["verified"] is False), \
        "local profile computed; registered device persists, honestly unverified"

def x5():
    k = Kernel(tmp(), allow_private_tools=True)
    ag = k.agents.create("Scout", "Fetch things.", tools=["net.fetch"])
    job = k.run_agent("read http://127.0.0.1:9/nothing", ag["id"], base_url=STUB, tools_enabled=False)
    invoked = [e for e in k.ledger.read() if e["kind"] == "tool.invoked"
               and e["raw"].get("tool") == "net.fetch"]
    return job["status"] == "completed" and not invoked, "HUD tools OFF: agent runs, zero net.fetch invocations"

def x6():
    home = tmp()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), OLLAMA_HOST=STUB)
    proc = subprocess.Popen([sys.executable, "-c",
        "import sys; sys.argv=['crow','--home',sys.argv[1],'serve','--port','8813'];"
        " from crow_os.cli import main; main()", str(home)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = "http://127.0.0.1:8813"
        for _ in range(60):
            try: urllib.request.urlopen(base + "/api/status", timeout=2); break
            except Exception: time.sleep(0.2)
        def call(method, path, payload=None):
            req = urllib.request.Request(base + path, method=method,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r: return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
        c1, devs = call("GET", "/api/devices")
        call("POST", "/api/agents", {"name": "HTTP Boss", "instructions": "Delegate.", "tools": ["agent.spawn"]})
        call("POST", "/api/agents", {"name": "HTTP Helper", "instructions": "Help."})
        c2, spawn = call("POST", "/api/spawn", {"agent": "http-helper", "objective": "help out", "parent": None})
        for _ in range(40):
            _, d = call("GET", f"/api/jobs/{spawn['job_id']}")
            if d["job"]["status"] != "running": break
            time.sleep(0.3)
        html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        return (c1 == 200 and devs["hosting"]["port"] == 8813
                and devs["devices"][0]["kind"] == "host"
                and c2 == 202 and d["job"]["status"] == "completed"
                and 'id="hud"' in html and 'id="hud-tools"' in html), \
            f"HTTP: devices={c1} hosting port={devs['hosting']['port']} spawn={c2}->{d['job']['status']} HUD served"
    finally: proc.terminate()

for name, fn in [("X1 spawn: attested parent->child, child completes", x1),
                 ("X2 no grant, no spawn", x2), ("X3 spawn depth cap", x3),
                 ("X4 devices: local profile + registry persists", x4),
                 ("X5 HUD tools OFF means zero tool calls", x5),
                 ("X6 HTTP: devices, spawn, HUD", x6)]:
    probe(name, fn)
stub.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-11 probes passed; failures: {fails}")
