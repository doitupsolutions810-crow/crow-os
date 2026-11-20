#!/usr/bin/env python3
"""Round-23 probes: federation replay vs. a future-dated dispatch.

Round 21/22 proved a replay is refused inside the 120s window — but
both rounds only ever signed bodies with ts == now. verify_incoming
accepts any timestamp within abs(now - ts) <= MAX_SKEW, *including a
future one* (clock skew between homes is the reason for the
tolerance), while check_replay prunes the seen-store by RECEIPT time
(now - recorded > MAX_SKEW). Those two clocks disagree for a
future-dated body:

  sender clock 110s fast -> ts = now + 110, accepted (abs diff 110)
  entry recorded at receipt time T, pruned at T + 120
  replay at T + 130: verify_incoming sees abs diff ~20 -> passes;
  the store entry is already pruned -> the replay RUNS again.

So a captured dispatch from a fast-clocked home gets a second
execution after the window was supposed to have closed (up to ~240s
after receipt). The store entry must be retained until the SIGNED
timestamp — not the receipt — is older than MAX_SKEW.

Probes (one real Crow home, beta, peer key generated here; stub
Ollama as in harnesses 21/22):
  F1 future-dated dispatch runs; the seen-store entry must hold the
     SIGNED ts (not the receipt time) — so the entry survives until
     ts+120 even though receipt+120 passes first — and an immediate
     replay is 401. Old code stored receipt time: at receipt+130 the
     entry is pruned while the body (ts receipt+110) is still
     verifiable, so the replay runs again.
     No time travel is available to the harness, so F1 asserts the
     stored clock directly: value == ts proves retention is keyed to
     the signed timestamp, and the replay-at-receipt+130 outcome then
     follows from the prune rule (kept while now - ts <= 120) plus
     verify_incoming (still passes at abs diff 20).
  F2 control: a stale body (ts 130s old) is refused 401 outright —
     the stale check, not the store, closes the window for normal
     dispatches, so the hole is specific to future-dated bodies.
  F3 control: two distinct future-dated objectives both run 200
     (future ts is accepted, distinct bodies are not conflated)
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
    errlog = open(f"/tmp/harness23-server-{port}.log", "ab")
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
beta_proc, BETA = boot("beta-home", 8833, home)
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

    def f1():
        ts = int(time.time()) + 110
        payload = signed({"from": "replay-peer", "agent": "remote-scout",
                          "objective": "future-dated replay", "ts": ts})
        first = run(payload)
        if first[0] != 200:
            return False, f"setup future-dated dispatch failed ({first[0]}) — future ts not accepted at all"
        seen_path = home / "fs" / "system" / "federation-seen.json"
        seen = json.loads(seen_path.read_text())
        if len(seen) != 1:
            return False, f"expected 1 seen entry, got {len(seen)}"
        stored = next(iter(seen.values()))
        # Retention must be keyed to the signed ts: at receipt+130 the
        # entry is still within ts+120 (20s left). Old code stored the
        # receipt time (~now), so the entry pruned at receipt+120 while
        # the body was still verifiable and the replay ran again.
        ok_value = abs(stored - ts) <= 2
        code, body = run(payload)
        ok = ok_value and code == 401
        return ok, (f"first=200 job={first[1].get('job', {}).get('job_id')}, "
                    f"stored value={stored} vs signed ts={ts} "
                    f"({'ts-keyed' if ok_value else 'RECEIPT-KEYED — entry prunes before the body goes stale'}), "
                    f"immediate replay -> {code} {body if code != 401 else ''}"
                    + ("" if ok else " — REPLAY WINDOW OUTLIVES ITS STORE ENTRY"))
    def f2():
        # A body whose ts is 130s old is stale on arrival: verify_incoming
        # refuses it before check_replay is even reached. This is the
        # normal-dispatch case — the stale check closes its window.
        payload = signed({"from": "replay-peer", "agent": "remote-scout",
                          "objective": "stale control", "ts": int(time.time()) - 130})
        code, body = run(payload)
        return code == 401, f"stale body (ts now-130s) -> {code} {body}"
    def f3():
        ts = int(time.time()) + 110
        payloads = [signed({"from": "replay-peer", "agent": "remote-scout",
                            "objective": f"distinct future {i}", "ts": ts}) for i in range(2)]
        out = [run(p) for p in payloads]
        jobs = [r[1].get("job", {}).get("job_id") for r in out if r and r[0] == 200]
        ok = len(jobs) == 2 and len(set(jobs)) == 2
        return ok, f"distinct future-dated: {len(jobs)}x200, {len(set(jobs))} distinct jobs"

    probe("F1 future-dated replay refused after store entry ages out", f1)
    probe("F2 stale body refused outright (control)", f2)
    probe("F3 distinct future-dated objectives both run (control)", f3)
finally:
    try: beta_proc.terminate()
    except Exception: pass
print(f"{sum(1 for _, ok in results if ok)}/{len(results)} passed")
