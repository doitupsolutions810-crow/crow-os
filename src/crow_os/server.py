"""Crow OS Home Screen server (v0.3).

Serves the Home Screen (home.html) backed by the real kernel: runs are
executed by the Kernel (demo runtime, or the Ollama LLM worker) in
threads, and the UI polls job state straight from the signed ledger —
the same event stream governance verifies. Stdlib only.

    crow serve --port 8787
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .llm import DEFAULT_BASE, DEFAULT_MODEL, LLMUnavailable, OllamaClient
from .runtime import Kernel

HOME_HTML = Path(__file__).with_name("home.html")


class HomeServer:
    def __init__(self, kernel: Kernel, port: int | None = None):
        self.kernel = kernel
        self.port = port
        self._threads: list[threading.Thread] = []

    # -- API ------------------------------------------------------------
    def model_info(self) -> dict[str, Any]:
        """What the OS actually has installed: ask Ollama itself."""
        client = OllamaClient()
        try:
            h = client.health()
            return {"reachable": True, "base": DEFAULT_BASE,
                    "active": DEFAULT_MODEL, "installed": h["models"],
                    "active_installed": h["model_present"]}
        except LLMUnavailable as e:
            return {"reachable": False, "base": DEFAULT_BASE,
                    "active": DEFAULT_MODEL, "installed": [],
                    "active_installed": False, "error": str(e)}

    def status(self) -> dict[str, Any]:
        verdict = self.kernel.ledger.verify()
        return {
            "version": __version__,
            "home": str(self.kernel.home),
            "model": DEFAULT_MODEL,
            "ollama": self.model_info(),
            "ledger": {"ok": verdict["ok"], "entries": verdict["entries"],
                       "errors": verdict["errors"][:5]},
            "jobs": len(self.kernel.workflow.jobs()),
            "hosting": {"serving": self.port is not None, "port": self.port},
        }

    def start_run(self, objective: str, mode: str,
                  model: str | None = None,
                  agent_id: str | None = None,
                  tools_enabled: bool = True) -> dict[str, Any]:
        # One wiring path for both modes: the job is created up front
        # (so the caller always gets its id immediately and the run is
        # in the ledger even if the server dies), then the KERNEL's own
        # chain implementation executes it in a thread. There is no
        # second copy of the chain logic here to drift out of sync.
        if agent_id:
            agent = self.kernel.agents.get(agent_id)  # AgentError -> 400
            job_id = self.kernel.workflow.create_job(
                objective, chain=[agent_id, "governance"])
            target = lambda: self.kernel.run_agent(  # noqa: E731
                objective, agent_id, model=model, job_id=job_id,
                tools_enabled=tools_enabled)
            t = threading.Thread(target=self._guarded, args=(job_id, target), daemon=True)
            t.start()
            self._threads.append(t)
            return {"started": True, "mode": "agent", "agent": agent["id"],
                    "job": self.kernel.workflow.get(job_id)}
        if mode == "auto":
            mode = "llm" if self.model_info()["reachable"] else "demo"
        job_id = self.kernel.workflow.create_job(objective)
        if mode == "llm":
            target = lambda: self.kernel.run_llm(objective, model=model, job_id=job_id, tools_enabled=tools_enabled)  # noqa: E731
        else:
            target = lambda: self.kernel.run_demo(objective, job_id=job_id)  # noqa: E731
        t = threading.Thread(target=self._guarded, args=(job_id, target), daemon=True)
        t.start()
        self._threads.append(t)
        return {"started": True, "mode": mode,
                "job": self.kernel.workflow.get(job_id)}

    def _guarded(self, job_id: str, target) -> None:
        try:
            target()
        except Exception as e:  # a dead thread must not strand the job
            try:
                job = self.kernel.workflow.get(job_id)
                if job["status"] == "running":
                    role = self.kernel.workflow.expected_role(job_id)
                    self.kernel.workflow.report(
                        job_id, role, False, f"{type(e).__name__}: {e}")
            except Exception:
                pass

    def job_detail(self, job_id: str) -> dict[str, Any] | None:
        try:
            job = self.kernel.workflow.get(job_id)
        except Exception:
            return None
        entries = [e for e in self.kernel.ledger.read()
                   if (e.get("raw") or {}).get("job_id") == job_id]
        return {"job": job, "entries": entries,
                "outputs": self.kernel.outputs.get(job_id)}


def make_handler(home: HomeServer):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path == "/":
                body = HOME_HTML.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/status":
                self._json(200, home.status())
            elif u.path == "/api/model":
                self._json(200, home.model_info())
            elif u.path.startswith("/api/jobs/") and u.path.endswith("/transcript"):
                job_id = u.path.split("/")[3]
                try:
                    md = home.kernel.transcript(job_id)
                except KeyError:
                    return self._json(404, {"error": "job not found"})
                body = md.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/markdown; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path.startswith("/api/jobs/") and u.path.endswith("/stream"):
                self._stream(u.path.split("/")[3])
            elif u.path == "/api/jobs":
                self._json(200, {"jobs": home.kernel.workflow.jobs()})
            elif u.path.startswith("/api/jobs/"):
                detail = home.job_detail(u.path.rsplit("/", 1)[-1])
                if detail is None:
                    self._json(404, {"error": "no such job"})
                else:
                    self._json(200, detail)
            elif u.path == "/api/connectors":
                from .connectors import CONNECTOR_TOOLS
                self._json(200, {"connectors": [
                    home.kernel.connectors.status(cid) for cid in CONNECTOR_TOOLS]})
            elif u.path == "/api/identity":
                self._json(200, home.kernel.identity_info())
            elif u.path == "/api/peers":
                self._json(200, {"peers": home.kernel.peers.list()})
            elif u.path == "/api/routines":
                self._json(200, {"routines": home.kernel.routines.list()})
            elif u.path.startswith("/api/agents/") and u.path.endswith("/memory"):
                agent_id = u.path.split("/")[3]
                try:
                    home.kernel.agents.get(agent_id)
                except Exception as e:  # noqa: BLE001
                    return self._json(404, {"error": str(e)})
                self._json(200, {"agent": agent_id,
                                 "memory": home.kernel.agent_memory_read(agent_id)})
            elif u.path == "/api/plugins":
                info = home.model_info()
                plugins = home.kernel.plugins.list(
                    info["reachable"],
                    connector_states=home.kernel.connectors.all_states())
                self._json(200, {"plugins": plugins, "connected":
                                 sum(1 for p in plugins if p["connected"])})
            elif u.path == "/api/computer":
                info = home.model_info()
                self._json(200, {"computer": home.kernel.computer_info(),
                                 "hosting": {"serving": home.port is not None,
                                             "port": home.port},
                                 "ollama": info})
            elif u.path == "/api/devices":
                info = home.model_info()
                self._json(200, {"devices": home.kernel.devices.list(
                    {"reachable": info["reachable"],
                     "installed": info["installed"]}),
                    "hosting": {"serving": home.port is not None,
                                "port": home.port}})
            elif u.path == "/api/agents":
                self._json(200, {"agents": home.kernel.agents.list()})
            elif u.path == "/api/fs":
                from .fs import FSError
                q = parse_qs(u.query)
                vp = (q.get("path") or ["/"])[0]
                try:
                    self._json(200, {"path": vp,
                                     "entries": home.kernel.fs.list_dir(vp)})
                except FSError as e:
                    self._json(400, {"error": str(e)})
            elif u.path == "/api/fs/read":
                from .fs import FSError
                q = parse_qs(u.query)
                vp = (q.get("path") or [""])[0]
                try:
                    self._json(200, {"path": vp,
                                     "content": home.kernel.fs.read_file(vp)})
                except FSError as e:
                    self._json(400, {"error": str(e)})
            elif u.path == "/api/ledger":
                limit = 50
                q = parse_qs(u.query)
                if "limit" in q:
                    try:
                        limit = max(1, min(500, int(q["limit"][0])))
                    except ValueError:
                        return self._json(400, {"error": "limit must be an integer"})
                entries = home.kernel.ledger.read()
                self._json(200, {"entries": entries[-limit:]})
            else:
                self._json(404, {"error": "not found"})

        def do_DELETE(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path.startswith("/api/routines/"):
                from .routines import RoutineError
                try:
                    home.kernel.routines.delete(u.path.rsplit("/", 1)[-1])
                    return self._json(200, {"deleted": True})
                except RoutineError as e:
                    return self._json(404, {"error": str(e)})
            if u.path.startswith("/api/peers/"):
                from .federation import FederationError
                try:
                    home.kernel.peers.remove(u.path.rsplit("/", 1)[-1])
                    return self._json(200, {"deleted": True})
                except FederationError as e:
                    return self._json(404, {"error": str(e)})
            if u.path.startswith("/api/agents/"):
                from .agents import AgentError
                try:
                    home.kernel.agents.delete(u.path.rsplit("/", 1)[-1])
                    return self._json(200, {"deleted": True})
                except AgentError as e:
                    return self._json(404, {"error": str(e)})
            return self._json(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            u = urlparse(self.path)
            if u.path == "/api/agents":
                from .agents import AgentError
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    agent = home.kernel.agents.create(
                        payload.get("name"), payload.get("instructions"),
                        model=payload.get("model"),
                        tools=payload.get("tools"),
                        avatar=payload.get("avatar"),
                        extra_tools=home.kernel.connectors.tool_names())
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "body must be JSON"})
                except AgentError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(201, {"agent": agent})
            if u.path == "/api/routines" or u.path.startswith("/api/routines/"):
                from .routines import RoutineError
                from .agents import AgentError
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    if u.path == "/api/routines":
                        routine = home.kernel.create_routine(
                            payload.get("name"), payload.get("agent"),
                            payload.get("objective"),
                            payload.get("interval_seconds"))
                        return self._json(201, {"routine": routine})
                    rid = u.path.split("/")[3]
                    if u.path.endswith("/toggle"):
                        routine = home.kernel.routines.set_enabled(
                            rid, bool(payload.get("enabled", True)))
                        return self._json(200, {"routine": routine})
                    return self._json(404, {"error": "not found"})
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "body must be JSON"})
                except (RoutineError, AgentError) as e:
                    return self._json(400, {"error": str(e)})
            if u.path == "/api/fleet":
                from .agents import AgentError
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    objective = payload.get("objective")
                    agents = payload.get("agents")
                    if not isinstance(objective, str) or not objective.strip():
                        return self._json(400, {"error": "objective must be a non-empty string"})
                    if not isinstance(agents, list) or not agents:
                        return self._json(400, {"error": "agents must be a non-empty list"})
                    for aid in agents:
                        home.kernel.agents.get(aid)
                    parent_id = home.kernel.workflow.create_job(
                        objective, chain=["fleet", "governance"])
                    t = threading.Thread(
                        target=home._guarded,
                        args=(parent_id, lambda: home.kernel.fleet_run(
                            objective, agents, job_id=parent_id)),
                        daemon=True)
                    t.start(); home._threads.append(t)
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "body must be JSON"})
                except AgentError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(202, {"started": True, "job_id": parent_id})
            if u.path.startswith("/api/connectors/"):
                from .connectors import ConnectorError
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    status = home.kernel.connectors.configure(
                        u.path.rsplit("/", 1)[-1], payload.get("base_url"))
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "body must be JSON"})
                except ConnectorError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(200, {"connector": status})
            if u.path == "/api/peers":
                from .federation import FederationError
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    peer = home.kernel.peers.register(
                        payload.get("name"), payload.get("url"),
                        payload.get("pubkey"))
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "body must be JSON"})
                except FederationError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(201, {"peer": peer})
            if u.path == "/api/federation/dispatch":
                from .federation import FederationError
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    reply = home.kernel.federation_dispatch(
                        payload.get("peer"), payload.get("agent"),
                        payload.get("objective"))
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "body must be JSON"})
                except FederationError as e:
                    return self._json(502, {"error": str(e)})
                return self._json(200, reply)
            if u.path == "/api/federation/run":
                from .federation import FederationError, verify_incoming
                from .agents import AgentError
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    peer = verify_incoming(home.kernel.peers, payload)
                    home.kernel.peers.check_replay(payload)
                    result = home.kernel.federation_receive(
                        peer["name"], payload["agent"], payload["objective"])
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "body must be JSON"})
                except AgentError as e:
                    return self._json(400, {"error": str(e)})
                except FederationError as e:
                    return self._json(401, {"error": str(e)})
                return self._json(200, result)
            if u.path == "/api/plugins":
                from .plugins import PluginError
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    home.kernel.plugins.set_asserted(
                        payload.get("id"), bool(payload.get("connected")))
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "body must be JSON"})
                except PluginError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(200, {"ok": True})
            if u.path == "/api/devices":
                from .devices import DeviceError
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    device = home.kernel.devices.register(
                        payload.get("name"), payload.get("kind", "device"),
                        payload.get("address", ""))
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "body must be JSON"})
                except DeviceError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(201, {"device": device})
            if u.path == "/api/spawn":
                from .agents import AgentError
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = json.loads(self.rfile.read(length) or b"{}")
                    agent_id = payload.get("agent")
                    objective = payload.get("objective")
                    parent = payload.get("parent")
                    if not isinstance(objective, str) or not objective.strip():
                        return self._json(400, {"error": "objective must be a non-empty string"})
                    home.kernel.agents.get(agent_id)  # validate first
                    child_id = home.kernel.workflow.create_job(
                        objective, chain=[agent_id, "governance"])
                    home.kernel.ledger.append("crow-os/kernel", "agent.spawned", {
                        "parent_job_id": parent, "child_job_id": child_id,
                        "agent": agent_id, "depth": 1})
                    t = threading.Thread(
                        target=home._guarded,
                        args=(child_id, lambda: home.kernel.run_agent(
                            objective, agent_id, job_id=child_id)),
                        daemon=True)
                    t.start(); home._threads.append(t)
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "body must be JSON"})
                except AgentError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(202, {"started": True, "job_id": child_id})
            if u.path != "/api/run":
                return self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._json(400, {"error": "body must be JSON"})
            objective = payload.get("objective")
            if not isinstance(objective, str) or not objective.strip():
                return self._json(400, {"error": "objective must be a non-empty string"})
            if len(objective) > 10_000:
                return self._json(400, {"error": "objective too long (max 10000 chars)"})
            mode = payload.get("mode", "auto")
            if mode not in ("demo", "llm", "auto"):
                return self._json(400, {"error": "mode must be 'demo', 'llm' or 'auto'"})
            model = payload.get("model")
            if model is not None and not isinstance(model, str):
                return self._json(400, {"error": "model must be a string"})
            agent_id = payload.get("agent")
            if agent_id is not None and not isinstance(agent_id, str):
                return self._json(400, {"error": "agent must be a string"})
            tools_enabled = payload.get("tools", True) is not False
            from .agents import AgentError
            try:
                self._json(202, home.start_run(objective, mode, model=model,
                                               agent_id=agent_id,
                                               tools_enabled=tools_enabled))
            except AgentError as e:
                return self._json(400, {"error": str(e)})

        def _stream(self, job_id: str) -> None:
            """Server-sent events: the job's ledger entries as they land,
            then the terminal status. This is the live feed the Home
            Screen's thinking cards render from."""
            try:
                home.kernel.workflow.get(job_id)
            except Exception:
                return self._json(404, {"error": "no such job"})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            sent = 0
            import time as _time
            for _ in range(600):  # ~5 minutes max
                detail = home.job_detail(job_id)
                entries = detail["entries"]
                while sent < len(entries):
                    e = entries[sent]; sent += 1
                    payload = json.dumps({"entry": e, "job": detail["job"],
                                          "outputs": detail["outputs"]})
                    try:
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                if detail["job"]["status"] != "running":
                    return
                _time.sleep(0.5)

        def log_message(self, *args):  # keep the terminal quiet
            pass

    return Handler


def serve(kernel: Kernel, port: int) -> int:
    home = HomeServer(kernel, port=port)

    def _routine_loop() -> None:
        while True:
            time.sleep(5)
            try:
                for routine in kernel.routines.due():
                    threading.Thread(
                        target=kernel.fire_routine, args=(routine,),
                        daemon=True).start()
            except Exception:  # noqa: BLE001 - the loop must not die
                continue
    threading.Thread(target=_routine_loop, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(home))
    print(f"Crow OS Home Screen: http://127.0.0.1:{port}  (home: {kernel.home})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
