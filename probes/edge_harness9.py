#!/usr/bin/env python3
"""Round-9 probes: custom agents — create, name, instruct, run, persist."""
import json, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.agents import AgentError
from crow_os.runtime import Kernel
from crow_os.workflow import WorkflowError

results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())

SEEN = {}
class Stub(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        msgs = body["messages"]
        SEEN["system"] = msgs[0]["content"]; SEEN["user"] = msgs[-1]["content"]
        stream = "stream" in self.path or body.get("stream")
        if body.get("stream"):
            lines = [json.dumps({"message": {"content": "agent "}}).encode(),
                     json.dumps({"message": {"content": "reply"}}).encode(),
                     json.dumps({"done": True}).encode()]
            payload = b"\n".join(lines) + b"\n"
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload))); self.end_headers()
            self.wfile.write(payload); return
        out = json.dumps({"message": {"content": "agent reply"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out)))
        self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass

class Target(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"AGENT-TARGET page content"
        self.send_response(200); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass

stub = HTTPServer(("127.0.0.1", 0), Stub); threading.Thread(target=stub.serve_forever, daemon=True).start()
tgt = HTTPServer(("127.0.0.1", 0), Target); threading.Thread(target=tgt.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{stub.server_address[1]}"
TGT = f"http://127.0.0.1:{tgt.server_address[1]}"

def v1():
    k = Kernel(tmp())
    a = k.agents.create("Edge Runner", "You hunt edge cases.", tools=["net.fetch"])
    listed = k.agents.list()
    return a["id"] == "edge-runner" and len(listed) == 1 and \
        k.fs.exists("/system/agents/edge-runner.json"), "created, listed, stored in CrowFS"

def v2():
    k = Kernel(tmp())
    bad = 0
    for args in [("", "x"), ("  ", "x"), ("Ok", ""), ("x" * 61, "x"),
                 ("Ok", "x" * 4001), ("Coder", "x"), ("Ok2", "x", )]:
        try:
            if len(args) == 2: k.agents.create(args[0], args[1])
            else: k.agents.create(args[0], args[1])
            bad += 0
        except AgentError: bad += 1
    try: k.agents.create("Ok3", "x", tools=["net.teleport"])
    except AgentError: bad += 1
    return bad == 7, f"invalid definitions refused: {bad}/7 (empty name/instructions, oversize, reserved role, unknown tool)"

def v3():
    k = Kernel(tmp())
    k.agents.create("Edge Runner", "You hunt edge cases.", tools=["net.fetch"])
    k.agents.create("Edge Runner", "Second one.")
    ids = [a["id"] for a in k.agents.list()]
    return ids == ["edge-runner", "edge-runner-2"], f"duplicate names get distinct ids: {ids}"

def v4():
    home = tmp()
    Kernel(home).agents.create("Persistent", "I survive restarts.")
    k2 = Kernel(home)
    return [a["name"] for a in k2.agents.list()] == ["Persistent"], "agent survives a kernel restart (stored in the FS)"

def v5():
    k = Kernel(tmp(), allow_private_tools=True)
    ag = k.agents.create("Scout", "CUSTOM-INSTRUCTIONS-MARKER: report what pages say.", tools=["net.fetch"])
    job = k.run_agent(f"read {TGT}/page and report", ag["id"], base_url=STUB)
    invoked = [e for e in k.ledger.read() if e["kind"] == "tool.invoked"
               and e["raw"].get("tool") == "net.fetch"]
    return (job["status"] == "completed" and job["chain"] == [ag["id"], "governance"]
            and "CUSTOM-INSTRUCTIONS-MARKER" in SEEN.get("system", "")
            and "AGENT-TARGET" in SEEN.get("user", "")
            and len(invoked) == 1), \
        f"agent ran: status={job['status']} chain={job.get('chain')} instructions-as-system={'CUSTOM-INSTRUCTIONS-MARKER' in SEEN.get('system','')} fetched-context={'AGENT-TARGET' in SEEN.get('user','')}"

def v6():
    k = Kernel(tmp(), allow_private_tools=True)
    ag = k.agents.create("NoTools", "Plain agent, no grants.")
    job = k.run_agent(f"read {TGT}/page", ag["id"], base_url=STUB)
    invoked = [e for e in k.ledger.read() if e["kind"] == "tool.invoked" and e["raw"].get("tool") == "net.fetch"]
    return job["status"] == "completed" and not invoked and "AGENT-TARGET" not in SEEN.get("user", ""), \
        "agent without the grant: no fetch, no fetched context, still completes under governance"

def v7():
    k = Kernel(tmp())
    try:
        k.workflow.create_job("x", chain=["rogue"])
        return False, "chain without governance accepted"
    except WorkflowError:
        return True, "no chain may skip governance — custom agents included"

def v8():
    import subprocess, time, urllib.request, urllib.error
    home = tmp()
    env = dict(__import__("os").environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"),
               OLLAMA_HOST=STUB)
    proc = subprocess.Popen([sys.executable, "-c",
        "import sys; sys.argv=['crow','--home',sys.argv[1],'serve','--port','8812'];"
        " from crow_os.cli import main; main()", str(home)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = "http://127.0.0.1:8812"
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
        c1, created = call("POST", "/api/agents", {"name": "API Agent", "instructions": "Via HTTP.", "tools": ["net.fetch"]})
        c2, listed = call("GET", "/api/agents")
        c3, bad = call("POST", "/api/agents", {"name": "", "instructions": "x"})
        c4, run = call("POST", "/api/run", {"objective": "hello agent", "agent": created["agent"]["id"]})
        jid = run["job"]["job_id"]
        for _ in range(40):
            _, d = call("GET", f"/api/jobs/{jid}")
            if d["job"]["status"] != "running": break
            time.sleep(0.3)
        c5, unknown = call("POST", "/api/run", {"objective": "hi", "agent": "ghost"})
        return (c1 == 201 and c2 == 200 and len(listed["agents"]) == 1 and c3 == 400
                and d["job"]["status"] == "completed" and c5 == 400), \
            f"HTTP: create={c1} list={c2} invalid={c3} agent-run={d['job']['status']} unknown-agent={c5}"
    finally: proc.terminate()

for name, fn in [("V1 create + list + FS storage", v1), ("V2 validation refuses bad definitions", v2),
                 ("V3 duplicate names, distinct ids", v3), ("V4 agents persist across restart", v4),
                 ("V5 agent runs: instructions are the prompt, granted tool fires", v5),
                 ("V6 ungranted tool never fires", v6), ("V7 governance is not skippable", v7),
                 ("V8 HTTP API end-to-end", v8)]:
    probe(name, fn)
stub.shutdown(); tgt.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-9 probes passed; failures: {fails}")
