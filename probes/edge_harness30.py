#!/usr/bin/env python3
"""Round-30 probes: the Round 29 residual — Kernel.fire_routine.

Read firsthand (runtime.py / routines.py / server.py), the routine
surface before this round:

  fire_routine:    workflow.create_job(...)          # job created
                   ledger.append(routine.fired)      # ATTESTED
                   routines.mark_run(...)            # runs += 1
                   run_agent(...)                    # agents.get by id
                   — NO validation anywhere in the path. create_routine
                   validates the agent exists, but only at CREATION
                   time; an agent deleted afterwards leaves every
                   later fire on the missing-agent path.
  serve() tick:    every 5s, for each due routine, an UNGUARDED
                   thread runs fire_routine (no _guarded wrapper) —
                   a raise in the thread just kills that thread.
  HTTP routes:     POST /api/routines (create — validates via
                   create_routine, 400 on unknown agent, attests
                   nothing), POST /api/routines/<id>/toggle,
                   DELETE /api/routines/<id>, GET /api/routines.
                   There is NO run-now/fire route.

So a deleted agent gives, deterministically (no race needed):
routine.fired attested, a job created and stranded "running",
runs incremented for a run that never happened, and run_agent
raising AgentError to the caller. The mid-fire race (delete landing
between the attestation and run_agent's re-fetch) is the same hole
through the narrower window.

Probes:
  R1  control: normal fire completes, exactly one routine.fired,
      runs == 1, ledger ok
  R2  deterministic: agent deleted after routine creation, fire
      TWICE — both must refuse cleanly: no routine.fired, no
      job.created, runs stays 0
  R3  storm: 24 fires while a deleter thread loops delete+recreate.
      Pass iff every routine.fired's job completed (0 false
      attestations, 0 stranded) and refusals were clean
  R4  targeted: watcher deletes the agent the moment a
      routine.fired entry for the routine appears (after
      attestation, before run_agent's re-fetch), 10 attempts
  R5  bookkeeping: runs/last_run move only on real fires — a
      refused fire (agent gone) must not increment runs
  R6  HTTP surface: create with unknown agent -> 400 and nothing
      created; valid create -> 201 and attests nothing; run-now
      route -> 404 (documented absence); delete -> 200
  R7  tick loop: server with a due routine whose agent is deleted —
      ticks must not attest routine.fired, must not increment
      runs, and the server stays responsive
  R8  after all of it: ledger verifies and a normal fire still
      completes on a fresh kernel
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
            payload = (json.dumps({"message": {"content": "routine reporting in"}}).encode() + b"\n"
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

def fired(k, routine_id=None):
    return [e for e in k.ledger.read() if e["kind"] == "routine.fired"
            and (routine_id is None or e["raw"].get("routine") == routine_id)]

def created_jobs(k):
    return [e for e in k.ledger.read() if e["kind"] == "job.created"]

def job_status(k, job_id):
    try: return k.workflow.get(job_id)["status"]
    except Exception: return "missing"

def false_and_stranded(k, routine_id=None):
    f = fired(k, routine_id)
    false_att = sum(1 for e in f if job_status(k, e["raw"]["job_id"]) != "completed")
    stranded = sum(1 for e in f if job_status(k, e["raw"]["job_id"]) == "running")
    return len(f), false_att, stranded

def ensure(k, agent_id, name, instructions):
    try: k.agents.get(agent_id)
    except AgentError: k.agents.create(name, instructions)

def make_deleter(k, agent_id, name, instructions):
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

# ---------------------------------------------------------------- R1/R2
k1 = Kernel(tmp())
k1.agents.create("Routine Solo", "You run on a schedule.")
r_solo = k1.create_routine("Solo Pulse", "routine-solo", "solo routine control", 60)

def r1():
    job = k1.fire_routine(r_solo, base_url=STUB)
    att, false_att, stranded = false_and_stranded(k1, r_solo["id"])
    runs = k1.routines.get(r_solo["id"])["runs"]
    ok = (job["status"] == "completed" and att == 1 and false_att == 0
          and stranded == 0 and runs == 1 and k1.ledger.verify()["ok"])
    return ok, (f"fire -> {job['status']}, routine.fired={att} (false={false_att}, stranded={stranded}), "
                f"runs={runs}, ledger ok={k1.ledger.verify()['ok']} (want completed, 1, 0, 0, 1, True)")

k1.agents.create("Routine Gone", "About to be deleted.")
r_gone = k1.create_routine("Gone Pulse", "routine-gone", "fire a deleted agent", 60)
k1.agents.delete("routine-gone")

def r2():
    att0 = len(fired(k1, r_gone["id"])); jobs0 = len(created_jobs(k1))
    raises = []
    for _ in range(2):
        try:
            k1.fire_routine(r_gone, base_url=STUB); raises.append(None)
        except AgentError as e:
            raises.append(str(e))
        except Exception as e:  # noqa: BLE001
            raises.append(f"{type(e).__name__}: {e}")
    att = len(fired(k1, r_gone["id"])) - att0
    jobs = len(created_jobs(k1)) - jobs0
    runs = k1.routines.get(r_gone["id"])["runs"]
    _, _, stranded_all = false_and_stranded(k1)
    ok = (all(r is not None and "unknown agent" in r for r in raises)
          and att == 0 and jobs == 0 and runs == 0)
    return ok, (f"two fires of a dead-agent routine -> raises={raises}, routine.fired +{att}, "
                f"job.created +{jobs}, runs={runs} (want 2 clean AgentErrors, +0, +0, 0)")

probe("R1 normal routine fire completes, one attestation (control)", r1)
probe("R2 agent deleted before fire: clean refusal, no attestation, no job, no runs", r2)

# ---------------------------------------------------------------- R3 storm
k2 = Kernel(tmp())
k2.agents.create("Routine Race", "I get deleted mid-fire.")
r_race = k2.create_routine("Race Pulse", "routine-race", "storm routine fire", 60)

def r3():
    dt, stop = make_deleter(k2, "routine-race", "Routine Race", "I get deleted mid-fire.")
    dt.start()
    completed = raised = 0
    try:
        for i in range(24):
            try:
                job = k2.fire_routine(r_race, base_url=STUB)
                if job["status"] == "completed": completed += 1
            except AgentError:
                raised += 1
    finally:
        stop.set(); dt.join(timeout=10)
    ensure(k2, "routine-race", "Routine Race", "I get deleted mid-fire.")
    att, false_att, stranded = false_and_stranded(k2, r_race["id"])
    clean_refusals = raised - false_att
    ok = false_att == 0 and stranded == 0 and att == completed and clean_refusals >= 0 \
        and completed + clean_refusals == 24
    return ok, (f"24 fires under delete-storm: attested={att} (FALSE={false_att}, stranded={stranded}), "
                f"completed={completed}, raises={raised} of which clean pre-attestation refusals={clean_refusals}")

probe("R3 routine fire delete-storm: no false attestations, no stranded jobs", r3)

# ---------------------------------------------------------------- R4 targeted
k3 = Kernel(tmp())
k3.agents.create("Routine Target", "Deleted right after attestation.")
r_tgt = k3.create_routine("Target Pulse", "routine-target", "targeted routine fire", 60)

def r4():
    parents = []
    for i in range(10):
        ensure(k3, "routine-target", "Routine Target", "Deleted right after attestation.")
        base_count = len(fired(k3, r_tgt["id"]))
        holder = {}
        def run_fire(holder=holder):
            try:
                holder["job"] = k3.fire_routine(r_tgt, base_url=STUB)
            except AgentError:
                holder["job"] = {"status": "refused"}
            except Exception as e:  # noqa: BLE001
                holder["job"] = {"status": f"EXC {type(e).__name__}"}
        ft = threading.Thread(target=run_fire, daemon=True); ft.start()
        deadline = time.time() + 30
        while time.time() < deadline:
            if len(fired(k3, r_tgt["id"])) > base_count:
                try: k3.agents.delete("routine-target")
                except AgentError: pass
                break
            if not ft.is_alive(): break
            time.sleep(0.0005)
        ft.join(timeout=60)
        parents.append(holder.get("job", {}).get("status", "no-result"))
        ensure(k3, "routine-target", "Routine Target", "Deleted right after attestation.")
    att, false_att, stranded = false_and_stranded(k3, r_tgt["id"])
    ok = false_att == 0 and stranded == 0
    return ok, (f"10 targeted delete-after-attestation fires: parents={ {s: parents.count(s) for s in set(parents)} }, "
                f"attested={att} (FALSE={false_att}, stranded={stranded})")

probe("R4 targeted delete-after-attestation: no false routine.fired", r4)

# ---------------------------------------------------------------- R5 bookkeeping
k4 = Kernel(tmp())
k4.agents.create("Routine Books", "Bookkeeping subject.")
r_books = k4.create_routine("Books Pulse", "routine-books", "bookkeeping fire", 60)

def r5():
    j1 = k4.fire_routine(r_books, base_url=STUB)
    after_ok = k4.routines.get(r_books["id"])
    k4.agents.delete("routine-books")
    try: k4.fire_routine(r_books, base_url=STUB)
    except AgentError: pass
    after_refused = k4.routines.get(r_books["id"])
    ensure(k4, "routine-books", "Routine Books", "Bookkeeping subject.")
    j2 = k4.fire_routine(r_books, base_url=STUB)
    after_ok2 = k4.routines.get(r_books["id"])
    # Sibling deterministic case: the routine RECORD is deleted
    # before firing (agent alive). Pre-fix this attests
    # routine.fired, then mark_run raises RoutineError and the job
    # strands; post-fix it must refuse before attesting.
    att0 = len(fired(k4, r_books["id"]))
    k4.routines.delete(r_books["id"])
    rec_raise = None
    try: k4.fire_routine(r_books, base_url=STUB)
    except Exception as e:  # noqa: BLE001
        rec_raise = type(e).__name__
    att_delta = len(fired(k4, r_books["id"])) - att0
    ok = (j1["status"] == "completed" and after_ok["runs"] == 1 and after_ok["last_run"] > 0
          and after_refused["runs"] == 1 and after_refused["last_run"] == after_ok["last_run"]
          and j2["status"] == "completed" and after_ok2["runs"] == 2
          and rec_raise == "RoutineError" and att_delta == 0)
    return ok, (f"runs: after good fire={after_ok['runs']}, after refused fire={after_refused['runs']} "
                f"(last_run unchanged={after_refused['last_run'] == after_ok['last_run']}), "
                f"after next good fire={after_ok2['runs']} (want 1, 1, True, 2); "
                f"deleted-record fire -> {rec_raise}, routine.fired +{att_delta} (want RoutineError, +0)")

probe("R5 bookkeeping: runs/last_run move only on real fires", r5)

# ---------------------------------------------------------------- R6/R7 HTTP + tick
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
    errlog = open(f"/tmp/harness30-server-{port}.log", "ab")
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
web_proc, WEB = boot("web-home", 8842, home)
try:
    call(WEB, "POST", "/api/agents", {"name": "Web Routine", "instructions": "Web routine agent."})

    def web_fired():
        code, ledger = call(WEB, "GET", "/api/ledger?limit=500")
        assert code == 200, f"ledger read failed ({code})"
        return [e for e in ledger["entries"] if e.get("kind") == "routine.fired"]

    def r6():
        fired0 = len(web_fired())
        code_bad, _ = call(WEB, "POST", "/api/routines",
                           {"name": "Bad Pulse", "agent": "no-such-agent",
                            "objective": "never runs", "interval_seconds": 60})
        code_ok, resp = call(WEB, "POST", "/api/routines",
                             {"name": "Web Pulse", "agent": "web-routine",
                              "objective": "web routine", "interval_seconds": 60})
        rid = (resp.get("routine") or {}).get("id")
        code_run, _ = call(WEB, "POST", f"/api/routines/{rid}/run", {})
        code_list, listing = call(WEB, "GET", "/api/routines")
        names = [r.get("name") for r in listing.get("routines", [])]
        code_del, _ = call(WEB, "DELETE", f"/api/routines/{rid}")
        att = len(web_fired()) - fired0
        ok = (code_bad == 400 and code_ok == 201 and code_run == 404
              and code_list == 200 and "Web Pulse" in names and "Bad Pulse" not in names
              and code_del == 200 and att == 0)
        return ok, (f"create unknown-agent -> {code_bad}, create valid -> {code_ok}, "
                    f"POST /run-now -> {code_run} (no such route), delete -> {code_del}, "
                    f"routine.fired from HTTP surface +{att} (want 400, 201, 404, 200, +0)")

    def r7():
        call(WEB, "POST", "/api/agents", {"name": "Tick Gone", "instructions": "Deleted before the tick."})
        code, resp = call(WEB, "POST", "/api/routines",
                          {"name": "Tick Pulse", "agent": "tick-gone",
                           "objective": "tick fire of a dead agent", "interval_seconds": 10})
        rid = (resp.get("routine") or {}).get("id")
        call(WEB, "DELETE", "/api/agents/tick-gone")
        fired0 = len([e for e in web_fired() if e["raw"].get("routine") == rid])
        # ticks run every 5s and the routine is due immediately
        # (last_run=0); give the loop at least two ticks.
        deadline = time.time() + 16
        while time.time() < deadline:
            if len([e for e in web_fired() if e["raw"].get("routine") == rid]) > fired0:
                break
            time.sleep(0.5)
        att = len([e for e in web_fired() if e["raw"].get("routine") == rid]) - fired0
        code_list, listing = call(WEB, "GET", "/api/routines")
        mine = [r for r in listing.get("routines", []) if r.get("id") == rid]
        runs = mine[0]["runs"] if mine else -1
        code_status, _ = call(WEB, "GET", "/api/status")
        ok = att == 0 and runs == 0 and code_status == 200
        return ok, (f"~2 ticks with a dead agent: routine.fired +{att}, runs={runs}, "
                    f"/api/status -> {code_status} (want +0, 0, 200)")

    probe("R6 HTTP routine surface: create validates, no run-now route, delete works", r6)
    probe("R7 tick loop with dead agent: no attestation, no runs, server alive", r7)
finally:
    try: web_proc.terminate()
    except Exception: pass

# ---------------------------------------------------------------- R8
def r8():
    k = Kernel(tmp())
    k.agents.create("Routine Final", "Final control.")
    r = k.create_routine("Final Pulse", "routine-final", "final routine control", 60)
    job = k.fire_routine(r, base_url=STUB)
    att, false_att, stranded = false_and_stranded(k, r["id"])
    ok = job["status"] == "completed" and att == 1 and false_att == 0 \
        and stranded == 0 and k.ledger.verify()["ok"]
    return ok, f"fresh-kernel fire -> {job['status']}, fired={att} (false={false_att}, stranded={stranded}), ledger ok={k.ledger.verify()['ok']}"

probe("R8 fresh kernel: ledger verifies, normal fire still completes", r8)
print(f"{sum(1 for _, ok in results if ok)}/{len(results)} passed")
