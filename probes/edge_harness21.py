#!/usr/bin/env python3
"""Round-21 probes: federation replay — the gap Round 18 left open.

Round 18 proved a federation request must be signed by a REGISTERED
peer and fresh (within 120s). It never asked: what happens when the
*exact same* signed request is delivered twice inside that window?
verify_incoming() checks signature + timestamp only — there is no
nonce, no seen-request store, and no idempotency key anywhere in
federation.py or server.py. A captured signed body (anyone on the
plain-HTTP path between homes can capture it) should therefore run
the remote job a second time: a second job, a second
federation.received attestation, and a second round of side effects.

Probes (all against one real Crow home, beta, with a peer key this
harness generates — the same rogue-peer construction Round 18 used,
so the signature is genuinely valid for the registered peer):
  R1 replay identical signed body twice -> second must be refused
  R2 signed but stale (ts -3600) -> 401 (control, Round 18 behaviour)
  R3 same signature, objective tampered after signing -> 401 (control)
  R4 same ts, different objective, freshly signed -> both run (a fix
     must key on the request, not on the timestamp alone)
  R5 replay of R4's first body -> must be refused
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
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")

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

def boot(name, port):
    home = Path(tempfile.mkdtemp())
    env = dict(os.environ, PYTHONPATH=str(SRC), OLLAMA_HOST=STUB, CROW_OS_NAME=name)
    proc = subprocess.Popen([sys.executable, "-c",
        "import sys; sys.argv=['crow','--home',sys.argv[1],'serve','--port',sys.argv[2]];"
        " from crow_os.cli import main; main()", str(home), str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(75):
        try: urllib.request.urlopen(base + "/api/status", timeout=2); break
        except Exception: time.sleep(0.2)
    return proc, base

beta_proc, BETA = boot("beta-home", 8831)
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

    replay_body = {"from": "replay-peer", "agent": "remote-scout",
                   "objective": "replay me", "ts": int(time.time())}
    replay_payload = signed(replay_body)
    first = run(replay_payload)
    second = run(replay_payload)

    def r1():
        ok = first[0] == 200 and second[0] == 401
        note = (f"first={first[0]} job={first[1].get('job', {}).get('job_id')}, "
                f"replay={second[0]} {second[1]}")
        if first[0] == 200 and second[0] == 200:
            note += (f" — REPLAY RAN TWICE: second job "
                     f"{second[1].get('job', {}).get('job_id')}")
        return ok, note
    def r2():
        code, _ = run(signed({"from": "replay-peer", "agent": "remote-scout",
                              "objective": "stale", "ts": int(time.time()) - 3600}))
        return code == 401, f"signed stale ts refused ({code})"
    def r3():
        payload = signed({"from": "replay-peer", "agent": "remote-scout",
                          "objective": "original", "ts": int(time.time())})
        payload["objective"] = "tampered after signing"
        code, _ = run(payload)
        return code == 401, f"tampered objective refused ({code})"
    def r4():
        ts = int(time.time())
        a = run(signed({"from": "replay-peer", "agent": "remote-scout",
                        "objective": "first distinct job", "ts": ts}))
        b = run(signed({"from": "replay-peer", "agent": "remote-scout",
                        "objective": "second distinct job", "ts": ts}))
        ok = (a[0] == 200 and b[0] == 200
              and a[1].get("job", {}).get("job_id") != b[1].get("job", {}).get("job_id"))
        return ok, f"same-ts distinct objectives both run ({a[0]}, {b[0]})"
    def r5():
        ts = int(time.time())
        body = {"from": "replay-peer", "agent": "remote-scout",
                "objective": "replay me too", "ts": ts}
        payload = signed(body)
        first5 = run(payload)
        second5 = run(payload)
        return first5[0] == 200 and second5[0] == 401, \
            f"first={first5[0]}, replay={second5[0]}"

    probe("R1 replay identical signed body refused", r1)
    probe("R2 signed stale timestamp refused", r2)
    probe("R3 tampered objective refused", r3)
    probe("R4 same-ts distinct objectives both run", r4)
    probe("R5 second replay refused", r5)
finally:
    beta_proc.terminate()
print(f"{sum(1 for _, ok in results if ok)}/{len(results)} passed")
