#!/usr/bin/env python3
# Live: start `python3 -m crow_os --home /tmp/h serve --port 8811` with Ollama up, then
# python3 probes/live_ollama_session.py http://127.0.0.1:8811 [agents|live|all]. Not in check.sh (needs a model).
"""Live session against a running Crow OS server on real local Ollama.
Drives it the way a user would through Chat, plus hostile agent-API edges."""
import json, sys, time, urllib.request, urllib.error

B = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8811"
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
findings = []


def req(method, path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(B + path, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with op.open(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except Exception as e:  # connection dropped = handler crashed
        return None, {"exc": f"{type(e).__name__}: {e}"}


def check(name, ok, detail):
    print(("PASS " if ok else "FAIL ") + name + "  " + str(detail)[:300], flush=True)
    if not ok:
        findings.append(name)


def wait(job_id, limit=400):
    t0 = time.time()
    while time.time() - t0 < limit:
        c, d = req("GET", f"/api/jobs/{job_id}")
        if c == 200 and d["job"]["status"] != "running":
            return d, time.time() - t0
        time.sleep(2)
    return req("GET", f"/api/jobs/{job_id}")[1], time.time() - t0


phase = sys.argv[2] if len(sys.argv) > 2 else "all"

if phase in ("agents", "all"):
    # --- user creates named agents with custom instructions (the asked-for feature)
    c, d = req("POST", "/api/agents", {
        "name": "Edge Hunter",
        "instructions": "You hunt edge cases in Crow OS. List concrete hostile inputs, then the expected safe behavior for each.",
        "tools": ["net.fetch", "net.resolve"], "avatar": {"shape": "round", "color": "orange"}})
    check("A1 create named agent with instructions + net tools", c == 201, (c, d))
    c, d = req("POST", "/api/agents", {"name": "Fixer", "instructions": "You propose minimal, tested fixes.",
                                        "tools": ["fs.read", "fs.write"]})
    check("A2 second agent", c == 201, (c, d))
    c, d = req("GET", "/api/agents")
    check("A3 agents listed by name", c == 200 and {"Edge Hunter", "Fixer"} <= {a["name"] for a in d.get("agents", [])}, c)

    # --- hostile agent API edges
    for name, body in [
        ("H1 avatar not an object", {"name": "x", "instructions": "y", "avatar": "red"}),
        ("H2 avatar list", {"name": "x", "instructions": "y", "avatar": [1]}),
        ("H3 model not a string", {"name": "x", "instructions": "y", "model": 5}),
        ("H4 tools not a list", {"name": "x", "instructions": "y", "tools": "net.fetch"}),
        ("H5 tools dict", {"name": "x", "instructions": "y", "tools": {"net.fetch": 1}}),
        ("H6 body is a JSON list", None),
        ("H7 name with NUL", {"name": "a\x00b", "instructions": "y"}),
        ("H8 tools with non-string", {"name": "x", "instructions": "y", "tools": [["net.fetch"]]}),
    ]:
        if body is None:
            c, d = req("POST", "/api/agents", raw=b"[1,2]")
        else:
            c, d = req("POST", "/api/agents", body)
        check(name + " -> clean 400", c == 400, (c, d))

    # traversal via agent id: plant a user file and try to delete/read it through the agents API
    for name, path in [("T1 DELETE ../../ escapes agents dir", "/api/agents/..%2F..%2Fhome%2Fnotes"),
                       ("T2 DELETE raw ..", "/api/agents/.."),
                       ("T3 DELETE empty id", "/api/agents/")]:
        c, d = req("DELETE", path)
        check(name + " -> 400/404, never 200/crash", c in (400, 404), (c, d))
    c, d = req("POST", "/api/run", {"objective": "hi", "mode": "demo", "agent": "../agents/edge-hunter"})
    check("T4 run with traversal agent id rejected", c == 400, (c, d))
    c, d = req("POST", "/api/spawn", {"agent": ["x"], "objective": "hi"})
    check("T5 spawn non-string agent -> 400", c == 400, (c, d))
    c, d = req("POST", "/api/spawn", raw=b'"str"')
    check("T6 spawn body not object -> 400", c == 400, (c, d))
    c, d = req("POST", "/api/run", raw=b"[]")
    check("T7 run body not object -> 400", c == 400, (c, d))
    c, d = req("POST", "/api/devices", raw=b"[]")
    check("T8 devices body not object -> 400", c == 400, (c, d))
    c, d = req("POST", "/api/plugins", raw=b"null")
    check("T9 plugins body null -> 400", c == 400, (c, d))
    c, d = req("GET", "/api/status")
    check("T10 server still alive after hostile batch", c == 200, c)

if phase in ("live", "all"):
    ORDER = "Continue Even Longer more robust Test against even more Edge cases and Fix Along the way"
    c, d = req("POST", "/api/run", {"objective": ORDER + " https://example.com/", "mode": "auto",
                                    "agent": "edge-hunter"})
    check("L1 chat order accepted (agent edge-hunter, live model)", c == 202, (c, d))
    jid = (d.get("job") or {}).get("job_id")
    if jid:
        job, secs = wait(jid)
        j = job.get("job", {})
        check("L2 agent run completes on live Ollama", j.get("status") == "completed", (round(secs), j))
        out = (job.get("output") or {})
        print("ARTIFACT:", json.dumps(out)[:1200])
    c, d = req("POST", "/api/run", {"objective": ORDER, "mode": "auto"})
    jid = (d.get("job") or {}).get("job_id")
    job, secs = wait(jid)
    check("L3 4-role chat chain completes on live Ollama", job.get("job", {}).get("status") == "completed", (round(secs), job.get("job")))
    print("OUTPUT:", json.dumps(job.get("output"))[:1500])
    c, d = req("GET", "/api/ledger?limit=500")
    kinds = [e.get("kind") or e.get("type") for e in d.get("entries", [])]
    check("L4 ledger has tool.invoked for the fetch", "tool.invoked" in json.dumps(d.get("entries", [])), kinds[-15:])

print("FINDINGS:", findings)
