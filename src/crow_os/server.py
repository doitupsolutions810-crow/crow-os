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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .llm import DEFAULT_BASE, DEFAULT_MODEL, MODEL_FROM_ENV, LLMUnavailable, OllamaClient, pick_local_model
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
            # The model a run with no explicit choice will actually use: the default when
            # it is pulled (or set by CROW_OS_MODEL), else the best installed local model.
            active = DEFAULT_MODEL if (h["model_present"] or MODEL_FROM_ENV) \
                else (pick_local_model(h["models"]) or DEFAULT_MODEL)
            return {"reachable": True, "base": DEFAULT_BASE,
                    "active": active, "default": DEFAULT_MODEL, "installed": h["models"],
                    "active_installed": active in h["models"]}
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
            info = self.model_info()
            # reachable with no usable model (nothing pulled yet) runs the demo instead of
            # failing every job on a model that is not there; an explicit model is honored
            mode = "llm" if info["reachable"] and (model or info["active_installed"]) else "demo"
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
        def _read_obj(self, length: int) -> dict:
            """Request body as a JSON object. A list, a string, null, a negative
            length (rfile.read(-1) would block the handler thread until the client
            hangs up) or a body over 1 MiB raise ValueError -> clean 400."""
            if length < 0 or length > 1 << 20:
                raise ValueError("bad Content-Length")
            payload = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("body must be a JSON object")
            return payload

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
            elif u.path == "/api/plugins":
                info = home.model_info()
                plugins = home.kernel.plugins.list(info["reachable"])
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
                    payload = self._read_obj(length)
                    agent = home.kernel.agents.create(
                        payload.get("name"), payload.get("instructions"),
                        model=payload.get("model"),
                        tools=payload.get("tools"),
                        avatar=payload.get("avatar"))
                except (ValueError, json.JSONDecodeError):
                    return self._json(400, {"error": "body must be JSON"})
                except AgentError as e:
                    return self._json(400, {"error": str(e)})
                return self._json(201, {"agent": agent})
            if u.path == "/api/plugins":
                from .plugins import PluginError
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    payload = self._read_obj(length)
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
                    payload = self._read_obj(length)
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
                    payload = self._read_obj(length)
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
                payload = self._read_obj(length)
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
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(home))
    print(f"Crow OS Home Screen: http://127.0.0.1:{port}  (home: {kernel.home})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
