#!/usr/bin/env python3
"""Round-26 probes: the v0.16.5 (Round 25) malformed-shape fix under
the conditions Rounds 21-24 tested only in isolation.

Round 25 fixed malformed federation shapes for a SINGLE dispatch:
field types are validated before attestation or run, and a NaN ts
is refused outright. But every earlier replay fix only broke when
combined with another condition — Round 22 caught the Round-21
store failing under concurrency and restart, Round 24 caught the
Round-23 fix only in combination. The v0.16.5 fix has never been
exercised in those combinations, and the combinations are exactly
where a type check could diverge:

  * Infinity is NaN's untested sibling. Python's json accepts
    Infinity/-Infinity (and 1e400 parses to inf) just as it accepts
    NaN, and the same "every comparison is False" logic applies —
    but harness 25 only ever sent NaN.
  * Concurrency + malformed: 8 identical NaN dispatches at once.
    verify_incoming refuses before check_replay, so none should
    touch the seen-store — but a refusal path that still wrote,
    locked, or crashed under threads would show up here as a
    dropped connection, a false attestation, or a poisoned store.
  * Concurrency + mixed traffic: valid and malformed dispatches
    fired together. The malformed ones must not steal a slot,
    conflate a key, or crash the handler the valid ones share.
  * Restart + malformed: a NaN body refused, the home restarts,
    the same body is sent again. Refusal must not depend on any
    in-memory state, and the restart must not turn a refused body
    into a runnable one.
  * Store pollution: a malformed body sharing a ts with a later
    valid dispatch. Refused-before-check_replay means the malformed
    body must leave no trace that blocks the valid one — including
    a future-dated malformed body sharing its ts.
  * Empty-string fields: "" is a string, so it passes the Round-25
    type check, but it is not a usable agent or objective. Before
    the fix, objective="" was attested and THEN crashed
    create_job (WorkflowError, uncaught by the handler) — a dropped
    connection and a false receipt. Blank from/agent/objective now
    refuse 401 at the federation boundary, before any attestation.

Probes (one real Crow home, beta, peer key generated here; stub
Ollama as in harnesses 21-25):
  R1  ts=+Infinity / -Infinity / 1e400 -> 401 each, no attestation
  R2  8x concurrent identical NaN dispatches -> 8x401, no jobs,
      no attestation, server alive
  R3  4x concurrent valid distinct + 4x concurrent malformed,
      same burst -> 4x200 distinct jobs, 4x401
  R4  NaN dispatch, restart, same NaN dispatch again -> 401, 401;
      valid control after restart -> 200
  R5  malformed body sharing a ts with a later valid dispatch
      (same-ts and future-dated) -> valid dispatch still runs 200
  R6  objective="" / objective="   " / agent="" signed -> 401 each,
      no attestation, no dropped connection
  R7  control: normal dispatch after all malformed traffic -> 200,
      exactly one new federation.received, server alive
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
    errlog = open(f"/tmp/harness26-server-{port}.log", "ab")
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
beta_proc, BETA = boot("beta-home", 8838, home)
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

    def base(ts=None):
        return {"from": "replay-peer", "agent": "remote-scout",
                "objective": "round26 probe", "ts": int(time.time()) if ts is None else ts}

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
        codes = []
        for ts in (float("inf"), float("-inf"), 1e400):
            code, _ = run(signed({**base(), "ts": ts}))
            codes.append(code)
        after = received_count()
        ok = all(c == 401 for c in codes) and after == before
        return ok, f"ts=+Inf/-Inf/1e400 -> {codes} (want all 401), received {before}->{after} (want unchanged)"
    def r2():
        before = received_count()
        payload = signed({**base(), "ts": float("nan")})
        outs = burst([payload] * 8)
        codes = [o[0] for o in outs]
        after = received_count()
        ok = all(c == 401 for c in codes) and after == before
        return ok, f"8x concurrent identical NaN -> {codes} (want all 401), received {before}->{after} (want unchanged)"
    def r3():
        ts = int(time.time())
        valid = [signed({"from": "replay-peer", "agent": "remote-scout",
                         "objective": f"mixed valid {i}", "ts": ts}) for i in range(4)]
        malformed = [
            signed({**base(ts), "ts": float("nan")}),
            signed({**base(ts), "objective": 123}),
            signed({**base(ts), "ts": None}),
            {**signed(base(ts)), "sig": 123},
        ]
        before = received_count()
        outs = burst(valid + malformed)
        jobs = [o[1].get("job", {}).get("job_id") for o in outs if o[0] == 200]
        bad = [o[0] for o in outs if o[0] not in (200, 401)]
        after = received_count()
        ok = len(jobs) == 4 and len(set(jobs)) == 4 and after == before + 4 and not bad
        return ok, f"mixed burst: {len(jobs)}x200 ({len(set(jobs))} distinct jobs), {sum(1 for o in outs if o[0]==401)}x401, other={bad}, received {before}->{after} (want 4x200, 4x401, +4)"
    def r4():
        global beta_proc, BETA
        payload = signed({**base(), "ts": float("nan")})
        c1, _ = run(payload)
        beta_proc.terminate(); beta_proc.wait(timeout=10)
        beta_proc, BETA = boot("beta-home", 8838, home)
        c2, _ = run(payload)
        before = received_count()
        c3, _ = run(signed({**base(), "objective": "control after restart"}))
        after = received_count()
        ok = c1 == 401 and c2 == 401 and c3 == 200 and after == before + 1
        return ok, f"NaN -> {c1} before restart, {c2} after restart (want 401, 401); valid control after restart -> {c3}, received {before}->{after} (want 200, +1)"
    def r5():
        ts = int(time.time())
        run(signed({**base(ts), "objective": 123}))
        run(signed({**base(ts), "ts": None}))
        before = received_count()
        c1, _ = run(signed({**base(ts), "objective": "valid sharing ts with malformed"}))
        after = received_count()
        fut = int(time.time()) + 110
        run(signed({**base(fut), "objective": None}))
        c2, _ = run(signed({**base(fut), "objective": "valid sharing future ts with malformed"}))
        ok = c1 == 200 and after == before + 1 and c2 == 200
        return ok, f"valid same-ts after malformed -> {c1} (want 200, received +1); valid future-ts after malformed -> {c2} (want 200)"
    def r6():
        before = received_count()
        codes = []
        for body in ({**base(), "objective": ""},
                     {**base(), "objective": "   "},
                     {**base(), "agent": ""}):
            code, _ = run(signed(body))
            codes.append(code)
        after = received_count()
        ok = all(c == 401 for c in codes) and after == before
        return ok, f"objective='' / '   ' / agent='' -> {codes} (want all 401), received {before}->{after} (want unchanged)"
    def r7():
        before = received_count()
        code, _ = run(signed({**base(), "objective": "control after all malformed traffic"}))
        after = received_count()
        s, _ = call(BETA, "GET", "/api/status")
        ok = code == 200 and after == before + 1 and s == 200
        return ok, f"normal dispatch -> {code}, received {before}->{after} (want 200, +1), /api/status -> {s}"

    probe("R1 ts=+Inf/-Inf/1e400 refused 401, no attestation", r1)
    probe("R2 8x concurrent identical NaN: all 401, no jobs, no attestation", r2)
    probe("R3 mixed burst: 4 valid run once each, 4 malformed refused", r3)
    probe("R4 NaN refused before and after restart; valid control runs", r4)
    probe("R5 malformed bodies do not poison the seen-store for their ts", r5)
    probe("R6 blank fields refused 401, no attestation, no dropped connection", r6)
    probe("R7 normal dispatch still runs after all malformed traffic (control)", r7)
finally:
    try: beta_proc.terminate()
    except Exception: pass
print(f"{sum(1 for _, ok in results if ok)}/{len(results)} passed")
