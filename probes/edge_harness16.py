#!/usr/bin/env python3
"""Round-16 probes: net.resolve, agent fs directives, transcript
edges, multi-URL objectives, hostile redirects, combined directives."""
import json, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.runtime import Kernel
from crow_os.tools import ToolRunner
from crow_os.capabilities import CapabilityError

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
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = json.dumps(body)
        if body.get("stream"):
            text = "saw quixotic-12345 in context" if "quixotic-12345" in prompt else "streamed reply"
            payload = (json.dumps({"message": {"content": text}}).encode() + b"\n"
                       + json.dumps({"done": True}).encode() + b"\n")
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        out = json.dumps({"message": {"content": "reply"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
stub = HTTPServer(("127.0.0.1", 0), Stub); threading.Thread(target=stub.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{stub.server_address[1]}"

class Target(BaseHTTPRequestHandler):
    hits = 0
    def do_GET(self):
        Target.hits += 1
        if self.path == "/redir-file":
            self.send_response(302); self.send_header("Location", "file:///etc/passwd")
            self.end_headers(); return
        body = b"<html><body>page</body></html>"
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass
target = HTTPServer(("127.0.0.1", 0), Target); threading.Thread(target=target.serve_forever, daemon=True).start()
TGT = f"http://127.0.0.1:{target.server_address[1]}"

def cc1():
    k = Kernel(tmp(), allow_private_tools=True)
    cap = k.broker.issue("t", [{"method": "TOOL", "resource": "net.resolve"}], ttl_seconds=60, rate_limit=5)
    r = ToolRunner(k.broker, cap, allow_private=True).resolve("localhost")
    cap2 = k.broker.issue("t", [{"method": "TOOL", "resource": "net.fetch"}], ttl_seconds=60, rate_limit=5)
    refused = False
    try: ToolRunner(k.broker, cap2, allow_private=True).resolve("localhost")
    except CapabilityError: refused = True
    return "127.0.0.1" in r["ips"] and refused, f"resolve granted -> {r['ips']}; ungranted -> refused"

def cc2():
    k = Kernel(tmp())
    k.fs.write_file("/home/user/notes.txt", "the secret word is quixotic-12345")
    ag = k.agents.create("Reader16", "Read files.", tools=["fs.read"])
    job = k.run_agent("Summarize the notes.\nfs read: /home/user/notes.txt", ag["id"], base_url=STUB)
    invoked = [e for e in k.ledger.read() if e["kind"] == "tool.invoked" and e["raw"].get("tool") == "fs.read"]
    return (job["status"] == "completed" and len(invoked) == 1
            and "quixotic-12345" in k.outputs[job["job_id"]]["artifact"]), \
        "fs.read directive: file content reached the agent, attested"

def cc3():
    k = Kernel(tmp())
    k.fs.write_file("/home/user/notes.txt", "quixotic-12345")
    ag = k.agents.create("NoGrant16", "No grants.")
    job = k.run_agent("Read.\nfs read: /home/user/notes.txt", ag["id"], base_url=STUB)
    invoked = [e for e in k.ledger.read() if e["kind"] == "tool.invoked" and e["raw"].get("tool") == "fs.read"]
    return job["status"] == "completed" and not invoked, "fs directive without the grant is inert"

def cc4():
    k = Kernel(tmp())
    ag = k.agents.create("Reader16b", "Read files.", tools=["fs.read", "fs.list"])
    job = k.run_agent("Check.\nfs read: /home/user/missing.txt\nfs list: /home/user", ag["id"], base_url=STUB)
    kinds = [e["raw"].get("tool") for e in k.ledger.read() if e["kind"] == "tool.invoked"
             and e["raw"].get("tool") != "fs.write"]  # fs.write here is artifact persistence
    return job["status"] == "completed" and kinds == ["fs.list"], \
        f"missing file fails soft; the list directive still runs (invoked: {kinds})"

def cc5():
    k = Kernel(tmp())
    job = k.run_llm("needs a model", base_url="http://127.0.0.1:9")
    md = k.transcript(job["job_id"])
    return job["status"] == "failed" and "**failed**" in md and "Steps" in md, \
        "transcript of a failed job shows the failure and its steps"

def cc6():
    Target.hits = 0
    k = Kernel(tmp(), allow_private_tools=True)
    ag = k.agents.create("Fetcher16", "Fetch pages.", tools=["net.fetch"])
    job = k.run_agent(f"Read {TGT}/a and {TGT}/b", ag["id"], base_url=STUB)
    invoked = [e for e in k.ledger.read() if e["kind"] == "tool.invoked" and e["raw"].get("tool") == "net.fetch"]
    return job["status"] == "completed" and len(invoked) == 1 and Target.hits == 1, \
        "two URLs in one objective: exactly the first is fetched (documented contract)"

def cc7():
    k = Kernel(tmp(), allow_private_tools=True)
    ag = k.agents.create("Fetcher16b", "Fetch pages.", tools=["net.fetch"])
    job = k.run_agent(f"Read {TGT}/redir-file", ag["id"], base_url=STUB)
    return job["status"] == "failed" and "root:" not in json.dumps(k.outputs), \
        "redirect to file:// fails closed; no host file content anywhere"

def cc8():
    k = Kernel(tmp(), allow_private_tools=True)
    k.fs.write_file("/home/user/data.txt", "quixotic-12345")
    k.agents.create("Helper16", "Help.")
    boss = k.agents.create("Boss16", "Everything.", tools=["net.fetch", "fs.read", "agent.spawn"])
    obj = (f"Full sweep {TGT}/page\nfs read: /home/user/data.txt\n"
           "delegate to helper16: assist")
    job = k.run_agent(obj, boss["id"], base_url=STUB)
    tools_used = sorted({e["raw"].get("tool") for e in k.ledger.read()
                         if e["kind"] == "tool.invoked" and e["raw"].get("job_id") == job["job_id"]
                         and e["raw"].get("tool") != "fs.write"})
    spawned = [e for e in k.ledger.read() if e["kind"] == "agent.spawned"]
    return (job["status"] == "completed" and tools_used == ["fs.read", "net.fetch"]
            and len(spawned) == 1), f"combined directives in one run: {tools_used} + {len(spawned)} spawn"

for name, fn in [("CC1 net.resolve gated both ways", cc1), ("CC2 agent fs.read directive", cc2),
                 ("CC3 fs directive needs the grant", cc3), ("CC4 fs failures fail soft", cc4),
                 ("CC5 failed job transcript", cc5), ("CC6 multi-URL contract", cc6),
                 ("CC7 redirect to file:// refused", cc7), ("CC8 combined directives", cc8)]:
    probe(name, fn)
stub.shutdown(); target.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-16 probes passed; failures: {fails}")
