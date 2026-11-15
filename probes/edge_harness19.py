#!/usr/bin/env python3
"""Round-19 probes: connectors — a real GitHub connector proven
against a local mock of the GitHub API (identical code path; on the
operator's hardware the default endpoint is api.github.com)."""
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.runtime import Kernel
from crow_os.connectors import ConnectorError

results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())

TOKEN = "test-token-ffentlich-12345"
class MockGitHub(BaseHTTPRequestHandler):
    mode = "ok"
    seen_auth = []
    def do_GET(self):
        MockGitHub.seen_auth.append(self.headers.get("Authorization"))
        if MockGitHub.mode == "deny":
            self.send_response(401); self.end_headers(); self.wfile.write(b"{}"); return
        if self.path == "/user":
            out = json.dumps({"login": "doitupsolutions810-crow", "name": "Brandon", "public_repos": 40}).encode()
        elif self.path.startswith("/user/repos"):
            out = json.dumps([{"full_name": "doitupsolutions810-crow/crow-os"},
                              {"full_name": "doitupsolutions810-crow/Avrone"}]).encode()
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
mock = HTTPServer(("127.0.0.1", 0), MockGitHub); threading.Thread(target=mock.serve_forever, daemon=True).start()
MOCK = f"http://127.0.0.1:{mock.server_address[1]}"

class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        out = json.dumps({"models": [{"name": "m"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = json.dumps(body)
        if body.get("stream"):
            text = "repos seen: crow-os and Avrone" if "crow-os" in prompt else "nothing seen"
            payload = (json.dumps({"message": {"content": text}}).encode() + b"\n"
                       + json.dumps({"done": True}).encode() + b"\n")
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        out = json.dumps({"message": {"content": "reply"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
stub = HTTPServer(("127.0.0.1", 0), Stub); threading.Thread(target=stub.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{stub.server_address[1]}"

def configured_kernel():
    home = tmp()
    (home / "secrets").mkdir()
    (home / "secrets" / "github.token").write_text(TOKEN)
    os.chmod(home / "secrets" / "github.token", 0o600)
    k = Kernel(home)
    k.connectors.configure("github", MOCK)
    return k

def ff1():
    k = Kernel(tmp())
    st = k.connectors.status("github")
    plugins = k.plugins.list(False, connector_states=k.connectors.all_states())
    gh = [p for p in plugins if p["id"] == "github"][0]
    return st["state"] == "not_configured" and gh["state"] != "live · verified", \
        "unconfigured connector: honest not_configured state, catalog does not claim live"

def ff2():
    k = configured_kernel()
    st = k.connectors.status("github")
    plugins = k.plugins.list(False, connector_states=k.connectors.all_states())
    gh = [p for p in plugins if p["id"] == "github"][0]
    return st["state"] == "live" and gh["connected"] and gh["state"] == "live · verified", \
        f"configured + check passes -> plugin is Live · verified ({st.get('detail')})"

def ff3():
    MockGitHub.seen_auth.clear()
    k = configured_kernel()
    ag = k.agents.create("Repo Scout19", "List repos.", tools=["github.repos"],
                         extra_tools=k.connectors.tool_names())
    job = k.run_agent("Inventory the account.\nconnector: github.repos", ag["id"], base_url=STUB)
    invoked = [e for e in k.ledger.read() if e["kind"] == "tool.invoked" and e["raw"].get("tool") == "github.repos"]
    return (job["status"] == "completed" and len(invoked) == 1
            and "crow-os and Avrone" in k.outputs[job["job_id"]]["artifact"]
            and MockGitHub.seen_auth and MockGitHub.seen_auth[-1] == f"Bearer {TOKEN}"), \
        "granted connector tool runs, is attested, data reaches the agent, token sent only as auth header"

def ff4():
    k = configured_kernel()
    ag = k.agents.create("NoGrant19", "No grants.")
    job = k.run_agent("Try it.\nconnector: github.repos", ag["id"], base_url=STUB)
    invoked = [e for e in k.ledger.read() if e["kind"] == "tool.invoked" and e["raw"].get("tool") == "github.repos"]
    refused = False
    try: k.agents.create("Bad19", "x", tools=["github.repos"])  # no extra_tools -> not in catalog
    except Exception: refused = True
    return job["status"] == "completed" and not invoked and refused, \
        "connector directive without the grant is inert; unlisted grants refused at creation"

def ff5():
    MockGitHub.mode = "deny"
    try:
        k = configured_kernel()
        st = k.connectors.status("github")
        ag = k.agents.create("Repo Scout19b", "List repos.", tools=["github.repos"],
                             extra_tools=k.connectors.tool_names())
        job = k.run_agent("Inventory.\nconnector: github.repos", ag["id"], base_url=STUB)
        return (st["state"] == "unreachable" and job["status"] == "completed"
                and "nothing seen" in k.outputs[job["job_id"]]["artifact"]), \
            "API 401: state drops to unreachable, directive fails soft, no fake data"
    finally:
        MockGitHub.mode = "ok"

def ff6():
    k = configured_kernel()
    ag = k.agents.create("Repo Scout19c", "List repos.", tools=["github.repos", "github.whoami"],
                         extra_tools=k.connectors.tool_names())
    job = k.run_agent("Go.\nconnector: github.repos\nconnector: github.whoami", ag["id"], base_url=STUB)
    blob = json.dumps(k.ledger.read()) + json.dumps(k.outputs) + k.transcript(job["job_id"])
    return job["status"] == "completed" and TOKEN not in blob, \
        "the token appears nowhere: not in the ledger, outputs, or transcript"

for name, fn in [("FF1 unconfigured is honest", ff1), ("FF2 configured goes live", ff2),
                 ("FF3 granted connector tool runs", ff3), ("FF4 grant discipline", ff4),
                 ("FF5 API refusal fails soft + honest", ff5), ("FF6 token never leaks", ff6)]:
    probe(name, fn)
mock.shutdown(); stub.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-19 probes passed; failures: {fails}")
