#!/usr/bin/env python3
"""Round-25 probes: malformed-but-signed federation payloads.

Rounds 21-24 hardened the replay store (seen-store, lock, restart
persistence, ts-keyed pruning) — but only for well-formed bodies.
verify_incoming checked presence + signature + staleness and never
checked TYPES, and /api/federation/run is synchronous and unguarded
(unlike the chat path, which runs behind _guarded). A signed body is
still a signed body even when its fields are the wrong JSON type,
and Python's json accepts NaN — so:

  * ts of None / list / dict: float() raises TypeError, which the
    handler does not catch (it catches ValueError) -> the client
    sees a dropped connection, no HTTP status at all.
  * sig of int / dict: bytes.fromhex() raises TypeError inside
    Identity.verify -> same dropped connection.
  * objective of int / None: passes verification and the replay
    check, federation.received is ATTESTED, then run_agent crashes
    on objective.splitlines() -> dropped connection AND a receipt
    in the ledger for a job that never ran.
  * ts as a numeric string: accepted and ran (200).
  * ts as NaN: the worst one. abs(now - NaN) > MAX_SKEW is False,
    so the body never goes stale, while the prune pass
    (now - v <= MAX_SKEW, also False for NaN) drops its seen-store
    entry on the next call — a captured NaN-ts dispatch replays
    forever, not just inside the 120s window. Reproduced: three
    runs, three distinct jobs, from one signed body.

Probes (one real Crow home, beta, peer key generated here; stub
Ollama as in harnesses 21-24):
  R1  ts=None -> 401 (was dropped connection)
  R2  ts list / dict -> 401 (was dropped connection)
  R3  ts="abc" -> 401 (was misleading 400 "body must be JSON")
  R4  ts=str(now) numeric string -> 401 (was 200)
  R5  ts=True -> 401 (bool is an int in Python; not a timestamp)
  R6  ts=NaN, sent twice -> 401, 401 (was 200, 200, 200 unbounded)
  R7  objective=123 -> 401, and no federation.received attested
  R8  objective=None -> 401, and no federation.received attested
  R9  sig int / dict -> 401 (was dropped connection)
  R10 agent=123 -> 401 (was 400 via AgentError; malformed signed
       fields now refuse uniformly at the federation boundary)
  R11 control: normal dispatch -> 200, exactly one new
       federation.received, server alive after all malformed traffic
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
    errlog = open(f"/tmp/harness25-server-{port}.log", "ab")
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
beta_proc, BETA = boot("beta-home", 8837, home)
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

    def received_count():
        code, ledger = call(BETA, "GET", "/api/ledger?limit=500")
        assert code == 200, f"ledger read failed ({code})"
        return sum(1 for e in ledger["entries"] if e.get("kind") == "federation.received")

    def base(ts=None):
        return {"from": "replay-peer", "agent": "remote-scout",
                "objective": "round25 probe", "ts": int(time.time()) if ts is None else ts}

    def r1():
        code, _ = run(signed({**base(), "ts": None}))
        return code == 401, f"ts=None -> {code} (want 401)"
    def r2():
        c1, _ = run(signed({**base(), "ts": ["x"]}))
        c2, _ = run(signed({**base(), "ts": {"t": 1}}))
        return c1 == 401 and c2 == 401, f"ts list/dict -> {c1}, {c2} (want 401, 401)"
    def r3():
        code, body = run(signed({**base(), "ts": "abc"}))
        return code == 401, f"ts='abc' -> {code} {body} (want 401)"
    def r4():
        code, _ = run(signed({**base(), "ts": str(int(time.time()))}))
        return code == 401, f"ts=str(now) -> {code} (want 401)"
    def r5():
        code, _ = run(signed({**base(), "ts": True}))
        return code == 401, f"ts=True -> {code} (want 401)"
    def r6():
        payload = signed({**base(), "ts": float("nan")})
        c1, _ = run(payload)
        c2, _ = run(payload)
        return c1 == 401 and c2 == 401, f"ts=NaN twice -> {c1}, {c2} (want 401, 401)"
    def r7():
        before = received_count()
        code, _ = run(signed({**base(), "objective": 123}))
        after = received_count()
        return code == 401 and after == before, f"objective=123 -> {code}, received {before}->{after} (want 401, unchanged)"
    def r8():
        before = received_count()
        code, _ = run(signed({**base(), "objective": None}))
        after = received_count()
        return code == 401 and after == before, f"objective=None -> {code}, received {before}->{after} (want 401, unchanged)"
    def r9():
        p1 = signed(base()); p1["sig"] = 123
        p2 = signed(base()); p2["sig"] = {"x": 1}
        c1, _ = run(p1)
        c2, _ = run(p2)
        return c1 == 401 and c2 == 401, f"sig int/dict -> {c1}, {c2} (want 401, 401)"
    def r10():
        code, _ = run(signed({**base(), "agent": 123}))
        return code == 401, f"agent=123 -> {code} (want 401)"
    def r11():
        before = received_count()
        code, body = run(signed({**base(), "objective": "control after malformed"}))
        after = received_count()
        ok = code == 200 and after == before + 1
        return ok, f"normal dispatch -> {code}, received {before}->{after} (want 200, +1)"

    probe("R1 ts=None refused 401", r1)
    probe("R2 ts list/dict refused 401", r2)
    probe("R3 ts='abc' refused 401", r3)
    probe("R4 ts numeric string refused 401", r4)
    probe("R5 ts=True refused 401", r5)
    probe("R6 ts=NaN refused 401 on first send and replay", r6)
    probe("R7 objective=123 refused 401, no false attestation", r7)
    probe("R8 objective=None refused 401, no false attestation", r8)
    probe("R9 sig of wrong type refused 401", r9)
    probe("R10 agent=123 refused 401", r10)
    probe("R11 normal dispatch still runs, exactly one attestation (control)", r11)
finally:
    try: beta_proc.terminate()
    except Exception: pass
print(f"{sum(1 for _, ok in results if ok)}/{len(results)} passed")
