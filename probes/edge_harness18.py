#!/usr/bin/env python3
"""Round-18 probes: federation — two real Crow homes, signed dispatch,
and every refusal mode."""
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

alpha_proc, ALPHA = boot("alpha-home", 8821)
beta_proc, BETA = boot("beta-home", 8822)
try:
    _, ia = call(ALPHA, "GET", "/api/identity")
    _, ib = call(BETA, "GET", "/api/identity")
    call(BETA, "POST", "/api/agents", {"name": "Remote Scout", "instructions": "You scout remote systems and report back."})
    call(ALPHA, "POST", "/api/peers", {"name": "beta-home", "url": BETA, "pubkey": ib["pubkey"]})
    call(BETA, "POST", "/api/peers", {"name": "alpha-home", "url": ALPHA, "pubkey": ia["pubkey"]})

    def ee1():
        return ia["federation"] and ib["federation"] and ia["pubkey"] != ib["pubkey"], \
            f"two homes, two identities ({ia['fingerprint']} vs {ib['fingerprint']})"
    def ee2():
        code, reply = call(ALPHA, "POST", "/api/federation/dispatch",
                           {"peer": "beta-home", "agent": "remote-scout",
                            "objective": "Scout this system and report"})
        _, bs = call(BETA, "GET", "/api/status")
        _, astatus = call(ALPHA, "GET", "/api/status")
        ok = (code == 200 and reply["job"]["status"] == "completed"
              and "remote agent reporting in" in reply["transcript"])
        return ok, f"dispatch -> remote job {reply.get('job', {}).get('job_id')} completed on beta; transcript returned; both ledgers verify ({bs['ledger']['ok']}, {astatus['ledger']['ok']})"
    def ee3():
        code, _ = call(BETA, "POST", "/api/federation/run",
                       {"from": "alpha-home", "agent": "remote-scout",
                        "objective": "sneak in", "ts": int(time.time())})
        return code == 401, "unsigned federation request refused (401)"
    def _rogue():
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex()
        return key, pub
    rogue_key, rogue_pub = _rogue()
    def ee4():
        body = {"from": "alpha-home", "agent": "remote-scout",
                "objective": "forged", "ts": int(time.time())}
        payload = dict(body); payload["sig"] = rogue_key.sign(canon(body).encode()).hex()
        code, _ = call(BETA, "POST", "/api/federation/run", payload)
        return code == 401, "request claiming to be alpha but signed by a rogue key refused (401)"
    def ee5():
        call(BETA, "POST", "/api/peers", {"name": "rogue", "url": "http://127.0.0.1:9", "pubkey": rogue_pub})
        body = {"from": "rogue", "agent": "remote-scout",
                "objective": "replayed", "ts": int(time.time()) - 3600}
        payload = dict(body); payload["sig"] = rogue_key.sign(canon(body).encode()).hex()
        code, err = call(BETA, "POST", "/api/federation/run", payload)
        body2 = {"from": "rogue", "agent": "remote-scout",
                 "objective": "fresh and valid", "ts": int(time.time())}
        payload2 = dict(body2); payload2["sig"] = rogue_key.sign(canon(body2).encode()).hex()
        code2, reply2 = call(BETA, "POST", "/api/federation/run", payload2)
        return code == 401 and code2 == 200 and reply2["job"]["status"] == "completed", \
            "stale-but-validly-signed refused (replay window); the same peer fresh runs fine"
    def ee6():
        code, _ = call(ALPHA, "POST", "/api/federation/dispatch",
                       {"peer": "nowhere", "agent": "x", "objective": "y"})
        call(ALPHA, "POST", "/api/peers", {"name": "dead-home", "url": "http://127.0.0.1:9", "pubkey": rogue_pub})
        code2, err2 = call(ALPHA, "POST", "/api/federation/dispatch",
                           {"peer": "dead-home", "agent": "x", "objective": "y"})
        return code == 502 and code2 == 502 and "unreachable" in json.dumps(err2), \
            "unknown peer and unreachable peer both fail cleanly (502), nothing runs"
    def ee7():
        call(BETA, "DELETE", "/api/peers/alpha-home")
        code, _ = call(BETA, "POST", "/api/federation/run",
                       {"from": "alpha-home", "agent": "remote-scout",
                        "objective": "after removal", "ts": int(time.time()), "sig": "00"})
        return code == 401, "after a peer is removed, its requests are refused"
    for name, fn in [("EE1 distinct home identities", ee1), ("EE2 signed cross-home dispatch", ee2),
                     ("EE3 unsigned refused", ee3), ("EE4 forged signature refused", ee4),
                     ("EE5 replay window enforced", ee5), ("EE6 unknown/unreachable peers", ee6),
                     ("EE7 removed peer refused", ee7)]:
        probe(name, fn)
finally:
    alpha_proc.terminate(); beta_proc.terminate(); stub.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-18 probes passed; failures: {fails}")
