#!/usr/bin/env python3
"""Round-12 probes: snapshot match — avatars, connect-apps, bot computer."""
import json, os, subprocess, sys, tempfile, threading, time
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.runtime import Kernel
from crow_os.agents import AgentError

results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())

class Stub(BaseHTTPRequestHandler):
    def do_GET(self):
        out = json.dumps({"models": [{"name": "qwen2.5-coder:32b"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        out = json.dumps({"message": {"content": "ok"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
stub = HTTPServer(("127.0.0.1", 0), Stub); threading.Thread(target=stub.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{stub.server_address[1]}"

def y1():
    home = tmp(); k = Kernel(home)
    a = k.agents.create("Blob", "Be round.", avatar={"shape": "star", "color": "teal"})
    b = k.agents.create("Plain", "Default look.")
    refused = 0
    for bad in ({"shape": "spiky"}, {"color": "neon"}):
        try: k.agents.create("Bad", "x", avatar=bad)
        except AgentError: refused += 1
    k2 = Kernel(home)
    a2 = k2.agents.get(a["id"])
    return (a2["avatar"] == {"shape": "star", "color": "teal"}
            and b["avatar"] == {"shape": "blob", "color": "orange"}
            and refused == 2), "avatar validated, defaulted (blob/orange), persisted"

def y2():
    home = tmp(); k = Kernel(home)
    plugins = k.plugins.list(ollama_reachable=True)
    by_id = {p["id"]: p for p in plugins}
    k.plugins.set_asserted("github", True)
    k2 = Kernel(home)
    after = {p["id"]: p for p in k2.plugins.list(ollama_reachable=False)}
    refused = False
    try: k.plugins.set_asserted("ollama", True)
    except Exception: refused = True
    return (by_id["ollama"]["connected"] and by_id["ollama"]["state"] == "verified"
            and by_id["net-tools"]["state"] == "built-in"
            and after["github"]["connected"] and "your word" in after["github"]["state"]
            and not after["ollama"]["connected"] and refused), \
        "plugins: verified vs built-in vs asserted states are distinct and honest"

def y3():
    k = Kernel(tmp())
    c = k.computer_info()
    return (c["disk_total"] > 0 and 0 <= c["disk_free"] <= c["disk_total"]
            and c["home_bytes"] > 0 and c["ledger"] >= 0), \
        f"computer: {c['disk_free']//(1024**3)}GB free, home {c['home_bytes']}B, ledger {c['ledger']} entries"

def y4():
    home = tmp()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"), OLLAMA_HOST=STUB)
    proc = subprocess.Popen([sys.executable, "-c",
        "import sys; sys.argv=['crow','--home',sys.argv[1],'serve','--port','8814'];"
        " from crow_os.cli import main; main()", str(home)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = "http://127.0.0.1:8814"
        for _ in range(60):
            try: urllib.request.urlopen(base + "/api/status", timeout=2); break
            except Exception: time.sleep(0.2)
        def call(method, path, payload=None):
            req = urllib.request.Request(base + path, method=method,
                data=json.dumps(payload).encode() if payload is not None else None,
                headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=10) as r: return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e: return e.code, json.loads(e.read())
        c1, plugs = call("GET", "/api/plugins")
        c2, comp = call("GET", "/api/computer")
        c3, _ = call("POST", "/api/plugins", {"id": "gmail", "connected": True})
        c4, plugs2 = call("GET", "/api/plugins")
        gmail = [p for p in plugs2["plugins"] if p["id"] == "gmail"][0]
        html = urllib.request.urlopen(base + "/", timeout=5).read().decode()
        return (c1 == 200 and plugs["connected"] >= 4 and c2 == 200
                and comp["computer"]["disk_total"] > 0 and c3 == 200
                and gmail["connected"]
                and 'id="profile"' in html and 'id="plugins"' in html
                and "#ff7a1a" in html), \
            f"HTTP: plugins={c1} (connected={plugs['connected']}) computer={c2} toggle={c3} UI has profile+plugins+orange"
    finally: proc.terminate()

for name, fn in [("Y1 agent avatars validated + persisted", y1),
                 ("Y2 plugin states honest (verified/built-in/asserted)", y2),
                 ("Y3 bot computer numbers real", y3),
                 ("Y4 HTTP: plugins, computer, snapshot UI", y4)]:
    probe(name, fn)
stub.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-12 probes passed; failures: {fails}")
