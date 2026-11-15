#!/usr/bin/env python3
"""Round-7 probes: network tooling — gate, SSRF guard, caps, end-to-end."""
import json, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.capabilities import Broker, CapabilityError
from crow_os.ledger import Ledger
from crow_os.runtime import Kernel
from crow_os.tools import ToolError, ToolRunner, _GuardedRedirectHandler, MAX_BYTES
from crow_os.workflow import Workflow
from crow_os.llm import OllamaClient
from crow_os.worker import LLMWorker

SECRET = b"probe-secret-probe-secret-32byte"
results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())

HITS = {"count": 0}
class Target(BaseHTTPRequestHandler):
    def do_GET(self):
        HITS["count"] += 1
        if self.path == "/page":
            body = b"<html>TARGET-MARKER hello from the network</html>"
        elif self.path == "/big":
            body = b"x" * 200_000
        elif self.path == "/slow":
            time.sleep(3); body = b"slow"
        elif self.path == "/redir":
            self.send_response(302); self.send_header("Location", "/page")
            self.end_headers(); return
        else:
            body = b"other"
        self.send_response(200); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self, *a): pass

tgt = HTTPServer(("127.0.0.1", 0), Target)
threading.Thread(target=tgt.serve_forever, daemon=True).start()
T = f"http://127.0.0.1:{tgt.server_address[1]}"

def runner(allow=True, rights=("net.fetch", "net.resolve"), timeout=10.0):
    b = Broker(SECRET)
    cap = b.issue("agent", [{"method": "TOOL", "resource": r} for r in rights], rate_limit=50)
    return ToolRunner(b, cap, allow_private=allow, timeout=timeout)

def t1():
    r = runner()
    out = r.fetch(T + "/page")
    return out["status"] == 200 and "TARGET-MARKER" in out["excerpt"], f"fetch ok status={out['status']} bytes={out['bytes']}"

def t2():
    before = HITS["count"]
    r = runner(rights=("net.resolve",))  # no net.fetch right
    try:
        r.fetch(T + "/page")
        return False, "fetch succeeded without the TOOL net.fetch right"
    except CapabilityError:
        return HITS["count"] == before, f"denied at the gate; target hits unchanged ({HITS['count'] - before} new)"

def t3():
    r = runner(allow=False)
    try:
        r.fetch("http://169.254.169.254/latest/meta-data")
        return False, "metadata address fetched"
    except ToolError as e:
        return "non-public" in str(e), f"metadata refused: {e}"

def t4():
    r = runner(allow=False)
    try:
        r.fetch(T + "/page")
        return False, "loopback fetched under default guard"
    except ToolError:
        return True, "loopback refused under default guard"

def t5():
    h = _GuardedRedirectHandler(allow_private=False)
    class R: pass
    try:
        h.redirect_request(R(), None, 302, "Found", {}, "http://127.0.0.1:9/secret")
        return False, "redirect to loopback allowed"
    except ToolError:
        return True, "redirect hop re-validated and refused"

def t6():
    r = runner()
    out = r.fetch(T + "/big")
    return out["truncated"] and len(out["excerpt"]) <= 4000, f"200KB response capped: bytes={out['bytes']} excerpt={len(out['excerpt'])} chars"

def t7():
    r = runner(timeout=0.5)
    try:
        r.fetch(T + "/slow")
        return False, "slow fetch not timed out"
    except ToolError:
        return True, "timeout -> ToolError (fail closed)"

def t8():
    r = runner()
    bad = 0
    for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x/"):
        try: r.fetch(url)
        except ToolError: bad += 1
    return bad == 3, f"non-http(s) schemes refused: {bad}/3"

def t9():
    # End-to-end: stub Ollama + objective with a URL. Kernel with the
    # operator switch ON for this intranet target: the coder must fetch
    # (ledgered) and the artifact must carry the fetched marker.
    class Stub(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            user = body["messages"][-1]["content"]
            if "Plan reference line:" in user:
                ref = user.split("Plan reference line:")[-1].strip()
                marker = "TARGET-MARKER" if "TARGET-MARKER" in user else "NO-CONTEXT"
                text = f"# artifact {marker}\n{ref}\n"
            else:
                text = "plan"
            out = json.dumps({"message": {"content": text}}).encode()
            self.send_response(200); self.send_header("Content-Length", str(len(out)))
            self.end_headers(); self.wfile.write(out)
        def log_message(self, *a): pass
    stub = HTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    try:
        k = Kernel(tmp(), allow_private_tools=True)
        job = k.run_llm(f"summarize {T}/page please",
                        base_url=f"http://127.0.0.1:{stub.server_address[1]}")
        invoked = [e for e in k.ledger.read() if e["kind"] == "tool.invoked"]
        artifact = (k.outputs.get(job["job_id"]) or {}).get("artifact", "")
        return (job["status"] == "completed" and len(invoked) == 1
                and invoked[0]["raw"]["host"] == "127.0.0.1"
                and "TARGET-MARKER" in artifact), \
            f"e2e: status={job['status']} tool.invoked={len(invoked)} marker-in-artifact={'TARGET-MARKER' in artifact}"
    finally: stub.shutdown()

def t10():
    r = runner(allow=False)
    try:
        r.resolve("localhost")
        return False, "localhost resolved under default guard"
    except ToolError:
        r2 = runner(allow=True)
        out = r2.resolve("localhost")
        return "127.0.0.1" in out["ips"] or "::1" in out["ips"], f"resolve gated; intranet mode returns {out['ips']}"

for name, fn in [("T1 gated fetch returns content", t1), ("T2 no right, no network", t2),
                 ("T3 metadata address refused", t3), ("T4 loopback refused by default", t4),
                 ("T5 redirect to private refused", t5), ("T6 oversized response capped", t6),
                 ("T7 slow target times out closed", t7), ("T8 non-http schemes refused", t8),
                 ("T9 end-to-end: chat objective fetches via tool", t9), ("T10 resolve gated", t10)]:
    probe(name, fn)
tgt.shutdown()
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-7 probes passed; failures: {fails}")
