#!/usr/bin/env python3
"""Round-24 probes: the v0.16.4 (Round 23) fix under the conditions
Rounds 21-23 tested only in isolation.

Round 23 fixed the clock-skew seam for a SINGLE future-dated
dispatch: the seen-store entry is now keyed to the signed ts. But
each earlier fix only broke when combined with another condition:
Round 22 caught the Round-21 replay store failing under concurrency
and restart. The v0.16.4 fix has never been exercised in those
combinations, and the combinations are exactly where a ts-keyed
store could diverge:

  * Concurrency + future-dated: 8 identical future-dated dispatches
    at once. The Round-22 lock must still serialize the
    read-check-write when the stored value is a future ts, and the
    prune pass (which runs inside the same locked section, keyed to
    now - v) must not drop a future entry mid-burst.
  * Restart + future-dated: a future-dated dispatch runs, the home
    restarts, and the captured body is replayed while the signed ts
    is still inside its window. The entry must survive the restart
    (persisted in CrowFS) AND still be ts-keyed after reload — a
    reload that re-stamped entries with receipt time would reopen
    the Round-23 hole.
  * Concurrency + distinct future-dated: 4 distinct future-dated
    objectives sharing one ts, fired together — all must run, each
    exactly once (no false conflation, no lost jobs from the
    Round-22 CrowFS temp-name fix regressing under future ts).

Probes (one real Crow home, beta, peer key generated here; stub
Ollama as in harnesses 21-23):
  R1 8x concurrent identical future-dated (+110s) dispatch -> 1x200, 7x401
  R2 future-dated dispatch, restart, replay inside window -> 401
  R3 4x concurrent distinct future-dated objectives, same ts -> 4x200 distinct jobs
  R4 control: a normal (ts=now) dispatch after a future-dated one still runs 200
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
    errlog = open(f"/tmp/harness24-server-{port}.log", "ab")
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
beta_proc, BETA = boot("beta-home", 8834, home)
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

    def burst(payload, n=8):
        outs = []
        def one():
            outs.append(run(payload))
        threads = [threading.Thread(target=one) for _ in range(n)]
        for t in threads: t.start()
        for t in threads: t.join()
        return outs

    def r1():
        ts = int(time.time()) + 110
        payload = signed({"from": "replay-peer", "agent": "remote-scout",
                          "objective": "concurrent future replay", "ts": ts})
        outs = burst(payload, 8)
        ok200 = [o for o in outs if o[0] == 200]
        ok401 = [o for o in outs if o[0] == 401]
        jobs = {o[1].get("job", {}).get("job_id") for o in ok200}
        ok = len(ok200) == 1 and len(ok401) == 7 and len(jobs) == 1
        return ok, f"8x concurrent identical future-dated: {len(ok200)}x200 ({len(jobs)} job), {len(ok401)}x401, other={[o[0] for o in outs if o[0] not in (200, 401)]}"
    def r2():
        global beta_proc, BETA
        ts = int(time.time()) + 110
        payload = signed({"from": "replay-peer", "agent": "remote-scout",
                          "objective": "restart future replay", "ts": ts})
        first = run(payload)
        if first[0] != 200:
            return False, f"setup future-dated dispatch failed ({first[0]})"
        beta_proc.terminate(); beta_proc.wait(timeout=10)
        beta_proc, BETA = boot("beta-home", 8834, home)
        code, body = run(payload)
        return code == 401, f"future-dated dispatch ran, home restarted, replay inside window -> {code} {body if code != 401 else ''}"
    def r3():
        ts = int(time.time()) + 110
        payloads = [signed({"from": "replay-peer", "agent": "remote-scout",
                            "objective": f"distinct concurrent future {i}", "ts": ts}) for i in range(4)]
        outs = []
        def one(p):
            outs.append(run(p))
        threads = [threading.Thread(target=one, args=(p,)) for p in payloads]
        for t in threads: t.start()
        for t in threads: t.join()
        jobs = [o[1].get("job", {}).get("job_id") for o in outs if o[0] == 200]
        ok = len(jobs) == 4 and len(set(jobs)) == 4
        return ok, f"4x concurrent distinct future-dated, same ts: {len(jobs)}x200, {len(set(jobs))} distinct jobs"
    def r4():
        payload = signed({"from": "replay-peer", "agent": "remote-scout",
                          "objective": "normal after future", "ts": int(time.time())})
        code, body = run(payload)
        return code == 200, f"normal dispatch after future-dated traffic -> {code}"

    probe("R1 concurrent identical future-dated replay: exactly one runs", r1)
    probe("R2 future-dated replay refused after restart inside window", r2)
    probe("R3 concurrent distinct future-dated objectives all run once", r3)
    probe("R4 normal dispatch still runs after future-dated traffic (control)", r4)
finally:
    try: beta_proc.terminate()
    except Exception: pass
print(f"{sum(1 for _, ok in results if ok)}/{len(results)} passed")
