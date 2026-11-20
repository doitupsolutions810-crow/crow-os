#!/usr/bin/env python3
"""Round-28 probes: the v0.16.7 (Round 27) residual race — agent
deleted between the handler's existence check and federation_receive.

Round 27 moved agent-existence validation BEFORE check_replay, so a
dispatch to an agent that does not exist (yet) no longer burns its
body. But the fix left a stated residual, in server.py itself: an
agent deleted between the handler's agents.get and federation_receive
still burns the body. The sequence in /api/federation/run is:

    agents.get(agent)          # handler check (Round 27)
    check_replay(payload)      # records the body in the seen-store
    federation_receive(...)    # agents.get(agent) AGAIN, then attests,
                               # then run_agent agents.get(agent) AGAIN

So a delete landing in either of two windows burns or lies:
  W1  between the handler check and federation_receive's get: the
      receive raises AgentError -> 400, but check_replay already
      recorded the body, so recreating the agent and resending the
      identical signed body hits 401 "replayed" for a job that never
      ran and was never attested. Same poison as Round 27, narrower
      window — and the window is not microscopic: check_replay does
      CrowFS read + write of the seen-store between the two checks.
  W2  between federation_receive's attestation and run_agent's get:
      the ledger already holds federation.received, then run_agent
      raises AgentError -> 400. A receipt for a job that never ran —
      the false-attestation class Rounds 25-26 closed for malformed
      shapes, reopened here by a delete. The resend is 401 too (the
      body was recorded), so W2 is W1 plus a lying ledger entry.
Both need only an operator deleting an agent mid-dispatch — no peer
action — but the operator action is the documented admin path
(DELETE /api/agents/<id>), and the burn persists across restarts
because the seen-store lives in CrowFS.

Probes (one real Crow home, beta, peer key generated here; stub
Ollama as in harnesses 21-27):
  R1  control: normal dispatch -> 200, exactly one federation.received
  R2  regression (Round 27 R1): dispatch to unknown agent -> 400, no
      attestation; create the agent; resend the SAME signed body ->
      200 (a refused body must still not burn a slot)
  R3  storm: 48 distinct signed dispatches to "race-agent", fired in
      waves of 8, while a deleter thread loops DELETE + recreate of
      that agent. Classify every body: 200 = ran; 400 then resend
      200 = clean refusal (delete landed before the handler check);
      400 then resend 401 = BURNED (delete landed in W1/W2). Also
      compare the federation.received delta against the 200-count:
      any excess is a W2 false attestation. Pass iff zero burns and
      zero excess attestations.
  R4  if R3 found a burn: restart, recreate the agent, resend a
      burned body -> still 401 (poison persists across restart).
      If R3 found none, there is nothing to persist (noted, pass).
  R5  control after the storm: fresh dispatch -> 200, +1 attestation,
      /api/status 200 with ledger ok:true, server alive
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
    errlog = open(f"/tmp/harness28-server-{port}.log", "ab")
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
beta_proc, BETA = boot("beta-home", 8840, home)
burned = []  # signed bodies R3 proved burned, for R4
try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
    call(BETA, "POST", "/api/agents", {"name": "Remote Scout", "instructions": "You scout remote systems and report back."})
    call(BETA, "POST", "/api/agents", {"name": "Race Agent", "instructions": "I get deleted mid-dispatch."})
    call(BETA, "POST", "/api/peers", {"name": "replay-peer", "url": "http://127.0.0.1:9", "pubkey": pub})

    def signed(body):
        payload = dict(body); payload["sig"] = key.sign(canon(body).encode()).hex()
        return payload

    def run(payload):
        return call(BETA, "POST", "/api/federation/run", payload)

    def base(ts=None, agent="remote-scout", objective="round28 probe"):
        return {"from": "replay-peer", "agent": agent,
                "objective": objective, "ts": int(time.time()) if ts is None else ts}

    def received_count():
        code, ledger = call(BETA, "GET", "/api/ledger?limit=500")
        assert code == 200, f"ledger read failed ({code})"
        return sum(1 for e in ledger["entries"] if e.get("kind") == "federation.received")

    def agent_exists(agent_id):
        code, listing = call(BETA, "GET", "/api/agents")
        return code == 200 and any(a.get("id") == agent_id for a in listing.get("agents", []))

    def ensure_agent(name="Race Agent"):
        if not agent_exists("race-agent"):
            code, agent = call(BETA, "POST", "/api/agents",
                               {"name": name, "instructions": "I get deleted mid-dispatch."})
            assert code in (200, 201), f"agent create failed ({code}): {agent}"

    def r1():
        before = received_count()
        code, _ = run(signed(base(objective="control before storm")))
        after = received_count()
        s, st = call(BETA, "GET", "/api/status")
        ok = code == 200 and after == before + 1 and s == 200 and st["ledger"]["ok"]
        return ok, f"normal dispatch -> {code}, received {before}->{after} (want 200, +1), ledger ok={st.get('ledger', {}).get('ok') if s == 200 else 'n/a'}"
    def r2():
        before = received_count()
        payload = signed(base(agent="late-agent", objective="run once the agent exists"))
        c1, _ = run(payload)
        code, agent = call(BETA, "POST", "/api/agents", {"name": "Late Agent", "instructions": "I arrived late."})
        assert code in (200, 201), f"agent create failed ({code}): {agent}"
        c2, _ = run(payload)
        after = received_count()
        ok = c1 == 400 and c2 == 200 and after == before + 1
        return ok, f"unknown agent -> {c1} (want 400); same body after create -> {c2} (want 200), received {before}->{after} (want +1)"
    def r3():
        ensure_agent()
        before = received_count()
        stop = threading.Event()
        def deleter():
            while not stop.is_set():
                call(BETA, "DELETE", "/api/agents/race-agent")
                if not agent_exists("race-agent"):
                    call(BETA, "POST", "/api/agents",
                         {"name": "Race Agent", "instructions": "I get deleted mid-dispatch."})
        dt = threading.Thread(target=deleter, daemon=True); dt.start()
        outcomes = []  # (payload, code)
        try:
            N, WAVE = 48, 8
            for wave_start in range(0, N, WAVE):
                batch = [signed(base(agent="race-agent", objective=f"storm {i}"))
                         for i in range(wave_start, min(wave_start + WAVE, N))]
                outs = [None] * len(batch)
                def one(idx, p):
                    outs[idx] = run(p)[0]
                threads = [threading.Thread(target=one, args=(i, p)) for i, p in enumerate(batch)]
                for t in threads: t.start()
                for t in threads: t.join()
                outcomes.extend(zip(batch, outs))
        finally:
            stop.set(); dt.join(timeout=10)
        ensure_agent()
        after = received_count()
        ran = sum(1 for _, c in outcomes if c == 200)
        refused = [(p, c) for p, c in outcomes if c == 400]
        other = [(p, c) for p, c in outcomes if c not in (200, 400)]
        # Classify every refusal by resending the identical body now
        # that the agent exists: 200 = clean refusal, 401 = burned.
        burns, cleans = 0, 0
        for p, _ in refused:
            c, _ = run(p)
            if c == 401: burns += 1; burned.append(p)
            elif c == 200: cleans += 1
            else: burns += 1; burned.append(p)
        excess = after - before - ran  # attestations beyond jobs that ran (W2)
        ok = burns == 0 and excess == 0 and not other
        return ok, (f"48 dispatches under delete-storm: ran={ran}, refused-400={len(refused)} "
                    f"(clean resend-200={cleans}, BURNED resend-401={burns}), other={sorted(set(c for _, c in other))}, "
                    f"received {before}->{after} (want +{ran}, excess={excess})")
    def r4():
        global beta_proc, BETA
        if not burned:
            return True, "no burns in R3, nothing to persist across restart"
        payload = burned[0]
        beta_proc.terminate(); beta_proc.wait(timeout=10)
        beta_proc, BETA = boot("beta-home", 8840, home)
        ensure_agent()
        before = received_count()
        c, _ = run(payload)
        after = received_count()
        ok = c == 401 and after == before
        return ok, f"burned body after restart+recreate -> {c} (want 401), received {before}->{after} (want unchanged)"
    def r5():
        ensure_agent()
        before = received_count()
        code, _ = run(signed(base(objective="control after storm")))
        after = received_count()
        s, st = call(BETA, "GET", "/api/status")
        ok = code == 200 and after == before + 1 and s == 200 and st["ledger"]["ok"]
        return ok, f"normal dispatch -> {code}, received {before}->{after} (want 200, +1), ledger ok={st.get('ledger', {}).get('ok') if s == 200 else 'n/a'}"

    probe("R1 normal dispatch runs, exactly one attestation (control)", r1)
    probe("R2 unknown agent then created: same body retries 200 (Round 27 regression)", r2)
    probe("R3 delete-storm: no burned bodies, no excess attestations", r3)
    probe("R4 burn (if any) persists across restart", r4)
    probe("R5 normal dispatch still runs after storm (control)", r5)
finally:
    try: beta_proc.terminate()
    except Exception: pass
print(f"{sum(1 for _, ok in results if ok)}/{len(results)} passed")
