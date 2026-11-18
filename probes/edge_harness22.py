#!/usr/bin/env python3
"""Round-22 probes: federation replay under concurrency and restart.

Round 21 proved a *sequential* replay is refused 401: check_replay()
records the signed body in CrowFS /system/federation-seen.json before
the job runs. It never asked two follow-ups:

1. Concurrency. server.py serves /api/federation/run on
   ThreadingHTTPServer, but PeerStore.check_replay() is an unlocked
   read-modify-write (read the JSON, check the key, add it, write the
   file back). Two identical dispatches arriving at the same moment
   can both read a store that does not yet contain the key, both pass,
   and both run the remote job — the exact replay Round 21 fixed,
   reopened by a race.
2. Restart persistence. The fix's claim is that the store is
   persistent in CrowFS "so a restart does not reopen the window".
   Round 21 never rebooted the home and replayed. If the store were
   (say) only flushed on clean shutdown, or keyed on something
   regenerated at boot, a restart inside the 120s window would
   reopen the hole.

Probes (one real Crow home, beta, peer key generated here — same
rogue-peer construction as Rounds 18/21, signature genuinely valid
for the registered peer; stub Ollama as in harness 21):
  C1 8 identical signed bodies POSTed concurrently -> exactly 1 runs
  C2 dispatch, terminate the server, reboot the SAME home, replay
     the same body -> 401 (store survived the restart)
  C3 4 distinct objectives POSTed concurrently -> all 4 run, distinct
     job ids (control: concurrency must not serialize distinct work)
  C4 corrupt the seen-store file on disk, then replay -> 401 if the
     store fails closed; 200 documents a fail-open on corruption
     (check_replay swallows JSONDecodeError and resets to {})
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
    except Exception as e:  # transport failure: server dropped the connection
        return 0, {"error": f"{type(e).__name__}: {e}"}

def boot(name, port, home):
    errlog = open(f"/tmp/harness22-server-{port}.log", "ab")
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
beta_proc, BETA = boot("beta-home", 8832, home)
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

    def burst(payload, n):
        """POST the same payload n times concurrently; return statuses."""
        barrier = threading.Barrier(n)
        out = [None] * n
        def worker(i):
            barrier.wait(timeout=30)
            out[i] = run(payload)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=90)
        return out

    def c1():
        # Three bursts with fresh bodies each, to catch an intermittent race.
        total_runs = 0; detail = []
        for b in range(3):
            payload = signed({"from": "replay-peer", "agent": "remote-scout",
                              "objective": f"concurrent replay burst {b}", "ts": int(time.time())})
            res = burst(payload, 8)
            runs = [r for r in res if r and r[0] == 200]
            total_runs += len(runs)
            from collections import Counter
            detail.append(f"burst{b}: {len(runs)}x200 / {len(res)} statuses={dict(Counter(r[0] for r in res if r))}")
            if len(runs) > 1:
                detail.append("job ids: " + ",".join(
                    str(r[1].get("job", {}).get("job_id")) for r in runs))
        ok = total_runs == 3
        return ok, "; ".join(detail) + (" — exactly one run per burst" if ok else " — REPLAY RAN MORE THAN ONCE CONCURRENTLY")
    def c2():
        payload = signed({"from": "replay-peer", "agent": "remote-scout",
                          "objective": "restart persistence", "ts": int(time.time())})
        first = run(payload)
        if first[0] != 200:
            return False, f"setup dispatch failed ({first[0]})"
        beta_proc.terminate(); beta_proc.wait(timeout=10)
        proc2, base2 = boot("beta-home", 8832, home)
        try:
            code, body = call(base2, "POST", "/api/federation/run", payload)
            return code == 401, f"after restart replay -> {code} {body}"
        finally:
            proc2.terminate(); proc2.wait(timeout=10)
    def c3():
        ts = int(time.time())
        payloads = [signed({"from": "replay-peer", "agent": "remote-scout",
                            "objective": f"distinct concurrent {i}", "ts": ts}) for i in range(4)]
        barrier = threading.Barrier(4)
        out = [None] * 4
        def worker(i):
            barrier.wait(timeout=30)
            out[i] = run(payloads[i])
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=90)
        jobs = [r[1].get("job", {}).get("job_id") for r in out if r and r[0] == 200]
        ok = len(jobs) == 4 and len(set(jobs)) == 4
        from collections import Counter
        return ok, (f"distinct concurrent: {len(jobs)}x200, {len(set(jobs))} distinct jobs; "
                    f"statuses={dict(Counter(r[0] for r in out if r))}; "
                    f"errors={[r[1] for r in out if r and r[0] == 0]}")
    def c4():
        payload = signed({"from": "replay-peer", "agent": "remote-scout",
                          "objective": "corrupt store replay", "ts": int(time.time())})
        first = run(payload)
        if first[0] != 200:
            return False, f"setup dispatch failed ({first[0]})"
        seen_path = home / "fs" / "system" / "federation-seen.json"
        seen_path.write_text("{corrupt json, not parseable")
        code, body = run(payload)
        return code == 401, f"after corrupting seen-store replay -> {code} {body}"

    probe("C1 concurrent identical dispatch runs exactly once", c1)
    probe("C2 replay refused after restart of same home", c2)
    # C2 rebooted the server; re-point BETA at the new process is handled
    # inside c2's own base2, but the outer BETA proc was terminated there,
    # so re-boot for C3/C4 on the same home.
    beta_proc, BETA = boot("beta-home", 8832, home)
    probe("C3 concurrent distinct objectives all run", c3)
    probe("C4 replay refused after seen-store corruption", c4)
finally:
    try: beta_proc.terminate()
    except Exception: pass
print(f"{sum(1 for _, ok in results if ok)}/{len(results)} passed")
