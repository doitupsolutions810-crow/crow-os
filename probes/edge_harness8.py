#!/usr/bin/env python3
"""Round-8 probes: CrowFS — the OS file system, its guards and gates."""
import os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from crow_os.capabilities import Broker, CapabilityError
from crow_os.fs import CrowFS, FSError, MAX_FILE_BYTES
from crow_os.runtime import Kernel
from crow_os.tools import FileTools

SECRET = b"probe-secret-probe-secret-32byte"
results = []
def probe(name, fn):
    try: ok, note = fn()
    except Exception as e: ok, note = False, f"EXC {type(e).__name__}: {e}"
    results.append((name, ok)); print(f"{'PASS' if ok else 'FAIL'}  {name}  {note}")
def tmp(): return Path(tempfile.mkdtemp())

def tools(home, rights):
    b = Broker(SECRET)
    cap = b.issue("agent", [{"method": "TOOL", "resource": r} for r in rights], rate_limit=50)
    return FileTools(b, cap, CrowFS(home))

def u1():
    fs = CrowFS(tmp())
    names = {e["name"] for e in fs.list_dir("/")}
    docs = {e["name"] for e in fs.list_dir("/home/user")}
    return {"system", "home", "apps", "var", "tmp"} <= names and \
        {"Documents", "Downloads", "Pictures", "Music"} <= docs, \
        f"standard tree present: root={sorted(names)}"

def u2():
    t = tools(tmp(), ["fs.write", "fs.read", "fs.list"])
    t.write("/home/user/Documents/note.txt", "hello crow")
    out = t.read("/home/user/Documents/note.txt")
    listed = t.list("/home/user/Documents")
    return out["content"] == "hello crow" and listed["entries"][0]["name"] == "note.txt", "gated write/read/list roundtrip"

def u3():
    home = tmp()
    t = tools(home, ["fs.read"])  # no write right
    try:
        t.write("/home/user/Documents/evil.txt", "x")
        return False, "write succeeded without fs.write"
    except CapabilityError:
        return not CrowFS(home).exists("/home/user/Documents/evil.txt"), "denied at the gate; no file created"

def u4():
    fs = CrowFS(tmp())
    bad = 0
    for p in ("/../broker.secret", "/home/user/../../ledger.jsonl",
              "/home/../../../etc/passwd", "..", "/home/user/../.."):
        try: fs.read_file(p)
        except FSError: bad += 1
    return bad == 5, f"traversal refused: {bad}/5"

def u5():
    home = tmp()
    fs = CrowFS(home)
    outside = tmp() / "host-secret.txt"
    outside.write_text("HOST SECRET")
    os.symlink(outside, home / "fs" / "home" / "user" / "link.txt")
    os.symlink(tmp(), home / "fs" / "home" / "user" / "linkdir")
    read_blocked = write_blocked = False
    try: fs.read_file("/home/user/link.txt")
    except FSError: read_blocked = True
    try: fs.write_file("/home/user/linkdir/pwned.txt", "x")
    except FSError: write_blocked = True
    return read_blocked and write_blocked, f"symlink escape refused (read={read_blocked}, write={write_blocked})"

def u6():
    fs = CrowFS(tmp())
    try:
        fs.write_file("/tmp/big.txt", "x" * (MAX_FILE_BYTES + 1))
        return False, "oversize write accepted"
    except FSError:
        return not fs.exists("/tmp/big.txt"), "oversize refused; no partial file left"

def u7():
    fs = CrowFS(tmp())
    a = b = False
    try: fs.write_file("/home/user/Documents", "x")
    except FSError: a = True
    try: fs.read_file("/home/user")
    except FSError: b = True
    return a and b, f"write-onto-dir refused={a}, read-a-dir refused={b}"

def u8():
    fs = CrowFS(tmp())
    fs.write_file("/var/log/app.log", "v1")
    fs.write_file("/var/log/app.log", "v2-longer")
    litter = [e["name"] for e in fs.list_dir("/var/log") if ".tmp" in e["name"]]
    return fs.read_file("/var/log/app.log") == "v2-longer" and not litter, "atomic replace; no temp litter"

def u9():
    home = tmp()
    k = Kernel(home)
    job = k.run_demo("fs e2e objective")
    saved = (k.outputs.get(job["job_id"]) or {}).get("saved_to", "")
    invoked = [e for e in k.ledger.read() if e["kind"] == "tool.invoked"]
    content = k.fs.read_file(saved) if saved else ""
    return (job["status"] == "completed" and saved.endswith(".md")
            and "artifact" in content and len(invoked) == 1
            and invoked[0]["raw"]["tool"] == "fs.write"), \
        f"artifact persisted to {saved}, attested fs.write x{len(invoked)}"

def u10():
    fs = CrowFS(tmp())
    try:
        content = fs.read_file("/etc/passwd")
        return False, f"virtual /etc/passwd returned content ({len(content)}b) — host leak?"
    except FSError:
        return True, "virtual /etc/passwd is not the host file (no such file in CrowFS)"


def u11():
    # The Files-tab backend over real HTTP: list, read, traversal refused.
    import json as J, subprocess, time, urllib.request, urllib.error
    home = tmp()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import sys; sys.argv=['crow','--home',sys.argv[1],'serve','--port','8811'];"
         " from crow_os.cli import main; main()", str(home)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        base = "http://127.0.0.1:8811"
        for _ in range(50):
            try:
                urllib.request.urlopen(base + "/api/status", timeout=2); break
            except Exception: time.sleep(0.2)
        def get(path):
            try:
                with urllib.request.urlopen(base + path, timeout=5) as r:
                    return r.status, J.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, J.loads(e.read())
        c1, root = get("/api/fs?path=/")
        c2, ver = get("/api/fs/read?path=/system/VERSION")
        c3, trav = get("/api/fs/read?path=/../broker.secret")
        names = {e["name"] for e in root.get("entries", [])}
        return (c1 == 200 and {"system", "home", "apps"} <= names
                and c2 == 200 and "crow-os" in ver.get("content", "")
                and c3 == 400), f"HTTP fs: list={c1} read={c2} traversal={c3}"
    finally:
        proc.terminate()

for name, fn in [("U1 standard tree on first boot", u1), ("U2 gated fs roundtrip", u2),
                 ("U3 no right, no write", u3), ("U4 traversal refused", u4),
                 ("U5 symlink escape refused", u5), ("U6 oversize write refused", u6),
                 ("U7 directory misuse refused", u7), ("U8 atomic writes, no litter", u8),
                 ("U9 kernel persists artifacts, attested", u9), ("U10 no host-file leak", u10), ("U11 fs over HTTP (Files tab backend)", u11)]:
    probe(name, fn)
fails = [n for n, ok in results if not ok]
print(f"\n{len(results)-len(fails)}/{len(results)} round-8 probes passed; failures: {fails}")

