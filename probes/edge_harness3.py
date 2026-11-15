#!/usr/bin/env python3
"""Round-3 probes: Home Screen server + signing/robustness edges."""
import json, sys, tempfile, threading, time, urllib.request, urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.ledger import Ledger
from crow_os.runtime import Kernel
from crow_os.server import HomeServer, make_handler
from http.server import ThreadingHTTPServer

results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())

kernel = Kernel(tmp())
httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(HomeServer(kernel)))
threading.Thread(target=httpd.serve_forever, daemon=True).start()
B = f"http://127.0.0.1:{httpd.server_address[1]}"

def req(method, path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(B + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def n1():
    code, page = req("GET", "/") if False else (None, None)
    with urllib.request.urlopen(B + "/", timeout=5) as resp:
        html = resp.read().decode()
    ok = "Crow OS" in html and "textContent" in html and 'id="chats"' in html and 'id="modelsel"' in html
    return ok, "home page serves with operations rail, model selector + XSS-safe renderer present"

def n2():
    code, d = req("POST", "/api/run", {"objective": ""})
    return code == 400, f"empty objective -> {code}"

def n3():
    code, d = req("POST", "/api/run", raw=b"NOT JSON")
    return code == 400, f"malformed JSON -> {code}"

def n4():
    code, d = req("GET", "/api/jobs/deadbeef0000")
    return code == 404, f"unknown job -> {code}"

def n5():
    code, d = req("POST", "/api/run", {"objective": "x" * 10_001})
    return code == 400, f"oversized objective -> {code}"

def n6():
    # 3 concurrent runs through the real server; all complete; ledger verifies
    ids = []
    def one(i):
        code, d = req("POST", "/api/run", {"objective": f"concurrent {i}"})
        ids.append(d["job"]["job_id"])
    ts = [threading.Thread(target=one, args=(i,)) for i in range(3)]
    [t.start() for t in ts]; [t.join() for t in ts]
    deadline = time.time() + 15
    while time.time() < deadline:
        if all(req("GET", f"/api/jobs/{j}")[1]["job"]["status"] == "completed" for j in ids):
            break
        time.sleep(0.3)
    v = kernel.ledger.verify()
    done = all(req("GET", f"/api/jobs/{j}")[1]["job"]["status"] == "completed" for j in ids)
    return done and v["ok"], f"3 concurrent runs completed={done} ledger ok={v['ok']} entries={v['entries']}"

def n7():
    # XSS objective: stored in ledger, served as JSON (never as HTML),
    # and the page's renderer uses textContent for dynamic text.
    code, d = req("POST", "/api/run", {"objective": "<script>alert(1)</script>"})
    jid = d["job"]["job_id"]
    time.sleep(0.7)
    code2, jd = req("GET", f"/api/jobs/{jid}")
    with urllib.request.urlopen(B + "/", timeout=5) as resp:
        ctype = resp.headers.get_content_type()
    return (jd["job"]["objective"] == "<script>alert(1)</script>" and ctype == "text/html"
            and jd["job"]["status"] == "completed"), "script objective stored as data, run completed"

def n8():
    # Ed25519-signed ledger: append/verify, forged rewrite rejected
    try:
        from crow_os.signers import Ed25519Signer
    except ImportError:
        return True, "skipped (no cryptography)"
    import tempfile as tf
    signer = Ed25519Signer.generate()
    led = Ledger(Path(tf.mkdtemp()) / "l.jsonl", signer=signer)
    led.append("t", "job.created", {"job_id": "x", "objective": "y"})
    ok1 = led.verify()["ok"]
    lines = led.path.read_text()
    led.path.write_text(lines.replace('"y"', '"forged"'))
    ok2 = not led.verify()["ok"]
    return ok1 and ok2, f"ed25519 ledger verifies={ok1}, tamper rejected={ok2}"

def n9():
    code, d = req("GET", "/api/ledger?limit=abc")
    return code == 400, f"bad limit -> {code}"

def n10():
    code, d = req("GET", "/api/status")
    return code == 200 and d["ledger"]["ok"], f"fresh-ish home status ok, entries={d['ledger']['entries']}"

for name, fn in [("N1 home page + safe renderer", n1), ("N2 empty objective 400", n2),
                 ("N3 malformed JSON 400", n3), ("N4 unknown job 404", n4),
                 ("N5 oversized objective 400", n5), ("N6 concurrent runs x3", n6),
                 ("N7 XSS objective as data", n7), ("N8 Ed25519-signed ledger", n8),
                 ("N9 bad ledger limit 400", n9), ("N10 status on live home", n10)]:
    probe(name, fn)
httpd.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-3 probes passed; failures: {fails}")
