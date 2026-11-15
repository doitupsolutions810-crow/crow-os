#!/usr/bin/env python3
"""Round-17 probes: the OS against a hostile or half-broken local Ollama.

Every job must either complete or fail closed with Ollama's real reason, and the
ledger must verify afterwards. No probe may need NO_PROXY.
"""
import json, os, socket, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os import llm
from crow_os.llm import LLMUnavailable, OllamaClient
from crow_os.runtime import Kernel

results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())

MODE = {"kind": "good", "tags": ["qwen2.5:3b"]}
SEEN = []
def nd(*objs): return b"".join(json.dumps(o).encode() + b"\n" for o in objs)

class Stub(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def _send(self, code, body):
        self.send_response(code); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if MODE["kind"] == "badtags": return self._send(200, b"<html>not json</html>")
        self._send(200, json.dumps({"models": [{"name": n} for n in MODE["tags"]]}).encode())
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append(body.get("model")); k = MODE["kind"]
        user = body["messages"][-1]["content"]
        ref = user.split("Plan reference line:")[-1].strip() if "Plan reference line:" in user else ""
        text = (ref + "\nprint('hi')\n") if ref else "1. step\nCriteria: artifact references plan"
        if body.get("model") not in MODE["tags"]:
            return self._send(404, json.dumps({"error": f"model '{body.get('model')}' not found, try pulling it first"}).encode())
        if k == "midstream_error":
            return self._send(200, nd({"message": {"content": "par"}}, {"error": "llama runner process has terminated"}))
        if k == "truncated":
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"message": {"content": "half'); self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR); return
        if k == "weird_text":
            text = text + "\x00\u202e\ud7ff \U0001F680 \r\n\x1b[31m"
        self._send(200, nd({"message": {"content": text}}, {"done": True}))
    def log_message(self, *a): pass

srv = HTTPServer(("127.0.0.1", 0), Stub)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}"
DEAD_PROXY = {"http_proxy": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
              "https_proxy": "http://127.0.0.1:9", "no_proxy": "", "NO_PROXY": ""}

def run(kind, model=None, tags=("qwen2.5:3b",)):
    MODE.update(kind=kind, tags=list(tags)); SEEN.clear()
    k = Kernel(tmp())
    with mock.patch.dict(os.environ, DEAD_PROXY), mock.patch.object(llm, "MODEL_FROM_ENV", False):
        job = k.run_llm("probe objective", model=model, base_url=URL)
    return job, k.ledger.verify()

def w1():
    job, v = run("good")
    return job["status"] == "completed" and v["ok"], f"dead proxy in env: status={job['status']} ledger_ok={v['ok']}"
def w2():
    job, v = run("good", tags=("llama3.2:3b", "nomic-embed-text:latest"))
    return job["status"] == "completed" and SEEN and set(SEEN) == {"llama3.2:3b"}, \
        f"default not pulled -> used {sorted(set(SEEN))} status={job['status']}"
def w3():
    job, v = run("good", model="ghost:70b")
    err = job.get("error") or ""
    return job["status"] == "failed" and "not found" in err and v["ok"], f"explicit missing model: {err[:90]}"
def w4():
    job, v = run("midstream_error")
    err = job.get("error") or ""
    return job["status"] == "failed" and "terminated" in err and v["ok"], f"mid-stream error: {err[:90]}"
def w5():
    job, v = run("truncated")
    return job["status"] == "failed" and "LLMUnavailable" in (job.get("error") or "") and v["ok"], \
        f"truncated stream: {(job.get('error') or '')[:90]}"
def w6():
    job, v = run("weird_text")
    return job["status"] in ("completed", "failed") and v["ok"], f"NUL/bidi/ESC/emoji output: status={job['status']} ledger_ok={v['ok']}"
def w7():
    MODE.update(kind="badtags")
    try:
        OllamaClient(base_url=URL, model="x").health(); return False, "no error"
    except LLMUnavailable as e:
        return True, f"html /api/tags -> LLMUnavailable ({str(e)[:50]})"
def w8():
    MODE.update(kind="good", tags=["nomic-embed-text:latest"])
    from crow_os.server import HomeServer
    home = HomeServer(Kernel(tmp()))
    with mock.patch("crow_os.server.OllamaClient", lambda *a, **kw: OllamaClient(base_url=URL)), \
            mock.patch("crow_os.server.MODEL_FROM_ENV", False), mock.patch.dict(os.environ, DEAD_PROXY):
        r = home.start_run("hello", "auto")
    return r["mode"] == "demo", f"only an embedding model pulled -> auto mode={r['mode']}"

for name, fn in [("W1 local Ollama works with a dead proxy in env", w1),
                 ("W2 default model not pulled -> best installed chat model", w2),
                 ("W3 explicit missing model fails closed with Ollama's reason", w3),
                 ("W4 mid-stream Ollama error fails closed", w4),
                 ("W5 truncated stream fails closed", w5),
                 ("W6 hostile characters in model output keep the ledger valid", w6),
                 ("W7 non-JSON /api/tags is LLMUnavailable", w7),
                 ("W8 auto mode with no chat model runs the demo", w8)]:
    probe(name, fn)
srv.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-17 probes passed; failures: {fails}")
