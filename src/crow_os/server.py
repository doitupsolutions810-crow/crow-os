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
from .llm import DEFAULT_MODEL
from .runtime import Kernel

HOME_HTML = Path(__file__).with_name("home.html")


class HomeServer:
    def __init__(self, kernel: Kernel):
        self.kernel = kernel
        self._threads: list[threading.Thread] = []

    # -- API ------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        verdict = self.kernel.ledger.verify()
        return {
            "version": __version__,
            "home": str(self.kernel.home),
            "model": DEFAULT_MODEL,
            "ledger": {"ok": verdict["ok"], "entries": verdict["entries"],
                       "errors": verdict["errors"][:5]},
            "jobs": len(self.kernel.workflow.jobs()),
        }

    def start_run(self, objective: str, mode: str) -> dict[str, Any]:
        # One wiring path for both modes: the job is created up front
        # (so the caller always gets its id immediately and the run is
        # in the ledger even if the server dies), then the KERNEL's own
        # chain implementation executes it in a thread. There is no
        # second copy of the chain logic here to drift out of sync.
        job_id = self.kernel.workflow.create_job(objective)
        if mode == "llm":
            target = lambda: self.kernel.run_llm(objective, job_id=job_id)  # noqa: E731
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
            elif u.path == "/api/jobs":
                self._json(200, {"jobs": home.kernel.workflow.jobs()})
            elif u.path.startswith("/api/jobs/"):
                detail = home.job_detail(u.path.rsplit("/", 1)[-1])
                if detail is None:
                    self._json(404, {"error": "no such job"})
                else:
                    self._json(200, detail)
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

        def do_POST(self):  # noqa: N802
            u = urlparse(self.path)
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
            mode = payload.get("mode", "demo")
            if mode not in ("demo", "llm"):
                return self._json(400, {"error": "mode must be 'demo' or 'llm'"})
            self._json(202, home.start_run(objective, mode))

        def log_message(self, *args):  # keep the terminal quiet
            pass

    return Handler


def serve(kernel: Kernel, port: int) -> int:
    home = HomeServer(kernel)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(home))
    print(f"Crow OS Home Screen: http://127.0.0.1:{port}  (home: {kernel.home})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
