#!/usr/bin/env python3
# Live round 2: start `python3 -m crow_os --home /tmp/h serve --port 8811` with Ollama up, then
# python3 probes/live_ollama_session2.py http://127.0.0.1:8811 [edges|live|all]. Not in check.sh.
"""Live round 2 against a running Crow OS: SSRF through an agent's net.fetch,
same-name agent races, fs directives on system paths, delegation loops,
SSE edges, size limits; then the hardening order through Chat."""
import json, sys, threading, time, urllib.request, urllib.error

B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8811"
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
findings = []


def req(method, path, body=None, raw=None, headers=None, timeout=30):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    r = urllib.request.Request(B + path, data=data, method=method, headers=h)
    try:
        with op.open(r, timeout=timeout) as resp:
            txt = resp.read()
            try:
                return resp.status, json.loads(txt or b"{}")
            except ValueError:
                return resp.status, {"text": txt[:2000].decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except Exception as e:
        return None, {"exc": f"{type(e).__name__}: {e}"}


def check(name, ok, detail):
    print(("PASS " if ok else "FAIL ") + name + "  " + str(detail)[:400], flush=True)
    if not ok:
        findings.append(name)


def wait(job_id, limit=400):
    t0 = time.time()
    while time.time() - t0 < limit:
        c, d = req("GET", f"/api/jobs/{job_id}")
        if c == 200 and d["job"]["status"] != "running":
            return d
        time.sleep(1.5)
    return req("GET", f"/api/jobs/{job_id}")[1]


def ledger():
    return req("GET", "/api/ledger?limit=500")[1].get("entries", [])


phase = sys.argv[2] if len(sys.argv) > 2 else "all"

if phase in ("edges", "all"):
    c, d = req("POST", "/api/agents", {"name": "Net Scout", "instructions": "Fetch and summarize.",
                                        "tools": ["net.fetch", "net.resolve", "fs.read", "fs.list"],
                                        "model": "qwen2.5:3b"})
    check("E0 create Net Scout (net + fs grants, explicit model)", c == 201, (c, d))

    # --- SSRF through the agent's fetch: every one must be refused, never fetched
    ssrf = ["http://127.0.0.1:11434/api/tags", "http://169.254.169.254/latest/meta-data/",
            "http://[::1]:8811/api/status", "http://0x7f000001:8811/api/status",
            "http://2130706433:8811/api/status", "http://localhost.:8811/api/status",
            "http://127.1:8811/api/status", "http://[::ffff:127.0.0.1]:8811/api/status",
            "http://0.0.0.0:8811/api/status", "http://10.0.0.1/"]
    for u in ssrf:
        before = len([e for e in ledger() if e["kind"] == "tool.invoked"])
        c, d = req("POST", "/api/run", {"objective": f"summarize {u}", "mode": "demo", "agent": "net-scout"})
        jid = (d.get("job") or {}).get("job_id")
        if not jid:
            check(f"S ssrf {u} accepted as job", False, (c, d)); continue
        job = wait(jid, 120)
        inv = [e for e in ledger() if e["kind"] == "tool.invoked"
               and e["raw"].get("job_id") == jid and e["raw"].get("tool") == "net.fetch"]
        fetched_ok = any(e["raw"].get("status") == 200 for e in inv)
        check(f"S ssrf refused {u}", not fetched_ok, (job.get("job", {}).get("status"), job.get("job", {}).get("error"), inv))

    # --- same-name race: 12 parallel creates must yield 12 distinct ids, none overwritten
    res = []
    def mk(i):
        res.append(req("POST", "/api/agents", {"name": "Racer", "instructions": f"I am racer {i}"}))
    ts = [threading.Thread(target=mk, args=(i,)) for i in range(12)]
    [t.start() for t in ts]; [t.join() for t in ts]
    ids = [d["agent"]["id"] for c, d in res if c == 201]
    stored = {a["id"]: a["instructions"] for a in req("GET", "/api/agents")[1]["agents"] if a["name"] == "Racer"}
    check("R1 12 parallel same-name creates -> 12 distinct ids", len(set(ids)) == 12 and len(ids) == 12, sorted(ids))
    check("R2 every create's instructions survive (no overwrite)", len(set(stored.values())) == 12, len(stored))

    # --- fs directives on system paths
    for path in ["/system/agents/edge-hunter.json", "../../broker.secret", "/../ledger.jsonl", "/system"]:
        c, d = req("POST", "/api/run", {"objective": f"fs read: {path}", "mode": "demo", "agent": "net-scout"})
        jid = (d.get("job") or {}).get("job_id")
        job = wait(jid, 200) if jid else {}
        blob = json.dumps(job)
        check(f"F fs read {path}: no broker secret / ledger leak, no crash",
              "BEGIN" not in blob and "prev_hash\\\": " not in blob.replace(json.dumps(job.get('entries', [])), "")
              and job.get("job", {}).get("status") in ("completed", "failed"), job.get("job"))

    # --- delegation loop: agent asked to delegate to itself repeatedly
    c, d = req("POST", "/api/run", {"objective": "delegate to net-scout: delegate to net-scout: loop", "mode": "demo",
                                    "agent": "net-scout"})
    jid = (d.get("job") or {}).get("job_id")
    job = wait(jid, 300) if jid else {}
    check("D1 self-delegation terminates", job.get("job", {}).get("status") in ("completed", "failed"), job.get("job"))

    # --- transport edges
    c, d = req("POST", "/api/run", raw=b"{}" , headers={"Content-Length": "2000000"}, timeout=8)
    check("X1 Content-Length 2MB lie -> 400 fast", c == 400, (c, d))
    c, d = req("GET", "/api/jobs/nope/stream", timeout=8)
    check("X2 SSE unknown job -> 404", c == 404, (c, d))
    for lim in ("1e3", "-5", "99999999999999999999", ""):
        c, d = req("GET", f"/api/ledger?limit={lim}")
        check(f"X3 ledger limit={lim!r} -> 200/400, no crash", c in (200, 400), c)
    c, d = req("GET", "/api/fs/read?path=../../broker.secret")
    check("X4 fs read traversal over HTTP refused", c in (400, 403, 404), (c, d))
    c, d = req("POST", "/api/agents", {"name": "🦅 Сrow", "instructions": "unicode"})
    check("X5 unicode-only name -> usable id", c == 201 and d["agent"]["id"], (c, d))
    c, d = req("GET", "/api/status")
    check("X6 server alive after all edges", c == 200, c)

if phase in ("live", "all"):
    ORDER = "Continue Even Longer more robust Test against even more Edge cases and Fix Along the way"
    c, d = req("POST", "/api/run", {"objective": ORDER + "\nhttps://example.com/\nfs list: /system/agents",
                                    "mode": "auto", "agent": "net-scout"})
    jid = (d.get("job") or {}).get("job_id")
    job = wait(jid)
    tools = [e["raw"].get("tool") for e in job.get("entries", []) if e["kind"] == "tool.invoked"]
    check("L1 Net Scout live run (net.fetch + fs.list) completes", job.get("job", {}).get("status") == "completed"
          and "net.fetch" in tools and "fs.list" in tools, (job.get("job"), tools))
    print("REPORT:", json.dumps(job.get("outputs"))[:1500])
    c, d = req("POST", "/api/run", {"objective": ORDER, "mode": "auto"})
    job = wait((d.get("job") or {}).get("job_id"))
    check("L2 4-role chain live", job.get("job", {}).get("status") == "completed", job.get("job"))

print("FINDINGS:", findings)
