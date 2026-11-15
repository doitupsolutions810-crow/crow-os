#!/usr/bin/env python3
"""Round-6 probes: Grok-style frontend + Ollama-backed auto mode + SSE."""
import json, os, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")

# ── stub Ollama with /api/tags and STREAMING /api/chat ──
LAST = {"model": None}
class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/tags":
            out = json.dumps({"models": [{"name": "qwen2.5-coder:32b"},
                                         {"name": "other-model:7b"}]}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out); return
        self.send_response(404); self.end_headers()
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        LAST["model"] = body.get("model")
        user = body["messages"][-1]["content"]
        if "Plan reference line:" in user:
            ref = user.split("Plan reference line:")[-1].strip()
            text = f"# artifact\n{ref}\n"
        else:
            text = "1. plan the thing\n2. build the thing"
        self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for chunk in [text[:8], text[8:16], text[16:]]:
            self.wfile.write(json.dumps({"message": {"content": chunk}}).encode() + b"\n")
            self.wfile.flush(); time.sleep(0.05)
    def log_message(self, *a): pass

stub = HTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=stub.serve_forever, daemon=True).start()
STUB_URL = f"http://127.0.0.1:{stub.server_address[1]}"

# ── boot the real Home Screen server against the stub ──
HOME = tempfile.mkdtemp()
env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), OLLAMA_HOST=STUB_URL)
srv = subprocess.Popen([sys.executable, "-m", "crow_os.cli", "--home", HOME,
                        "serve", "--port", "8796"], env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
B = "http://127.0.0.1:8796"

def req(method, path, body=None, base=B, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def wait_server():
    for _ in range(50):
        try:
            req("GET", "/api/status"); return True
        except Exception: time.sleep(0.2)
    return False

def wait_job(jid, timeout=40):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = req("GET", f"/api/jobs/{jid}")[1]
        if j["job"]["status"] != "running": return j
        time.sleep(0.4)
    return None

def r1():
    wait_server()
    code, d = req("GET", "/api/model")
    return (d["reachable"] and "qwen2.5-coder:32b" in d["installed"]
            and d["active_installed"]), f"installed={d.get('installed')} active_installed={d.get('active_installed')}"

def r2():
    code, d = req("POST", "/api/run", {"objective": "auto mode run"})  # no mode -> auto
    j = wait_job(d["job"]["job_id"])
    if not j: return False, "auto job never settled"
    live = (j["outputs"] or {}).get("live", "")
    return j["job"]["status"] == "completed" and bool((j["outputs"] or {}).get("artifact")) and len(live) > 0, \
        f"auto->llm completed={j['job']['status']} artifact captured, streamed live buffer {len(live)} chars"

def r3():
    code, d = req("POST", "/api/run", {"objective": "sse run", "mode": "demo"})
    jid = d["job"]["job_id"]
    kinds, final = [], None
    with urllib.request.urlopen(B + f"/api/jobs/{jid}/stream", timeout=30) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if line.startswith("data:"):
                ev = json.loads(line[5:])
                kinds.append(ev["entry"]["kind"]); final = ev["job"]["status"]
    return kinds[0] == "job.created" and kinds.count("job.step") == 4 and final == "completed", \
        f"SSE kinds={kinds} final={final}"

def r4():
    code, d = req("POST", "/api/run", {"objective": "model pick", "mode": "llm", "model": "other-model:7b"})
    j = wait_job(d["job"]["job_id"])
    return j and j["job"]["status"] == "completed" and LAST["model"] == "other-model:7b", \
        f"stub saw model={LAST['model']} status={j['job']['status'] if j else None}"

def r5():
    html = (ROOT / "src/crow_os/home.html").read_text()
    checks = {"model selector": 'id="modelsel"' in html, "SSE": "EventSource" in html,
              "build panel": 'id="build"' in html, "thinking card": "think" in html,
              "search": 'id="search"' in html}
    return all(checks.values()), f"frontend features: {checks}"

def r6():
    # Ollama down (nothing on the default port in this sandbox): auto -> demo
    home2 = tempfile.mkdtemp()
    env2 = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    env2["OLLAMA_HOST"] = "127.0.0.1:9"  # down for sure, even on a box running a real Ollama
    srv2 = subprocess.Popen([sys.executable, "-m", "crow_os.cli", "--home", home2,
                             "serve", "--port", "8797"], env=env2,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try: req("GET", "/api/status", base="http://127.0.0.1:8797"); break
            except Exception: time.sleep(0.2)
        code, d = req("POST", "/api/run", {"objective": "offline auto"}, base="http://127.0.0.1:8797")
        jid = d["job"]["job_id"]
        deadline = time.time() + 20
        status = None
        while time.time() < deadline:
            status = req("GET", f"/api/jobs/{jid}", base="http://127.0.0.1:8797")[1]["job"]["status"]
            if status != "running": break
            time.sleep(0.4)
        return status == "completed", f"auto with Ollama down -> demo fallback, status={status}"
    finally:
        srv2.terminate()

for name, fn in [("R1 /api/model lists installed models", r1), ("R2 auto mode runs on Ollama + streams", r2),
                 ("R3 SSE job stream", r3), ("R4 model selector honored", r4),
                 ("R5 frontend is the Grok-style UI", r5), ("R6 auto falls back to demo offline", r6)]:
    probe(name, fn)
srv.terminate(); stub.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-6 probes passed; failures: {fails}")
