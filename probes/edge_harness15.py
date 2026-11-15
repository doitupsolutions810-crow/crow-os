#!/usr/bin/env python3
"""Round-15 probes: chat/agent outputs — transcripts, persistence,
isolation between runs, the live streaming buffer."""
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

SLOW = {"delay": 0.0}
class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        out = json.dumps({"models": [{"name": "m"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("stream"):
            chunks = ["alpha ", "beta ", "gamma"]
            payload = b""
            for c in chunks:
                payload += json.dumps({"message": {"content": c}}).encode() + b"\n"
            payload += json.dumps({"done": True}).encode() + b"\n"
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload))); self.end_headers()
            for line in payload.splitlines(keepends=True):
                self.wfile.write(line); self.wfile.flush()
                if SLOW["delay"]: time.sleep(SLOW["delay"])
            return
        out = json.dumps({"message": {"content": "single reply"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
stub = HTTPServer(("127.0.0.1", 0), Stub); threading.Thread(target=stub.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{stub.server_address[1]}"

def bb1():
    k = Kernel(tmp())
    k.agents.create("Helper15", "Help tersely.")
    boss = k.agents.create("Boss15", "Lead and delegate.", tools=["agent.spawn"])
    job = k.run_agent("Ship the report.\ndelegate to helper15: check it", boss["id"], base_url=STUB)
    md = k.transcript(job["job_id"])
    return (job["status"] == "completed" and "Ship the report" in md
            and "alpha beta gamma" in md and "agent.spawned" not in md
            and "helper15" in md and "Sub-agents" in md
            and "governance" in md and "completed" in md), \
        "transcript carries the chat, the agent output, spawn links and steps"

def bb2():
    home = tmp(); k = Kernel(home)
    ag = k.agents.create("Scribe15", "Write things down.")
    job = k.run_agent("Write the summary", ag["id"], base_url=STUB)
    k2 = Kernel(home)  # restart: outputs sink is empty
    md = k2.transcript(job["job_id"])
    return ("alpha beta gamma" in md and "Documents" in md
            and k2.workflow.get(job["job_id"])["status"] == "completed"), \
        "after restart the transcript still carries the agent output (from the persisted artifact)"

def bb3():
    k = Kernel(tmp())
    try:
        k.transcript("deadbeef0000"); return False, "transcript of unknown job did not raise"
    except KeyError:
        return True, "unknown job -> KeyError (HTTP maps it to 404)"

def bb4():
    k = Kernel(tmp())
    ag = k.agents.create("Echo15", "Reply.")
    j1 = k.run_agent("first conversation", ag["id"], base_url=STUB)
    j2 = k.run_agent("second conversation", ag["id"], base_url=STUB)
    o1, o2 = k.outputs[j1["job_id"]], k.outputs[j2["job_id"]]
    return (o1["artifact"] == "alpha beta gamma" and o2["artifact"] == "alpha beta gamma"
            and j1["job_id"] != j2["job_id"]
            and "first conversation" in k.transcript(j1["job_id"])
            and "second conversation" not in k.transcript(j1["job_id"])), \
        "two chats with one agent stay isolated per job"

def bb5():
    k = Kernel(tmp())
    ag = k.agents.create("Streamer15", "Stream.")
    SLOW["delay"] = 0.6
    holder = {}
    def run():
        holder["job"] = k.run_agent("stream slowly", ag["id"], base_url=STUB)
    t = threading.Thread(target=run); t.start()
    seen = ""
    deadline = time.time() + 10
    while time.time() < deadline:
        for jid, out in list(k.outputs.items()):
            if out.get("live"):
                seen = out["live"]; break
        if "alpha" in seen: break
        time.sleep(0.1)
    t.join(); SLOW["delay"] = 0.0
    return "alpha" in seen and holder["job"]["status"] == "completed", \
        f"live buffer showed partial output mid-run ({seen!r})"

def bb6():
    home = tmp()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), OLLAMA_HOST=STUB)
    proc = subprocess.Popen([sys.executable, "-c",
        "import sys; sys.argv=['crow','--home',sys.argv[1],'serve','--port','8817'];"
        " from crow_os.cli import main; main()", str(home)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = "http://127.0.0.1:8817"
        for _ in range(60):
            try: urllib.request.urlopen(base + "/api/status", timeout=2); break
            except Exception: time.sleep(0.2)
        def call(method, path, payload=None):
            req = urllib.request.Request(base + path, method=method,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=15) as r: return r.status, r.read().decode()
            except urllib.error.HTTPError as e: return e.code, e.read().decode()
        call("POST", "/api/agents", {"name": "HTTP Scribe", "instructions": "Write."})
        _, run = call("POST", "/api/run", {"objective": "write it down", "agent": "http-scribe"})
        jid = json.loads(run)["job"]["job_id"]
        for _ in range(40):
            _, d = call("GET", f"/api/jobs/{jid}")
            if json.loads(d)["job"]["status"] != "running": break
            time.sleep(0.3)
        code, md = call("GET", f"/api/jobs/{jid}/transcript")
        code404, _ = call("GET", "/api/jobs/deadbeef0000/transcript")
        return (code == 200 and "write it down" in md and "alpha beta gamma" in md
                and code404 == 404), f"HTTP transcript {code} with chat+output; unknown job {code404}"
    finally: proc.terminate()

for name, fn in [("BB1 transcript completeness", bb1), ("BB2 transcript survives restart", bb2),
                 ("BB3 unknown job transcript", bb3), ("BB4 per-job output isolation", bb4),
                 ("BB5 live streaming buffer", bb5), ("BB6 transcript over HTTP", bb6)]:
    probe(name, fn)
stub.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-15 probes passed; failures: {fails}")
