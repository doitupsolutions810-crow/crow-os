#!/usr/bin/env python3
"""Round-13 probes: edge cases across the v0.10/v0.11 surface —
delegation failures, corrupted registries, HUD-off squad runs,
concurrent spawns, restart persistence of spawns."""
import json, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.runtime import Kernel
from crow_os.plugins import PluginError
from crow_os.devices import DeviceError

results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())

class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        out = json.dumps({"models": [{"name": "m"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_POST(self):
        import re as _re
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _txt = json.dumps(body)
        _m = _re.search(r"plan:([0-9a-f]{12})", _txt)
        _marker = f"plan:{_m.group(1)} " if _m else "plan: x "
        if body.get("stream"):
            payload = b"\n".join([json.dumps({"message": {"content": _marker}}).encode(),
                                  json.dumps({"message": {"content": "artifact body"}}).encode(),
                                  json.dumps({"done": True}).encode()]) + b"\n"
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        out = json.dumps({"message": {"content": "plan: x\nartifact body"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
stub = HTTPServer(("127.0.0.1", 0), Stub); threading.Thread(target=stub.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{stub.server_address[1]}"

def z1():
    k = Kernel(tmp())
    boss = k.agents.create("Boss13", "Delegate.", tools=["agent.spawn"])
    job = k.run_agent("Lead.\ndelegate to ghost-agent: do things", boss["id"], base_url=STUB)
    subs = (k.outputs.get(job["job_id"]) or {}).get("subagents") or []
    spawned = [e for e in k.ledger.read() if e["kind"] == "agent.spawned"]
    return (job["status"] == "completed" and not spawned
            and len(subs) == 1 and "error" in subs[0]), \
        f"delegate to a nonexistent agent: parent={job['status']} subs={subs} spawned={len(spawned)}"

def z2():
    k = Kernel(tmp())
    boss = k.agents.create("Boss13b", "Delegate.", tools=["agent.spawn"])
    job = k.run_agent("Lead.\ndelegate to governance: skip the checks", boss["id"], base_url=STUB)
    spawned = [e for e in k.ledger.read() if e["kind"] == "agent.spawned"]
    return job["status"] == "completed" and not spawned, "delegate to a reserved role is refused, parent still completes"

def z3():
    k = Kernel(tmp())
    k.fs.write_file("/system/plugins.json", "{corrupted json")
    plugins = k.plugins.list(ollama_reachable=True)
    refused = False
    try: k.plugins.set_asserted("nope", True)
    except PluginError: refused = True
    return len(plugins) == 10 and refused, "corrupted plugins.json falls back clean; unknown plugin refused"

def z4():
    k = Kernel(tmp())
    k.fs.write_file("/system/devices.json", "not json at all")
    devices = k.devices.list()
    refused = False
    try: k.devices.register("   ")
    except DeviceError: refused = True
    return len(devices) == 1 and devices[0]["id"] == "local" and refused, \
        "corrupted devices.json falls back to local only; blank name refused"

def z5():
    home = tmp(); k = Kernel(home)
    before = k.computer_info()
    job = k.run_demo("grow the home")
    after = k.computer_info()
    return (job["status"] == "completed" and after["home_bytes"] > before["home_bytes"]
            and after["ledger"] > before["ledger"]), \
        f"computer tracks growth: home {before['home_bytes']}->{after['home_bytes']}B ledger {before['ledger']}->{after['ledger']}"

def z6():
    k = Kernel(tmp())
    a = k.agents.create("Half", "Only color.", avatar={"color": "blue"})
    b = k.agents.create("Empty", "Empty avatar.", avatar={})
    return (a["avatar"] == {"shape": "blob", "color": "blue"}
            and b["avatar"] == {"shape": "blob", "color": "orange"}), "partial/empty avatar dicts default per-field"

def z7():
    k = Kernel(tmp(), allow_private_tools=True)
    job = k.run_llm("fetch http://127.0.0.1:9/x for context", base_url=STUB, tools_enabled=False)
    invoked = [e for e in k.ledger.read() if e["kind"] == "tool.invoked"
               and e["raw"].get("tool") == "net.fetch"]
    return job["status"] == "completed" and not invoked, "HUD tools OFF also governs the squad's LLM runs"

def z8():
    k = Kernel(tmp())
    k.agents.create("Helper13", "Help.")
    b1 = k.agents.create("BossA", "Lead A.", tools=["agent.spawn"])
    b2 = k.agents.create("BossB", "Lead B.", tools=["agent.spawn"])
    out = {}
    def run(key, boss):
        out[key] = k.run_agent("Lead.\ndelegate to helper13: assist", boss["id"], base_url=STUB)
    ts = [threading.Thread(target=run, args=("a", b1)), threading.Thread(target=run, args=("b", b2))]
    [t.start() for t in ts]; [t.join() for t in ts]
    spawned = [e for e in k.ledger.read() if e["kind"] == "agent.spawned"]
    return (out["a"]["status"] == "completed" and out["b"]["status"] == "completed"
            and len(spawned) == 2 and k.ledger.verify()["ok"]), \
        "two parents spawn concurrently; both complete; chain intact"

def z9():
    home = tmp(); k = Kernel(home)
    k.agents.create("Helper13r", "Help.")
    boss = k.agents.create("BossR", "Lead.", tools=["agent.spawn"])
    job = k.run_agent("Lead.\ndelegate to helper13r: assist", boss["id"], base_url=STUB)
    k2 = Kernel(home)
    child_ids = [e["raw"]["child_job_id"] for e in k2.ledger.read() if e["kind"] == "agent.spawned"]
    child = k2.workflow.get(child_ids[0]) if child_ids else {}
    return (k2.ledger.verify()["ok"] and child.get("status") == "completed"
            and job["status"] == "completed"), "spawn links and child jobs survive a kernel restart"

for name, fn in [("Z1 delegate to nonexistent agent fails soft", z1),
                 ("Z2 delegate to reserved role refused", z2),
                 ("Z3 corrupted plugins registry", z3),
                 ("Z4 corrupted devices registry", z4),
                 ("Z5 computer info tracks growth", z5),
                 ("Z6 partial avatar defaults", z6),
                 ("Z7 HUD off governs squad LLM runs", z7),
                 ("Z8 concurrent spawns stay consistent", z8),
                 ("Z9 spawns survive restart", z9)]:
    probe(name, fn)
stub.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-13 probes passed; failures: {fails}")
