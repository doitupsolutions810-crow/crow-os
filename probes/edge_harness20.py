#!/usr/bin/env python3
"""Round-20 probes: the combined surfaces — the gap flagged after
v0.16.0, when federation, connectors, routines and agent memory had
each passed their own round but had never been probed together:
a routine that writes memory, memory written by a federated run, a
federated run that uses a connector, a connector pointed somewhere
it should not be, fleet memory, and a routine that uses a connector.
"""
import json, os, subprocess, sys, tempfile, threading, time
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TOKEN = "test-token-ffentlich-20001"
MARKERS = ["garnet-9", "cobalt-7", "quartz-3"]

class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        out = json.dumps({"models": [{"name": "m"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = json.dumps(body)
        if body.get("stream"):
            hit = next((m for m in MARKERS if m in prompt), None)
            text = f"I recall {hit} from memory" if hit else \
                ("repos seen: crow-os and Avrone" if "crow-os" in prompt else "noted")
            payload = (json.dumps({"message": {"content": text}}).encode() + b"\n"
                       + json.dumps({"done": True}).encode() + b"\n")
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        out = json.dumps({"message": {"content": "reply"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
stub = HTTPServer(("127.0.0.1", 0), Stub); threading.Thread(target=stub.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{stub.server_address[1]}"
os.environ["OLLAMA_HOST"] = STUB
os.environ["CROW_OS_NAME"] = "alpha-home"

class MockGitHub(BaseHTTPRequestHandler):
    def do_GET(self):
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

class WrongShape(BaseHTTPRequestHandler):
    """A live HTTP service that is NOT GitHub: /user returns an
    Ollama-style payload, /user/repos returns a bare object."""
    def do_GET(self):
        if self.path == "/user":
            out = json.dumps({"models": [{"name": "m"}]}).encode()
        elif self.path.startswith("/user/repos"):
            out = json.dumps({"not": "repos"}).encode()
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
wrong = HTTPServer(("127.0.0.1", 0), WrongShape); threading.Thread(target=wrong.serve_forever, daemon=True).start()
WRONG = f"http://127.0.0.1:{wrong.server_address[1]}"

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
from crow_os.runtime import Kernel

results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())

def gg1():
    k = Kernel(tmp())
    ag = k.agents.create("Ticker20", "Tick.")
    r = k.create_routine("Memory Pulse", ag["id"],
                         "remember: the routine code is garnet-9\nReport status", 60)
    job = k.fire_routine(r, base_url=STUB)
    mem = k.agent_memory_read(ag["id"])
    j2 = k.run_agent("What is the routine code?", ag["id"], base_url=STUB)
    fired = [e for e in k.ledger.read() if e["kind"] == "routine.fired"]
    remembered = [e for e in k.ledger.read() if e["kind"] == "agent.remembered"]
    return (job["status"] == "completed" and len(fired) == 1 and len(remembered) == 1
            and "garnet-9" in mem and "garnet-9" in k.outputs[j2["job_id"]]["artifact"]), \
        "routine fires (attested), its remember: line lands in agent memory (attested), later run recalls it"

def gg4():
    k = Kernel(tmp())
    (k.home / "secrets").mkdir(exist_ok=True)
    (k.home / "secrets" / "github.token").write_text(TOKEN)
    os.chmod(k.home / "secrets" / "github.token", 0o600)
    k.connectors.configure("github", WRONG)
    st = k.connectors.status("github")
    return st["state"] == "unreachable", \
        f"non-GitHub endpoint must not count as live (got {st['state']} {st.get('detail')!r})"

def gg5():
    k = Kernel(tmp())
    for n in ("Fleet A20", "Fleet B20"):
        k.agents.create(n, f"You are {n}.")
    job = k.fleet_run("Assess.\nremember: the fleet code is quartz-3",
                      ["fleet-a20", "fleet-b20"], base_url=STUB)
    mems = [k.agent_memory_read(a) for a in ("fleet-a20", "fleet-b20")]
    members = [e for e in k.ledger.read() if e["kind"] == "fleet.member"]
    return (job["status"] == "completed" and len(members) == 2
            and all("quartz-3" in m for m in mems)), \
        "fleet: both members complete, each member's memory holds the shared remember: note"

def gg6():
    home = tmp()
    (home / "secrets").mkdir()
    (home / "secrets" / "github.token").write_text(TOKEN)
    os.chmod(home / "secrets" / "github.token", 0o600)
    k = Kernel(home)
    k.connectors.configure("github", MOCK)
    ag = k.agents.create("Repo Scout20", "List repos.", tools=["github.repos"],
                         extra_tools=k.connectors.tool_names())
    r = k.create_routine("Repo Pulse", ag["id"],
                         "Inventory the account.\nconnector: github.repos", 60)
    job = k.fire_routine(r, base_url=STUB)
    invoked = [e for e in k.ledger.read() if e["kind"] == "tool.invoked"
               and e["raw"].get("tool") == "github.repos"]
    fired = [e for e in k.ledger.read() if e["kind"] == "routine.fired"]
    transcript = k.transcript(job["job_id"])
    return (job["status"] == "completed" and len(fired) == 1 and len(invoked) == 1
            and "crow-os" in k.outputs[job["job_id"]]["artifact"]
            and "github.repos" in transcript), \
        "routine fires a granted connector, attested as routine.fired + tool.invoked, data in artifact and transcript"

for name, fn in [("GG1 routine writes agent memory", gg1),
                 ("GG4 misdirected connector is not live", gg4),
                 ("GG5 fleet writes per-member memory", gg5),
                 ("GG6 routine uses a connector", gg6)]:
    probe(name, fn)

# -- federation combinations: one real peer home (server) + sender kernel
def call(base, method, path, payload=None):
    req = urllib.request.Request(base + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode()
            return r.status, json.loads(body) if r.headers.get_content_type() == "application/json" else body
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try: return e.code, json.loads(body)
        except Exception: return e.code, body

def boot(name, port):
    home = Path(tempfile.mkdtemp())
    env = dict(os.environ, PYTHONPATH=str(SRC), OLLAMA_HOST=STUB,
               CROW_OS_NAME=name, CROW_OS_GITHUB_TOKEN=TOKEN)
    proc = subprocess.Popen([sys.executable, "-c",
        "import sys; sys.argv=['crow','--home',sys.argv[1],'serve','--port',sys.argv[2]];"
        " from crow_os.cli import main; main()", str(home), str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(75):
        try: urllib.request.urlopen(base + "/api/status", timeout=2); break
        except Exception: time.sleep(0.2)
    return proc, base

beta_proc, BETA = boot("beta-home", 8823)
try:
    _, ib = call(BETA, "GET", "/api/identity")
    call(BETA, "POST", "/api/agents", {"name": "Remote Keeper", "instructions": "You keep notes for peers."})
    call(BETA, "POST", "/api/agents", {"name": "Remote Scout", "instructions": "You scout remote systems.",
                                       "tools": ["github.repos"]})
    _, cfg = call(BETA, "POST", "/api/connectors/github", {"base_url": MOCK})
    sender = Kernel(tmp())
    sender.peers.register("beta-home", BETA, ib["pubkey"])
    ident = sender.identity_info()
    call(BETA, "POST", "/api/peers", {"name": "alpha-home", "url": "http://127.0.0.1:9",
                                      "pubkey": ident["pubkey"]})

    def gg2():
        reply = sender.federation_dispatch(
            "beta-home", "remote-keeper",
            "Log this.\nremember: the federated code is cobalt-7")
        _, mem = call(BETA, "GET", "/api/agents/remote-keeper/memory")
        reply2 = sender.federation_dispatch(
            "beta-home", "remote-keeper", "What is the federated code?")
        sender_remembered = [e for e in sender.ledger.read()
                             if e["kind"] == "agent.remembered"]
        return (reply["job"]["status"] == "completed"
                and "cobalt-7" in mem.get("memory", "")
                and "cobalt-7" in reply2.get("artifact", "")
                and not sender_remembered
                and sender.agent_memory_read("remote-keeper") == ""), \
            "federated remember: lands in the REMOTE agent's memory and is recalled there; nothing leaks into the sender's memory or ledger"

    def gg3():
        reply = sender.federation_dispatch(
            "beta-home", "remote-scout", "Inventory.\nconnector: github.repos")
        blob = json.dumps(reply) + json.dumps(sender.ledger.read())
        sender_invoked = [e for e in sender.ledger.read()
                          if e["kind"] == "tool.invoked"]
        _, bs = call(BETA, "GET", "/api/status")
        return (reply["job"]["status"] == "completed"
                and "crow-os" in reply.get("artifact", "")
                and TOKEN not in blob and not sender_invoked
                and bs["ledger"]["ok"]), \
            "federated connector run: remote data reaches the sender, token appears nowhere on the sender, remote ledger still verifies"

    for name, fn in [("GG2 federated run writes remote memory", gg2),
                     ("GG3 federated run uses a connector", gg3)]:
        probe(name, fn)
finally:
    beta_proc.terminate()
    stub.shutdown(); mock.shutdown(); wrong.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-20 probes passed; failures: {fails}")
