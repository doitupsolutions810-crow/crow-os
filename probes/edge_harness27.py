#!/usr/bin/env python3
"""Round-27 probes: the v0.16.6 (Round 26) blank-field fix, and the
validation that still happens AFTER the replay check.

Rounds 25-26 established a rule and proved it for malformed shapes:
a body refused before it runs must leave no trace in the seen-store
that blocks legitimate traffic (harness 26 R5: a malformed body
sharing a ts with a later valid dispatch must not poison it). The
Round-26 fix enforces that for shape failures by refusing them in
verify_incoming, BEFORE check_replay is called.

But one validation still runs after check_replay: agent existence.
The /api/federation/run handler order is verify_incoming ->
check_replay -> federation_receive, and federation_receive is where
agents.get(agent_id) runs. So a signed dispatch naming an agent
that does not exist (yet) is recorded in the seen-store FIRST, then
refused 400 — and the identical body can never be retried inside
the 120s window even after the agent exists, because check_replay
now refuses it as a replay. The job never ran and was never
attested, yet its retry is blocked exactly like a job that did run.
Same class as the R5 poison, one validation later in the chain.
A corrupt agent definition hits the same path (agents.get raises
AgentError for corrupt JSON too), and the poison survives restart
because the seen-store is persisted in CrowFS.

Probes (one real Crow home, beta, peer key generated here; stub
Ollama as in harnesses 21-26):
  R1  dispatch to agent "late-agent" before it exists -> 400, no
      attestation; create the agent; resend the SAME signed body
      -> 200 if unpoisoned (want), 401 if poisoned (defect)
  R2  corrupt an agent's definition file, dispatch to it -> 400,
      no attestation; restore the file; resend the SAME signed
      body -> 200 if unpoisoned, 401 if poisoned
  R3  8x concurrent identical dispatches to an unknown agent ->
      all 400 (agent existence is now validated before the replay
      check, so none of them consumes a seen-store slot), server
      alive, no attestation
  R4  unknown-agent body sharing a ts with a later valid dispatch
      -> valid dispatch still runs 200 (control: no cross-poison)
  R5  unknown-agent dispatch, restart, create the agent, resend
      the SAME body -> 200 if unpoisoned, 401 if poisoned
      (poison survives restart)
  R6  traversal / oversized agent ids ("../../etc/passwd",
      "x"*500, "remote-scout/../../x") -> 400 each, no attestation,
      no dropped connection, server alive
  R7  control: normal dispatch after all of it -> 200, exactly one
      new federation.received, server alive
"""
import json, os, subprocess, sys, tempfile, threading, time
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
from crow_os.signers import canon

results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}", flush=True)

class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        out = json.dumps({"models": [{"name": "m"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if body.get("stream"):
            payload = (json.dumps({"message": {"content": "remote agent reporting in"}}).encode() + b"\n"
                       + json.dumps({"done": True}).encode() + b"\n")
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        out = json.dumps({"message": {"content": "reply"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
stub = HTTPServer(("127.0.0.1", 0), Stub); threading.Thread(target=stub.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{stub.server_address[1]}"

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
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}

def boot(name, port, home):
    errlog = open(f"/tmp/harness27-server-{port}.log", "ab")
    env = dict(os.environ, PYTHONPATH=str(SRC), OLLAMA_HOST=STUB, CROW_OS_NAME=name)
    proc = subprocess.Popen([sys.executable, "-c",
        "import sys; sys.argv=['crow','--home',sys.argv[1],'serve','--port',sys.argv[2]];"
        " from crow_os.cli import main; main()", str(home), str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=errlog)
    base = f"http://127.0.0.1:{port}"
    for _ in range(75):
        try: urllib.request.urlopen(base + "/api/status", timeout=2); break
        except Exception: time.sleep(0.2)
    return proc, base

home = Path(tempfile.mkdtemp())
beta_proc, BETA = boot("beta-home", 8839, home)
try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    call(BETA, "POST", "/api/agents", {"name": "Remote Scout", "instructions": "You scout remote systems and report back."})
    call(BETA, "POST", "/api/peers", {"name": "replay-peer", "url": "http://127.0.0.1:9", "pubkey": pub})

    def signed(body):
        payload = dict(body); payload["sig"] = key.sign(canon(body).encode()).hex()
        return payload

    def run(payload):
        return call(BETA, "POST", "/api/federation/run", payload)

    def base(ts=None, agent="remote-scout", objective="round27 probe"):
        return {"from": "replay-peer", "agent": agent,
                "objective": objective, "ts": int(time.time()) if ts is None else ts}

    def received_count():
        code, ledger = call(BETA, "GET", "/api/ledger?limit=500")
        assert code == 200, f"ledger read failed ({code})"
        return sum(1 for e in ledger["entries"] if e.get("kind") == "federation.received")

    def burst(payloads):
        outs = []
        def one(p):
            outs.append(run(p))
        threads = [threading.Thread(target=one, args=(p,)) for p in payloads]
        for t in threads: t.start()
        for t in threads: t.join()
        return outs

    def r1():
        before = received_count()
        payload = signed(base(agent="late-agent", objective="run once the agent exists"))
        c1, _ = run(payload)
        code, agent = call(BETA, "POST", "/api/agents", {"name": "Late Agent", "instructions": "I arrived late."})
        assert code in (200, 201), f"agent create failed ({code}): {agent}"
        c2, _ = run(payload)
        # fresh body to the now-existing agent must run: proves the
        # agent itself works, only the burned body is blocked.
        c3, _ = run(signed(base(agent="late-agent", objective="fresh body, same agent")))
        after = received_count()
        ok = c1 == 400 and c2 == 200 and c3 == 200 and after == before + 2
        return ok, f"unknown agent -> {c1} (want 400); same body after create -> {c2} (want 200); fresh body -> {c3} (want 200), received {before}->{after} (want +2)"
    def r2():
        code, agent = call(BETA, "POST", "/api/agents", {"name": "Corruptible", "instructions": "I get corrupted."})
        assert code in (200, 201), f"agent create failed ({code})"
        agent_file = home / "fs" / "system" / "agents" / "corruptible.json"
        original = agent_file.read_text()
        payload = signed(base(agent="corruptible", objective="run after repair"))
        agent_file.write_text("{not json")
        before = received_count()
        c1, _ = run(payload)
        agent_file.write_text(original)
        c2, _ = run(payload)
        after = received_count()
        ok = c1 == 400 and c2 == 200 and after == before + 1
        return ok, f"corrupt agent def -> {c1} (want 400); same body after repair -> {c2} (want 200), received {before}->{after} (want +1)"
    def r3():
        before = received_count()
        payload = signed(base(agent="ghost-agent", objective="concurrent ghost"))
        outs = burst([payload] * 8)
        codes = sorted(o[0] for o in outs)
        after = received_count()
        s, _ = call(BETA, "GET", "/api/status")
        ok = codes.count(400) == 8 and after == before and s == 200
        return ok, f"8x concurrent unknown agent -> {codes} (want eight 400), received {before}->{after} (want unchanged), /api/status -> {s}"
    def r4():
        ts = int(time.time())
        c0, _ = run(signed(base(ts, agent="ghost-agent", objective="ghost sharing ts")))
        before = received_count()
        c1, _ = run(signed(base(ts, objective="valid sharing ts with ghost")))
        after = received_count()
        ok = c0 == 400 and c1 == 200 and after == before + 1
        return ok, f"ghost -> {c0} (want 400); valid same-ts -> {c1} (want 200), received {before}->{after} (want +1)"
    def r5():
        global beta_proc, BETA
        payload = signed(base(agent="restart-agent", objective="run after restart and create"))
        c1, _ = run(payload)
        beta_proc.terminate(); beta_proc.wait(timeout=10)
        beta_proc, BETA = boot("beta-home", 8839, home)
        code, agent = call(BETA, "POST", "/api/agents", {"name": "Restart Agent", "instructions": "I arrived after a restart."})
        assert code in (200, 201), f"agent create failed ({code})"
        before = received_count()
        c2, _ = run(payload)
        after = received_count()
        ok = c1 == 400 and c2 == 200 and after == before + 1
        return ok, f"unknown agent -> {c1} before restart (want 400); same body after restart+create -> {c2} (want 200), received {before}->{after} (want +1)"
    def r6():
        before = received_count()
        codes = []
        for agent in ("../../etc/passwd", "x" * 500, "remote-scout/../../x"):
            code, _ = run(signed(base(agent=agent, objective="traversal probe")))
            codes.append(code)
        after = received_count()
        s, _ = call(BETA, "GET", "/api/status")
        ok = all(c == 400 for c in codes) and after == before and s == 200
        return ok, f"traversal/oversized agent ids -> {codes} (want all 400), received {before}->{after} (want unchanged), /api/status -> {s}"
    def r7():
        before = received_count()
        code, _ = run(signed(base(objective="control after all ghost traffic")))
        after = received_count()
        s, _ = call(BETA, "GET", "/api/status")
        ok = code == 200 and after == before + 1 and s == 200
        return ok, f"normal dispatch -> {code}, received {before}->{after} (want 200, +1), /api/status -> {s}"

    probe("R1 unknown agent then created: same body retries 200", r1)
    probe("R2 corrupt agent def then repaired: same body retries 200", r2)
    probe("R3 8x concurrent unknown agent: all 400, no attestation, none burns a slot", r3)
    probe("R4 ghost sharing ts does not block valid dispatch", r4)
    probe("R5 unknown agent, restart, create, same body retries 200", r5)
    probe("R6 traversal/oversized agent ids refused 400, no attestation, server alive", r6)
    probe("R7 normal dispatch still runs after all ghost traffic (control)", r7)
finally:
    try: beta_proc.terminate()
    except Exception: pass
print(f"{sum(1 for _, ok in results if ok)}/{len(results)} passed")
