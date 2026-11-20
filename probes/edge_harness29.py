#!/usr/bin/env python3
"""Round-29 probes: the Round 28 residual — the validate -> attest ->
re-fetch pattern in spawn_subagent and fleet_run.

Round 28 fixed federation: the handler holds the AgentRegistry lock
across agents.get -> check_replay -> federation_receive, and the
validated definition is captured and passed through run_agent's
`agent` parameter, so attestation and run use the agent as validated.
Two other paths kept the old shape (runtime.py, read firsthand):

  spawn_subagent:  agents.get(agent_id)   # result DISCARDED
                   workflow.create_job(child)
                   ledger.append(agent.spawned)
                   run_agent(...)         # agents.get AGAIN by id
  fleet_run:       agents.get(each aid)   # results DISCARDED
                   create parent + child jobs
                   ledger.append(fleet.member) for each member
                   member threads -> run_agent(...)  # get AGAIN

A delete landing between validation and the re-fetch gives:
  spawn: agent.spawned attested, then run_agent raises AgentError —
         the caller sees a crash for a spawn the ledger says happened,
         and the child job is stranded "running" (never reported on).
  fleet: fleet.member attested, the member thread's run_agent raises,
         fleet_run catches it as a "failed member" — but the child
         job itself is never reported on, so it is stranded "running"
         forever while the fleet parent says it failed.
The HTTP /api/spawn handler duplicates the spawn pattern in
server.py (validate-discard -> create -> attest -> thread ->
run_agent re-fetch), with a wider window (thread start).

Probes (in-process Kernel + stub Ollama, as in harness 20; plus one
real server for the HTTP spawn path, as in harness 28):
  R1  control: normal spawn completes, exactly one agent.spawned,
      child completed, ledger ok
  R2  delete BEFORE spawn: clean AgentError, no agent.spawned added
  R3  spawn storm: 48 spawn_subagent calls in waves of 8 while a
      deleter thread loops delete+recreate of the target agent.
      Pass iff every attested spawn's child completed (zero false
      attestations) and every refusal was clean (raised BEFORE
      attesting: attempts == attested + clean refusals)
  R4  control: normal fleet (2 agents) completes, fleet.member x2,
      both children completed
  R5  delete BEFORE fleet: fleet_run refuses cleanly, no
      fleet.member attestation added
  R6  fleet storm: 12 fleet_runs x 4 members of one agent while the
      deleter storms. Pass iff no attested member's child is left
      stranded (status "running" after fleet_run returned) and every
      attested member completed
  R7  HTTP control: POST /api/spawn -> 202, child completes
  R8  HTTP spawn storm: 32 POST /api/spawn while a deleter thread
      loops DELETE+recreate over HTTP. Pass iff every agent.spawned
      attestation over HTTP belongs to a child that completed
"""
import json, os, subprocess, sys, tempfile, threading, time
import urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

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
            payload = (json.dumps({"message": {"content": "agent reporting in"}}).encode() + b"\n"
                       + json.dumps({"done": True}).encode() + b"\n")
            self.send_response(200); self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        out = json.dumps({"message": {"content": "reply"}}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
stub = HTTPServer(("127.0.0.1", 0), Stub); threading.Thread(target=stub.serve_forever, daemon=True).start()
STUB = f"http://127.0.0.1:{stub.server_address[1]}"

from crow_os.runtime import Kernel
from crow_os.agents import AgentError

def tmp(): return Path(tempfile.mkdtemp())

def entries(k, kind, agent=None):
    return [e for e in k.ledger.read() if e["kind"] == kind
            and (agent is None or e["raw"].get("agent") == agent)]

def child_status(k, child_id):
    try: return k.workflow.get(child_id)["status"]
    except Exception: return "missing"

def make_deleter(k, agent_id, name, instructions):
    """Loop delete+recreate of one agent until stopped (the harness
    28 storm technique, in-process). Returns (thread, stop_event)."""
    stop = threading.Event()
    def loop():
        while not stop.is_set():
            try: k.agents.delete(agent_id)
            except AgentError: pass
            try: k.agents.get(agent_id)
            except AgentError:
                try: k.agents.create(name, instructions)
                except AgentError: pass
    t = threading.Thread(target=loop, daemon=True)
    return t, stop

def ensure(k, agent_id, name, instructions):
    try: k.agents.get(agent_id)
    except AgentError: k.agents.create(name, instructions)

# ---------------------------------------------------------------- R1/R2/R3
k1 = Kernel(tmp())
k1.agents.create("Spawn Solo", "You are a solo spawn target.")
k1.agents.create("Spawn Race", "I get deleted mid-spawn.")
PARENT = k1.workflow.create_job("parent objective", chain=["debugger", "governance"])["job_id"] \
    if False else "parent-job-r1"

def r1():
    before = len(entries(k1, "agent.spawned", "spawn-solo"))
    job = k1.spawn_subagent(PARENT, "spawn-solo", "solo spawn control", base_url=STUB)
    after = len(entries(k1, "agent.spawned", "spawn-solo"))
    ok = (job["status"] == "completed" and after == before + 1
          and k1.ledger.verify()["ok"])
    return ok, f"spawn -> {job['status']}, agent.spawned {before}->{after} (want completed, +1), ledger ok={k1.ledger.verify()['ok']}"

def r2():
    k1.agents.delete("spawn-solo")
    before = len(entries(k1, "agent.spawned", "spawn-solo"))
    try:
        k1.spawn_subagent(PARENT, "spawn-solo", "spawn a deleted agent", base_url=STUB)
        raised = None
    except AgentError as e:
        raised = str(e)
    after = len(entries(k1, "agent.spawned", "spawn-solo"))
    ensure(k1, "spawn-solo", "Spawn Solo", "You are a solo spawn target.")
    ok = raised is not None and after == before
    return ok, f"deleted agent spawn -> AgentError={raised!r}, agent.spawned {before}->{after} (want refusal, +0)"

def r3():
    ensure(k1, "spawn-race", "Spawn Race", "I get deleted mid-spawn.")
    before_ids = {e["raw"]["child_job_id"] for e in entries(k1, "agent.spawned", "spawn-race")}
    dt, stop = make_deleter(k1, "spawn-race", "Spawn Race", "I get deleted mid-spawn.")
    dt.start()
    outcomes = []  # ("completed"|"agent-error"|"other", detail)
    try:
        N, WAVE = 48, 8
        for wave_start in range(0, N, WAVE):
            outs = [None] * WAVE
            def one(idx, i):
                try:
                    job = k1.spawn_subagent(f"storm-parent-{i}", "spawn-race",
                                            f"storm spawn {i}", base_url=STUB)
                    outs[idx] = ("completed" if job["status"] == "completed" else job["status"], job["job_id"])
                except AgentError as e:
                    outs[idx] = ("agent-error", str(e))
                except Exception as e:  # noqa: BLE001
                    outs[idx] = ("other", f"{type(e).__name__}: {e}")
            threads = [threading.Thread(target=one, args=(j, wave_start + j)) for j in range(WAVE)]
            for t in threads: t.start()
            for t in threads: t.join()
            outcomes.extend(outs)
    finally:
        stop.set(); dt.join(timeout=10)
    ensure(k1, "spawn-race", "Spawn Race", "I get deleted mid-spawn.")
    new = [e for e in entries(k1, "agent.spawned", "spawn-race")
           if e["raw"]["child_job_id"] not in before_ids]
    attested = len(new)
    att_completed = sum(1 for e in new if child_status(k1, e["raw"]["child_job_id"]) == "completed")
    att_stranded = sum(1 for e in new if child_status(k1, e["raw"]["child_job_id"]) == "running")
    completed = sum(1 for o in outcomes if o[0] == "completed")
    raised = sum(1 for o in outcomes if o[0] == "agent-error")
    other = [o for o in outcomes if o[0] not in ("completed", "agent-error")]
    false_att = attested - att_completed
    # A spawn that attests and THEN raises is indistinguishable at the
    # caller from a clean refusal — both surface as AgentError. The
    # ledger tells them apart: clean refusals = raises - false_att.
    clean_refusals = raised - false_att
    ok = (false_att == 0 and att_stranded == 0 and not other
          and attested + clean_refusals == len(outcomes))
    return ok, (f"48 spawns under delete-storm: attested={attested} (completed={att_completed}, "
                f"FALSE attested-not-completed={false_att}, stranded-running={att_stranded}), "
                f"raises={raised} of which clean pre-attestation refusals={clean_refusals}, other={other[:2]}")

# ---------------------------------------------------------------- R4/R5/R6
k2 = Kernel(tmp())
k2.agents.create("Fleet One", "Fleet member one.")
k2.agents.create("Fleet Two", "Fleet member two.")
k2.agents.create("Fleet Race", "I get deleted mid-fleet.")
k2.agents.create("Fleet Gone", "About to be deleted.")

def r4():
    before = len(entries(k2, "fleet.member"))
    job = k2.fleet_run("fleet control", ["fleet-one", "fleet-two"], base_url=STUB)
    new = entries(k2, "fleet.member")[before:]
    kids = [child_status(k2, e["raw"]["child_job_id"]) for e in new]
    ok = job["status"] == "completed" and len(new) == 2 and all(s == "completed" for s in kids)
    return ok, f"fleet -> {job['status']}, fleet.member +{len(new)}, children={kids} (want completed, 2, all completed)"

def r5():
    k2.agents.delete("fleet-gone")
    before = len(entries(k2, "fleet.member"))
    try:
        k2.fleet_run("fleet with a deleted member", ["fleet-one", "fleet-gone"], base_url=STUB)
        raised = None
    except AgentError as e:
        raised = str(e)
    after = len(entries(k2, "fleet.member"))
    ok = raised is not None and after == before
    return ok, f"fleet with deleted member -> AgentError={raised!r}, fleet.member {before}->{after} (want refusal, +0)"

def r6():
    ensure(k2, "fleet-race", "Fleet Race", "I get deleted mid-fleet.")
    before_ids = {e["raw"]["child_job_id"] for e in entries(k2, "fleet.member", "fleet-race")}
    # Phase A: continuous delete+recreate storm (as in R3). On the
    # unfixed tree this phase alone attested nothing in its first
    # run — the deleter's duty cycle meant the agent was almost
    # always missing at fleet_run's validation instant, so all 12
    # fleets refused cleanly before attesting. The window itself is
    # AFTER validation, so phase B targets it directly.
    dt, stop = make_deleter(k2, "fleet-race", "Fleet Race", "I get deleted mid-fleet.")
    dt.start()
    parents = []
    try:
        for i in range(12):
            try:
                job = k2.fleet_run(f"storm fleet {i}", ["fleet-race"] * 4, base_url=STUB)
                parents.append(job["status"])
            except AgentError:
                parents.append("refused-clean")
    finally:
        stop.set(); dt.join(timeout=10)
    # Phase B: targeted delete-after-attestation. Each attempt runs
    # fleet_run on a thread while a watcher polls the ledger and
    # deletes the agent the moment the first fleet.member entry for
    # it appears — i.e. after validation and attestation, before the
    # member threads' run_agent re-fetch. No recreate until the
    # fleet has returned.
    targeted_parents = []
    for i in range(16):
        ensure(k2, "fleet-race", "Fleet Race", "I get deleted mid-fleet.")
        base_count = len(entries(k2, "fleet.member", "fleet-race"))
        holder = {}
        def run_fleet(i=i, holder=holder):
            try:
                holder["job"] = k2.fleet_run(f"targeted fleet {i}",
                                             ["fleet-race"] * 4, base_url=STUB)
            except AgentError:
                holder["job"] = {"status": "refused-clean"}
        ft = threading.Thread(target=run_fleet, daemon=True); ft.start()
        deadline = time.time() + 30
        while time.time() < deadline:
            if len(entries(k2, "fleet.member", "fleet-race")) > base_count:
                try: k2.agents.delete("fleet-race")
                except AgentError: pass
                break
            if not ft.is_alive():
                break
            time.sleep(0.001)
        ft.join(timeout=60)
        targeted_parents.append(holder.get("job", {}).get("status", "no-result"))
        ensure(k2, "fleet-race", "Fleet Race", "I get deleted mid-fleet.")
    new = [e for e in entries(k2, "fleet.member", "fleet-race")
           if e["raw"]["child_job_id"] not in before_ids]
    attested = len(new)
    att_completed = sum(1 for e in new if child_status(k2, e["raw"]["child_job_id"]) == "completed")
    att_stranded = sum(1 for e in new if child_status(k2, e["raw"]["child_job_id"]) == "running")
    false_att = attested - att_completed
    ok = att_stranded == 0 and false_att == 0
    return ok, (f"storm phase parents={ {s: parents.count(s) for s in set(parents)} }, "
                f"targeted phase parents={ {s: targeted_parents.count(s) for s in set(targeted_parents)} }, "
                f"attested members={attested} (completed={att_completed}, FALSE={false_att}, stranded-running={att_stranded})")

probe("R1 normal spawn completes, one attestation (control)", r1)
probe("R2 delete before spawn: clean refusal, no attestation", r2)
probe("R3 spawn delete-storm: no false attestations, no stranded children", r3)
probe("R4 normal fleet completes, both members attested+completed (control)", r4)
probe("R5 delete before fleet: clean refusal, no attestation", r5)
probe("R6 fleet delete-storm: no stranded attested members", r6)

# ---------------------------------------------------------------- R7/R8 HTTP
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
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}

def boot(name, port, home):
    errlog = open(f"/tmp/harness29-server-{port}.log", "ab")
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
web_proc, WEB = boot("web-home", 8841, home)
try:
    call(WEB, "POST", "/api/agents", {"name": "Web Solo", "instructions": "Solo web spawn target."})
    call(WEB, "POST", "/api/agents", {"name": "Web Race", "instructions": "I get deleted mid-spawn over HTTP."})

    def web_spawned():
        code, ledger = call(WEB, "GET", "/api/ledger?limit=1000")
        assert code == 200, f"ledger read failed ({code})"
        return [e for e in ledger["entries"] if e.get("kind") == "agent.spawned"]

    def web_job_status(job_id):
        code, jobs = call(WEB, "GET", "/api/jobs")
        assert code == 200, f"jobs read failed ({code})"
        for j in jobs.get("jobs", []):
            if j.get("job_id") == job_id: return j.get("status")
        return "missing"

    def wait_job(job_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            s = web_job_status(job_id)
            if s in ("completed", "failed"): return s
            time.sleep(0.2)
        return web_job_status(job_id)

    def web_agent_exists(agent_id):
        code, listing = call(WEB, "GET", "/api/agents")
        return code == 200 and any(a.get("id") == agent_id for a in listing.get("agents", []))

    def web_ensure():
        if not web_agent_exists("web-race"):
            call(WEB, "POST", "/api/agents",
                 {"name": "Web Race", "instructions": "I get deleted mid-spawn over HTTP."})

    def r7():
        before = len([e for e in web_spawned() if e["raw"].get("agent") == "web-solo"])
        code, resp = call(WEB, "POST", "/api/spawn",
                          {"agent": "web-solo", "objective": "web spawn control", "parent": "web-parent"})
        status = wait_job(resp.get("job_id")) if code == 202 else "no-job"
        after = len([e for e in web_spawned() if e["raw"].get("agent") == "web-solo"])
        ok = code == 202 and status == "completed" and after == before + 1
        return ok, f"POST /api/spawn -> {code}, child -> {status}, agent.spawned {before}->{after} (want 202, completed, +1)"

    def r8():
        web_ensure()
        before_ids = {e["raw"].get("child_job_id") for e in web_spawned()
                      if e["raw"].get("agent") == "web-race"}
        stop = threading.Event()
        def deleter():
            while not stop.is_set():
                call(WEB, "DELETE", "/api/agents/web-race")
                if not web_agent_exists("web-race"):
                    call(WEB, "POST", "/api/agents",
                         {"name": "Web Race", "instructions": "I get deleted mid-spawn over HTTP."})
        dt = threading.Thread(target=deleter, daemon=True); dt.start()
        accepted = []  # child_job_ids from 202s
        refused = 0
        try:
            N, WAVE = 32, 8
            for wave_start in range(0, N, WAVE):
                outs = [None] * WAVE
                def one(idx, i):
                    outs[idx] = call(WEB, "POST", "/api/spawn",
                                     {"agent": "web-race", "objective": f"web storm {i}",
                                      "parent": "web-parent"})
                threads = [threading.Thread(target=one, args=(j, wave_start + j)) for j in range(WAVE)]
                for t in threads: t.start()
                for t in threads: t.join()
                for code, resp in outs:
                    if code == 202: accepted.append(resp.get("job_id"))
                    elif code == 400: refused += 1
                    else: accepted.append(None)
        finally:
            stop.set(); dt.join(timeout=15)
        web_ensure()
        # every accepted child settles (completed or failed via _guarded)
        finals = {}
        for cid in accepted:
            if cid: finals[cid] = wait_job(cid)
        new = [e for e in web_spawned() if e["raw"].get("agent") == "web-race"
               and e["raw"].get("child_job_id") not in before_ids]
        attested = len(new)
        att_completed = sum(1 for e in new if finals.get(e["raw"].get("child_job_id")) == "completed"
                            or web_job_status(e["raw"].get("child_job_id")) == "completed")
        false_att = attested - att_completed
        ok = false_att == 0
        return ok, (f"32 HTTP spawns under delete-storm: 202s={len(accepted)}, clean-400s={refused}, "
                    f"attested={attested} (completed={att_completed}, FALSE attested-not-completed={false_att})")

    probe("R7 HTTP spawn control completes", r7)
    probe("R8 HTTP spawn delete-storm: no false attestations", r8)
finally:
    try: web_proc.terminate()
    except Exception: pass
print(f"{sum(1 for _, ok in results if ok)}/{len(results)} passed")
